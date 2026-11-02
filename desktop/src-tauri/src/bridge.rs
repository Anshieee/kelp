//! HTTP/SSE bridge between the webview and `chat/api.py`.
//!
//! The webview cannot call the server directly: there is no CORS layer, and the
//! server's CSRF gate compares `Origin` to `Host` and rejects non-http schemes,
//! so a `tauri://` origin fails twice over. Rust makes the calls instead, keeps
//! the cookie jar, and forwards SSE frames as Tauri events.
//!
//! The bearer is used once, in `bridge_sign_in`. Nothing else carries it.

use std::collections::HashMap;
use std::sync::Mutex;

use futures_util::StreamExt;
use serde::Serialize;
use serde_json::{json, Value};
use tauri::{Emitter, State};

const SERVER_ENV: &str = "KELP_SERVER_URL";
const DEFAULT_SERVER: &str = "http://127.0.0.1:8000";

/// A failure the webview can render without parsing HTTP itself.
#[derive(Debug, Serialize)]
pub struct ApiError {
    pub status: u16,
    pub code: String,
    pub message: String,
    pub retryable: bool,
}

impl ApiError {
    fn transport(message: impl Into<String>) -> Self {
        Self {
            status: 0,
            code: "transport".into(),
            message: message.into(),
            retryable: true,
        }
    }

    /// Read the server's `{ok:false, error:{...}}` envelope.
    fn from_body(status: u16, body: &Value) -> Self {
        let error = body.get("error").cloned().unwrap_or(Value::Null);
        let text = |key: &str, fallback: &str| {
            error
                .get(key)
                .and_then(Value::as_str)
                .unwrap_or(fallback)
                .to_string()
        };
        Self {
            status,
            code: text("code", "request_failed"),
            message: text("message", "The request did not go through."),
            retryable: error
                .get("retryable")
                .and_then(Value::as_bool)
                .unwrap_or(status >= 500 || status == 429),
        }
    }
}

pub struct Bridge {
    client: reqwest::Client,
    server: String,
    csrf: Mutex<Option<String>>,
    streams: Mutex<HashMap<String, tokio::sync::watch::Sender<bool>>>,
}

impl Bridge {
    pub fn new() -> Self {
        let server = std::env::var(SERVER_ENV)
            .ok()
            .filter(|value| !value.trim().is_empty())
            .unwrap_or_else(|| DEFAULT_SERVER.to_string());
        let server = server.trim_end_matches('/').to_string();
        let client = reqwest::Client::builder()
            .cookie_store(true)
            .build()
            .expect("failed to build the bridge HTTP client");
        Self {
            client,
            server,
            csrf: Mutex::new(None),
            streams: Mutex::new(HashMap::new()),
        }
    }

    fn csrf_token(&self) -> String {
        self.csrf
            .lock()
            .ok()
            .and_then(|slot| slot.clone())
            .unwrap_or_default()
    }

    fn cancel_stream(&self, thread_id: &str) {
        if let Ok(mut streams) = self.streams.lock() {
            if let Some(tx) = streams.remove(thread_id) {
                let _ = tx.send(true);
            }
        }
    }

    async fn request(
        &self,
        method: reqwest::Method,
        path: &str,
        body: Option<Value>,
    ) -> Result<Value, ApiError> {
        let mut request = self
            .client
            .request(method.clone(), format!("{}{}", self.server, path));

        if method != reqwest::Method::GET {
            // Rust is not a browser: it supplies the same-origin proof and the
            // double-submit half itself.
            request = request
                .header("Origin", &self.server)
                .header("X-CSRF-Token", self.csrf_token());
        }
        if let Some(payload) = body {
            request = request.json(&payload);
        }

        let response = request
            .send()
            .await
            .map_err(|error| ApiError::transport(error.to_string()))?;
        let status = response.status().as_u16();
        let text = response.text().await.unwrap_or_default();
        let value: Value = serde_json::from_str(&text).unwrap_or(Value::Null);

        if status >= 400 {
            return Err(ApiError::from_body(status, &value));
        }
        Ok(value)
    }
}

#[tauri::command]
pub fn bridge_server_url(bridge: State<'_, Bridge>) -> String {
    bridge.server.clone()
}

#[tauri::command]
pub async fn bridge_sign_in(
    bridge: State<'_, Bridge>,
    bearer: String,
    device_label: String,
) -> Result<Value, ApiError> {
    let response = bridge
        .client
        .post(format!("{}/api/auth/session", bridge.server))
        .header("Authorization", format!("Bearer {bearer}"))
        .json(&json!({ "device_label": device_label }))
        .send()
        .await
        .map_err(|error| ApiError::transport(error.to_string()))?;

    // Capture the CSRF half before the body is consumed: it is the cookie the
    // browser would have read with document.cookie.
    for value in response.headers().get_all(reqwest::header::SET_COOKIE).iter() {
        if let Ok(cookie) = value.to_str() {
            if let Some(rest) = cookie.strip_prefix("csrf=") {
                let token = rest.split(';').next().unwrap_or("").to_string();
                if let Ok(mut slot) = bridge.csrf.lock() {
                    *slot = Some(token);
                }
            }
        }
    }

    let status = response.status().as_u16();
    let text = response.text().await.unwrap_or_default();
    let value: Value = serde_json::from_str(&text).unwrap_or(Value::Null);
    if status >= 400 {
        return Err(ApiError::from_body(status, &value));
    }
    Ok(value)
}

#[tauri::command]
pub async fn bridge_sign_out(bridge: State<'_, Bridge>) -> Result<Value, ApiError> {
    let result = bridge
        .request(reqwest::Method::DELETE, "/api/auth/session", None)
        .await;
    if let Ok(mut slot) = bridge.csrf.lock() {
        *slot = None;
    }
    result
}

#[tauri::command]
pub async fn bridge_api(
    bridge: State<'_, Bridge>,
    method: String,
    path: String,
    body: Option<Value>,
) -> Result<Value, ApiError> {
    let method = reqwest::Method::from_bytes(method.to_uppercase().as_bytes())
        .map_err(|_| ApiError::transport("Unsupported method"))?;
    bridge.request(method, &path, body).await
}

/// Open one SSE subscription. Frames arrive as `chat-event`.
#[tauri::command]
pub async fn bridge_open_stream(
    app: tauri::AppHandle,
    bridge: State<'_, Bridge>,
    thread_id: String,
    after: u64,
) -> Result<(), ApiError> {
    bridge.cancel_stream(&thread_id);

    let (cancel_tx, mut cancel_rx) = tokio::sync::watch::channel(false);
    if let Ok(mut streams) = bridge.streams.lock() {
        streams.insert(thread_id.clone(), cancel_tx);
    }

    let client = bridge.client.clone();
    let url = format!(
        "{}/api/threads/{}/stream?after={}",
        bridge.server, thread_id, after
    );
    let handle = app.clone();
    let id = thread_id.clone();

    tauri::async_runtime::spawn(async move {
        let response = client
            .get(&url)
            .header("Accept", "text/event-stream")
            .send()
            .await;

        let response = match response {
            Ok(response) if response.status().is_success() => response,
            Ok(response) => {
                let _ = handle.emit(
                    "chat-stream-state",
                    json!({ "threadId": id, "state": "error", "status": response.status().as_u16() }),
                );
                return;
            }
            Err(error) => {
                let _ = handle.emit(
                    "chat-stream-state",
                    json!({ "threadId": id, "state": "error", "message": error.to_string() }),
                );
                return;
            }
        };

        let _ = handle.emit("chat-stream-state", json!({ "threadId": id, "state": "open" }));

        let mut body = response.bytes_stream();
        let mut buffer = String::new();

        loop {
            tokio::select! {
                _ = cancel_rx.changed() => break,
                chunk = body.next() => match chunk {
                    Some(Ok(bytes)) => {
                        buffer.push_str(&String::from_utf8_lossy(&bytes));
                        // Frames end with a blank line.
                        while let Some(index) = buffer.find("\n\n") {
                            let frame: String = buffer.drain(..index + 2).collect();
                            forward_frame(&handle, &id, &frame);
                        }
                    }
                    Some(Err(error)) => {
                        let _ = handle.emit(
                            "chat-stream-state",
                            json!({ "threadId": id, "state": "error", "message": error.to_string() }),
                        );
                        break;
                    }
                    None => break,
                },
            }
        }

        let _ = handle.emit("chat-stream-state", json!({ "threadId": id, "state": "closed" }));
    });

    Ok(())
}

#[tauri::command]
pub fn bridge_close_stream(bridge: State<'_, Bridge>, thread_id: String) {
    bridge.cancel_stream(&thread_id);
}

/// Turn one SSE frame into a `chat-event`. Comments (heartbeats) carry no data
/// and are dropped.
fn forward_frame(app: &tauri::AppHandle, thread_id: &str, frame: &str) {
    let mut id: Option<String> = None;
    let mut event: Option<String> = None;
    let mut data = String::new();

    for line in frame.lines() {
        if let Some(rest) = line.strip_prefix("id:") {
            id = Some(rest.trim().to_string());
        } else if let Some(rest) = line.strip_prefix("event:") {
            event = Some(rest.trim().to_string());
        } else if let Some(rest) = line.strip_prefix("data:") {
            if !data.is_empty() {
                data.push('\n');
            }
            data.push_str(rest.trim_start());
        }
    }

    if data.is_empty() {
        return;
    }

    let parsed: Value = serde_json::from_str(&data).unwrap_or(Value::Null);
    let kind = event.or_else(|| {
        parsed
            .get("type")
            .and_then(Value::as_str)
            .map(str::to_string)
    });

    let _ = app.emit(
        "chat-event",
        json!({
            "threadId": thread_id,
            "id": id,
            "event": kind,
            "data": parsed,
        }),
    );
}

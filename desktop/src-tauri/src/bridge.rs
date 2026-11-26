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

#[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
#[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
use std::sync::Arc;

use futures_util::StreamExt;
use serde::Serialize;
use serde_json::{json, Value};
use tauri::{Emitter, State};

#[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
use crate::local_transport::{LocalTransport, LocalTransportError};

const SERVER_ENV: &str = "KELP_SERVER_URL";
/// The fixed non-URL label the Local backend reports to the webview.
#[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
const LOCAL_SERVER_LABEL: &str = "local";
/// Bound for one incomplete Local SSE frame held across reads.
#[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
const LOCAL_STREAM_FRAME_LIMIT: usize = 1024 * 1024;
/// Debug builds talk to a local `make serve` instance. Release builds default
/// to the https placeholder and fail closed (a connection error, not a silent
/// cleartext dial) until the operator sets KELP_SERVER_URL — a packaged app
/// must never default to loopback in production.
#[cfg(debug_assertions)]
const DEFAULT_SERVER: &str = "http://127.0.0.1:8000";
#[cfg(not(debug_assertions))]
const DEFAULT_SERVER: &str = "https://kelp.example.com";

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

/// Fixed Local refusals. None of these ever includes a URL, transport error,
/// credential, or header value.
#[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
impl ApiError {
    fn fixed(status: u16, code: &str, message: &str, retryable: bool) -> Self {
        Self {
            status,
            code: code.into(),
            message: message.into(),
            retryable,
        }
    }

    fn local_transport() -> Self {
        Self::fixed(
            0,
            "local_transport",
            "The local sidecar did not answer.",
            true,
        )
    }

    fn local_unavailable() -> Self {
        Self::fixed(
            0,
            "local_unavailable",
            "The local sidecar is not running.",
            false,
        )
    }

    /// A Local 401 is remapped away from 401 so the webview never offers the
    /// hosted sign-in prompt for a Rust-owned owner credential.
    fn local_auth_failed() -> Self {
        Self::fixed(
            500,
            "local_auth_failed",
            "The local sidecar rejected the desktop owner credential.",
            false,
        )
    }

    fn local_sign_in_refused() -> Self {
        Self::fixed(
            400,
            "local_sign_in_unavailable",
            "Local mode does not use a hosted sign-in.",
            false,
        )
    }

    fn local_sign_out_refused() -> Self {
        Self::fixed(
            400,
            "local_sign_out_unavailable",
            "Local mode does not use a hosted sign-out.",
            false,
        )
    }

    fn local_approvals_unavailable() -> Self {
        Self::fixed(
            400,
            "local_approvals_unavailable",
            "Approvals are not available in local mode.",
            false,
        )
    }

    /// No redirect is part of the Local contract. The error is fixed and never
    /// interprets the redirect body.
    fn local_unexpected_redirect() -> Self {
        Self::fixed(
            502,
            "unexpected_redirect",
            "The local sidecar returned an unexpected redirect.",
            false,
        )
    }
}

/// One route segment of a backend id (`thread_…`, `turn_…`, `approval_…`).
/// Backend ids are hex/UUID-style spellings; anything outside this charset is
/// rejected rather than smuggled into a URL.
fn is_id_segment(segment: &str) -> bool {
    !segment.is_empty()
        && segment.len() <= 128
        && segment
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

/// Percent-encode a path segment per RFC 3986, leaving unreserved bytes alone.
/// Combined with the id checks below this is belt and braces: a validated id
/// encodes to itself, and anything unvalidated never reaches a URL.
fn encode_path_segment(segment: &str) -> String {
    const UNRESERVED: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~";
    let mut out = String::with_capacity(segment.len());
    for &b in segment.as_bytes() {
        if UNRESERVED.contains(&b) {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// The hosted backend's mutable session state. Kept intact for the default
/// path: cookie jar, server text, and CSRF half.
struct HostedBridge {
    client: reqwest::Client,
    server: String,
    csrf: Mutex<Option<String>>,
}

/// The one backend a [`Bridge`] talks to. The variant is fixed at startup; no
/// command can select, construct, or fall back to the other one.
enum Backend {
    Hosted(HostedBridge),
    #[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
    Local {
        transport: Arc<LocalTransport>,
        /// Best-effort child liveness set by `LocalRuntime::shutdown`.
        closed: Arc<AtomicBool>,
    },
}

/// One local stream registration. `generation` distinguishes the current
/// owner of a thread id from an older task that has not wound down yet.
#[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
struct LocalStreamRegistration {
    generation: u64,
    cancel: tokio::sync::watch::Sender<bool>,
}

/// Local-only stream ownership. Every open gets a fresh generation; emissions
/// and terminal cleanup check that generation under the same lock, so a
/// replaced task can never write to the thread map or emit into the new
/// stream's lifetime.
#[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
struct LocalStreams {
    next_generation: AtomicU64,
    current: Mutex<HashMap<String, LocalStreamRegistration>>,
}

#[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
impl LocalStreams {
    fn new() -> Self {
        Self {
            next_generation: AtomicU64::new(0),
            current: Mutex::new(HashMap::new()),
        }
    }

    /// Replace any current registration and return this task's generation and
    /// cancellation receiver. The closed check happens under the same lock as
    /// insertion, so `RunEvent::Exit` (flag, then drain) cannot be raced by a
    /// new stream.
    fn start(
        &self,
        thread_id: &str,
        closed: &AtomicBool,
    ) -> Option<(u64, tokio::sync::watch::Receiver<bool>)> {
        let generation = self
            .next_generation
            .fetch_add(1, Ordering::SeqCst)
            .wrapping_add(1);
        let (cancel, receiver) = tokio::sync::watch::channel(false);
        let mut current = self.current.lock().ok()?;
        if closed.load(Ordering::SeqCst) {
            return None;
        }
        let replaced = current.insert(
            thread_id.to_owned(),
            LocalStreamRegistration { generation, cancel },
        );
        drop(current);
        if let Some(replaced) = replaced {
            let _ = replaced.cancel.send(true);
        }
        Some((generation, receiver))
    }

    /// True only for the current owner. Used by focused tests; production
    /// emissions go through [`Self::emit_if_current`].
    #[cfg(test)]
    fn is_current(&self, thread_id: &str, generation: u64) -> bool {
        self.current
            .lock()
            .ok()
            .map(|current| current.get(thread_id).map(|entry| entry.generation))
            .flatten()
            == Some(generation)
    }

    /// Run one emission under the registration lock, only while this task owns
    /// the thread. Holding the lock through the emit means a replacement cannot
    /// interleave between the ownership check and the write.
    fn emit_if_current<F: FnOnce()>(&self, thread_id: &str, generation: u64, emit: F) -> bool {
        let Ok(current) = self.current.lock() else {
            return false;
        };
        if current.get(thread_id).map(|entry| entry.generation) != Some(generation) {
            return false;
        }
        emit();
        true
    }

    /// Emit the terminal state and remove only this generation. A replaced task
    /// must not remove the new owner's entry or emit its closed event.
    fn finish_if_current<F: FnOnce()>(&self, thread_id: &str, generation: u64, emit: F) -> bool {
        let Ok(mut current) = self.current.lock() else {
            return false;
        };
        if current.get(thread_id).map(|entry| entry.generation) != Some(generation) {
            return false;
        }
        emit();
        current.remove(thread_id);
        true
    }

    /// Signal the current stream without removing it: its own terminal path
    /// emits `closed` and cleans up.
    fn cancel_current(&self, thread_id: &str) {
        if let Ok(current) = self.current.lock() {
            if let Some(entry) = current.get(thread_id) {
                let _ = entry.cancel.send(true);
            }
        }
    }

    /// Shutdown path: drain everything and cancel. Drained receivers no longer
    /// own an entry, so their terminal paths emit nothing.
    fn cancel_all(&self) {
        let Ok(mut current) = self.current.lock() else {
            return;
        };
        let drained: Vec<LocalStreamRegistration> =
            current.drain().map(|(_, entry)| entry).collect();
        drop(current);
        for entry in drained {
            let _ = entry.cancel.send(true);
        }
    }
}

pub struct Bridge {
    backend: Backend,
    /// Hosted per-thread cancellation senders. Hosted semantics are unchanged.
    streams: Mutex<HashMap<String, tokio::sync::watch::Sender<bool>>>,
    /// Local-only generation-checked registrations.
    #[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
    local_streams: Arc<LocalStreams>,
    reachable: Mutex<Option<bool>>,
}

impl HostedBridge {
    fn new() -> Self {
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
        }
    }

    fn csrf_token(&self) -> String {
        self.csrf
            .lock()
            .ok()
            .and_then(|slot| slot.clone())
            .unwrap_or_default()
    }
}

impl Bridge {
    /// The default hosted bridge. Behaviour is unchanged from the first
    /// bridge: `KELP_SERVER_URL`, a cookie jar, and per-session CSRF.
    pub fn new() -> Self {
        Self {
            backend: Backend::Hosted(HostedBridge::new()),
            streams: Mutex::new(HashMap::new()),
            #[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
            local_streams: Arc::new(LocalStreams::new()),
            reachable: Mutex::new(None),
        }
    }

    /// A bridge bound to one verified Local transport and its owned child's
    /// liveness flag. Never constructed unless the guard already passed.
    #[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
    pub(crate) fn local(transport: LocalTransport, closed: Arc<AtomicBool>) -> Self {
        Self {
            backend: Backend::Local {
                transport: Arc::new(transport),
                closed,
            },
            streams: Mutex::new(HashMap::new()),
            local_streams: Arc::new(LocalStreams::new()),
            reachable: Mutex::new(None),
        }
    }

    fn hosted(&self) -> Option<&HostedBridge> {
        match &self.backend {
            Backend::Hosted(hosted) => Some(hosted),
            #[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
            Backend::Local { .. } => None,
        }
    }

    fn is_local(&self) -> bool {
        match &self.backend {
            Backend::Hosted(_) => false,
            #[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
            Backend::Local { .. } => true,
        }
    }

    fn backend_name(&self) -> &'static str {
        if self.is_local() {
            "local"
        } else {
            "hosted"
        }
    }

    fn server_label(&self) -> &str {
        match &self.backend {
            Backend::Hosted(hosted) => hosted.server.as_str(),
            #[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
            Backend::Local { .. } => LOCAL_SERVER_LABEL,
        }
    }

    fn cancel_stream(&self, thread_id: &str) {
        if let Ok(mut streams) = self.streams.lock() {
            if let Some(tx) = streams.remove(thread_id) {
                let _ = tx.send(true);
            }
        }
    }

    /// Cancel every live stream. Used on Local sign-out and on
    /// `RunEvent::Exit`; clearing the map releases every sender.
    #[cfg_attr(not(feature = "local-dev"), allow(dead_code))]
    pub(crate) fn cancel_all_streams(&self) {
        if let Ok(mut streams) = self.streams.lock() {
            for (_, tx) in streams.drain() {
                let _ = tx.send(true);
            }
        }
        #[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
        self.local_streams.cancel_all();
    }

    /// Tell every window whether the server can be reached, but only when the
    /// answer *changes*: a flapping connection must not spam the pet with
    /// events, and the first answer is worth emitting so it can leave `idle`.
    fn note_reachability(&self, app: &tauri::AppHandle, reachable: bool) {
        let changed = match self.reachable.lock() {
            Ok(mut slot) => {
                let changed = *slot != Some(reachable);
                *slot = Some(reachable);
                changed
            }
            Err(_) => false,
        };
        if changed {
            let _ = app.emit("bridge-reachability", json!({ "reachable": reachable }));
        }
    }
}

/// The routes the webview is allowed to reach through `bridge_api`, with the
/// methods each accepts. This is an allowlist, not a prefix check: the webview
/// is untrusted input (any XSS reaches these commands), so a caller-controlled
/// `path` like `/api/../admin` or `/other-host` must be rejected, not
/// concatenated. Keep in sync with `desktop/src/bridge.ts`.
fn check_api_route(method: &reqwest::Method, path: &str) -> Result<(), ApiError> {
    if !path.starts_with('/') || path.bytes().any(|b| b <= b' ' || b == 0x7f) {
        return Err(ApiError::transport("Unsupported bridge request"));
    }
    let allowed = match (method.as_str(), path) {
        ("GET", "/api/threads") => true,
        ("POST", "/api/threads") => true,
        // `GET /api/auth/session` is handled by bridge_sign_in, not bridge_api,
        // and `DELETE /api/auth/session` by bridge_sign_out — they are not
        // re-exposed here.
        _ => false,
    };
    if allowed {
        return Ok(());
    }
    // `GET /api/threads?limit=N` is the only call that carries a query.
    if *method == reqwest::Method::GET {
        if let Some(rest) = path.strip_prefix("/api/threads?limit=") {
            if !rest.is_empty() && rest.bytes().all(|b| b.is_ascii_digit()) {
                return Ok(());
            }
        }
    }
    let segments: Vec<&str> = path.split('/').collect();
    let allowed = match (method.as_str(), segments.as_slice()) {
        ("GET", ["", "api", "threads", id]) => is_id_segment(id),
        ("POST", ["", "api", "threads", id, "turns"]) => is_id_segment(id),
        ("POST", ["", "api", "turns", id, "stop"]) => is_id_segment(id),
        ("POST", ["", "api", "turns", id, "retry"]) => is_id_segment(id),
        ("POST", ["", "api", "approvals", id, "decision"]) => is_id_segment(id),
        _ => false,
    };
    if allowed {
        Ok(())
    } else {
        Err(ApiError::transport("Unsupported bridge request"))
    }
}

impl Bridge {
    async fn hosted_request(
        &self,
        app: &tauri::AppHandle,
        method: reqwest::Method,
        path: &str,
        body: Option<Value>,
    ) -> Result<Value, ApiError> {
        let hosted = self
            .hosted()
            .ok_or_else(|| ApiError::transport("Unsupported bridge request"))?;
        let mut request = hosted
            .client
            .request(method.clone(), format!("{}{}", hosted.server, path));

        if method != reqwest::Method::GET {
            // Rust is not a browser: it supplies the same-origin proof and the
            // double-submit half itself.
            request = request
                .header("Origin", &hosted.server)
                .header("X-CSRF-Token", hosted.csrf_token());
        }
        if let Some(payload) = body {
            request = request.json(&payload);
        }

        let response = match request.send().await {
            Ok(response) => response,
            Err(error) => {
                self.note_reachability(app, false);
                return Err(ApiError::transport(error.to_string()));
            }
        };
        // Any answer at all — including a 500 — proves the server is there.
        self.note_reachability(app, true);

        let status = response.status().as_u16();
        let text = response.text().await.unwrap_or_default();
        let value: Value = serde_json::from_str(&text).unwrap_or(Value::Null);

        if status >= 400 {
            return Err(ApiError::from_body(status, &value));
        }
        Ok(value)
    }
}

/// Map one bounded Local REST reply to the webview error shape.
///
/// A 401 becomes the fixed non-401 `local_auth_failed`; transport failures are
/// never rendered here at all. HTTP envelopes come from the local server and
/// contain fixed text only.
#[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
fn local_error(status: u16, body: &[u8]) -> ApiError {
    // Redirects are never part of the Local profile: the transport does not
    // follow them, and the bridge must not read the redirect body as a
    // success or as a server envelope.
    if (300..400).contains(&status) {
        return ApiError::local_unexpected_redirect();
    }
    if status == 401 {
        return ApiError::local_auth_failed();
    }
    let value: Value = serde_json::from_slice(body).unwrap_or(Value::Null);
    ApiError::from_body(status, &value)
}

#[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
fn emit_local_state(
    streams: &LocalStreams,
    handle: &tauri::AppHandle,
    thread_id: &str,
    generation: u64,
    value: Value,
) {
    streams.emit_if_current(thread_id, generation, || {
        let _ = handle.emit("chat-stream-state", value);
    });
}

#[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
fn finish_local_stream(
    streams: &LocalStreams,
    handle: &tauri::AppHandle,
    thread_id: &str,
    generation: u64,
) {
    streams.finish_if_current(thread_id, generation, || {
        let _ = handle.emit(
            "chat-stream-state",
            json!({ "threadId": thread_id, "state": "closed" }),
        );
    });
}

#[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
fn is_local_approval_decision(method: &reqwest::Method, path: &str) -> bool {
    if *method != reqwest::Method::POST {
        return false;
    }
    let segments: Vec<&str> = path.split('/').collect();
    matches!(
        segments.as_slice(),
        ["", "api", "approvals", id, "decision"] if is_id_segment(id)
    )
}

#[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
impl Bridge {
    fn local_closed(&self) -> bool {
        self.local_closed_flag()
            .map_or(true, |closed| closed.load(Ordering::SeqCst))
    }

    fn local_closed_flag(&self) -> Option<Arc<AtomicBool>> {
        match &self.backend {
            Backend::Local { closed, .. } => Some(Arc::clone(closed)),
            Backend::Hosted(_) => None,
        }
    }

    fn local_transport(&self) -> Option<Arc<LocalTransport>> {
        match &self.backend {
            Backend::Local { transport, .. } => Some(Arc::clone(transport)),
            Backend::Hosted(_) => None,
        }
    }

    fn close_local_stream(&self, thread_id: &str) {
        // Signal the current owner without deleting its registration: the
        // task's own terminal path emits `closed` and removes only its token.
        self.local_streams.cancel_current(thread_id);
    }

    /// Execute one Local REST call through the sealed transport method. The
    /// command path never receives a request builder or a response stream.
    async fn local_request(
        &self,
        method: reqwest::Method,
        path: &str,
        body: Option<Value>,
    ) -> Result<Value, ApiError> {
        if self.local_closed() {
            return Err(ApiError::local_unavailable());
        }
        let transport = self
            .local_transport()
            .ok_or_else(ApiError::local_unavailable)?;
        let response = transport
            .send_api(method, path, body)
            .await
            .map_err(|_| ApiError::local_transport())?;
        let status = response.status();
        if status >= 300 {
            return Err(local_error(status, response.body()));
        }
        Ok(serde_json::from_slice(response.body()).unwrap_or(Value::Null))
    }

    /// Open one Local SSE subscription. Cancellation is honoured before
    /// headers and while reading; frames reuse the hosted `chat-event` and
    /// `chat-stream-state` shapes.
    async fn open_local_stream(
        &self,
        app: &tauri::AppHandle,
        thread_id: String,
        after: u64,
    ) -> Result<(), ApiError> {
        if self.local_closed() {
            return Err(ApiError::local_unavailable());
        }
        let transport = self
            .local_transport()
            .ok_or_else(ApiError::local_unavailable)?;
        let closed = self
            .local_closed_flag()
            .ok_or_else(ApiError::local_unavailable)?;
        let streams = Arc::clone(&self.local_streams);

        // Atomic with the closed check inside `start`: Exit sets the flag and
        // then drains, so a new registration cannot slip past the shutdown.
        let Some((generation, mut cancel_rx)) = streams.start(&thread_id, &closed) else {
            return Err(ApiError::local_unavailable());
        };

        let handle = app.clone();
        let id = thread_id;
        tauri::async_runtime::spawn(async move {
            let response = match transport.open_stream(&id, after, &mut cancel_rx).await {
                Ok(response) => response,
                Err(LocalTransportError::Cancelled) => {
                    finish_local_stream(&streams, &handle, &id, generation);
                    return;
                }
                Err(_) => {
                    emit_local_state(
                        &streams,
                        &handle,
                        &id,
                        generation,
                        json!({
                            "threadId": id,
                            "state": "error",
                            "message": "The local sidecar did not answer the live stream.",
                        }),
                    );
                    finish_local_stream(&streams, &handle, &id, generation);
                    return;
                }
            };

            let status = response.status().as_u16();
            if status == 401 {
                emit_local_state(
                    &streams,
                    &handle,
                    &id,
                    generation,
                    json!({
                        "threadId": id,
                        "state": "error",
                        "status": 500,
                        "message": "The local sidecar rejected the desktop owner credential.",
                    }),
                );
                finish_local_stream(&streams, &handle, &id, generation);
                return;
            }
            if !response.status().is_success() {
                emit_local_state(
                    &streams,
                    &handle,
                    &id,
                    generation,
                    json!({
                        "threadId": id,
                        "state": "error",
                        "status": status,
                        "message": "The local sidecar refused the live stream.",
                    }),
                );
                finish_local_stream(&streams, &handle, &id, generation);
                return;
            }

            emit_local_state(
                &streams,
                &handle,
                &id,
                generation,
                json!({ "threadId": id, "state": "open" }),
            );

            let mut body = response.bytes_stream();
            let mut buffer = String::new();

            loop {
                tokio::select! {
                    _ = cancel_rx.changed() => break,
                    chunk = body.next() => match chunk {
                        Some(Ok(bytes)) => {
                            buffer.push_str(&String::from_utf8_lossy(&bytes));
                            // Frames end with a blank line. Drain every
                            // complete frame first; only the leftover
                            // incomplete frame is bounded. The hosted path
                            // keeps its own limits; this bound is Local-only.
                            while let Some(index) = buffer.find("\n\n") {
                                let frame: String = buffer.drain(..index + 2).collect();
                                if let Some(event) = frame_event(&id, &frame) {
                                    streams.emit_if_current(&id, generation, || {
                                        let _ = handle.emit("chat-event", event);
                                    });
                                }
                            }
                            if buffer.len() > LOCAL_STREAM_FRAME_LIMIT {
                                emit_local_state(
                                    &streams,
                                    &handle,
                                    &id,
                                    generation,
                                    json!({
                                        "threadId": id,
                                        "state": "error",
                                        "message": "The local live stream sent an oversized frame.",
                                    }),
                                );
                                break;
                            }
                        }
                        Some(Err(_)) => {
                            emit_local_state(
                                &streams,
                                &handle,
                                &id,
                                generation,
                                json!({
                                    "threadId": id,
                                    "state": "error",
                                    "message": "The local live stream stopped.",
                                }),
                            );
                            break;
                        }
                        None => break,
                    },
                }
            }

            finish_local_stream(&streams, &handle, &id, generation);
        });

        Ok(())
    }
}

#[tauri::command]
pub fn bridge_server_url(bridge: State<'_, Bridge>) -> String {
    bridge.server_label().to_string()
}

/// The one authoritative backend label. Local never reports a URL or port.
#[tauri::command]
pub fn bridge_backend(bridge: State<'_, Bridge>) -> String {
    bridge.backend_name().to_string()
}

#[tauri::command]
pub async fn bridge_sign_in(
    app: tauri::AppHandle,
    bridge: State<'_, Bridge>,
    bearer: String,
    device_label: String,
) -> Result<Value, ApiError> {
    #[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
    if bridge.is_local() {
        // Local mode never mints a hosted session and never reads the UI
        // bearer. The owner credential is Rust-owned and never crosses this
        // command.
        return Err(ApiError::local_sign_in_refused());
    }
    let hosted = bridge
        .hosted()
        .ok_or_else(|| ApiError::transport("Unsupported bridge request"))?;
    let response = match hosted
        .client
        .post(format!("{}/api/auth/session", hosted.server))
        .header("Authorization", format!("Bearer {bearer}"))
        .json(&json!({ "device_label": device_label }))
        .send()
        .await
    {
        Ok(response) => response,
        Err(error) => {
            bridge.note_reachability(&app, false);
            return Err(ApiError::transport(error.to_string()));
        }
    };
    bridge.note_reachability(&app, true);

    // Capture the CSRF half before the body is consumed: it is the cookie the
    // browser would have read with document.cookie.
    for value in response
        .headers()
        .get_all(reqwest::header::SET_COOKIE)
        .iter()
    {
        if let Ok(cookie) = value.to_str() {
            if let Some(rest) = cookie.strip_prefix("csrf=") {
                let token = rest.split(';').next().unwrap_or("").to_string();
                if let Ok(mut slot) = hosted.csrf.lock() {
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
pub async fn bridge_sign_out(
    app: tauri::AppHandle,
    bridge: State<'_, Bridge>,
) -> Result<Value, ApiError> {
    #[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
    if bridge.is_local() {
        // No hosted session exists to revoke. Cancel local subscriptions and
        // return the fixed refusal; the Rust-owned child stays owned.
        bridge.cancel_all_streams();
        return Err(ApiError::local_sign_out_refused());
    }
    let result = bridge
        .hosted_request(&app, reqwest::Method::DELETE, "/api/auth/session", None)
        .await;
    if let Some(hosted) = bridge.hosted() {
        if let Ok(mut slot) = hosted.csrf.lock() {
            *slot = None;
        }
    }
    result
}

#[tauri::command]
pub async fn bridge_api(
    app: tauri::AppHandle,
    bridge: State<'_, Bridge>,
    method: String,
    path: String,
    body: Option<Value>,
) -> Result<Value, ApiError> {
    let method = reqwest::Method::from_bytes(method.to_uppercase().as_bytes())
        .map_err(|_| ApiError::transport("Unsupported method"))?;
    if method != reqwest::Method::GET
        && method != reqwest::Method::POST
        && method != reqwest::Method::DELETE
    {
        return Err(ApiError::transport("Unsupported method"));
    }
    if bridge.is_local() {
        // Local has no approval surface. Refuse a decision explicitly rather
        // than smuggling it into the LocalTransport allowlist.
        #[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
        if is_local_approval_decision(&method, &path) {
            return Err(ApiError::local_approvals_unavailable());
        }
        #[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
        {
            return bridge.local_request(method, &path, body).await;
        }
        #[cfg(not(all(any(test, feature = "local-dev"), target_os = "linux")))]
        {
            return Err(ApiError::transport("Unsupported bridge request"));
        }
    }
    check_api_route(&method, &path)?;
    bridge.hosted_request(&app, method, &path, body).await
}

/// Open one SSE subscription. Frames arrive as `chat-event`.
#[tauri::command]
pub async fn bridge_open_stream(
    app: tauri::AppHandle,
    bridge: State<'_, Bridge>,
    thread_id: String,
    after: u64,
) -> Result<(), ApiError> {
    if !is_id_segment(&thread_id) {
        return Err(ApiError::transport("Unsupported bridge request"));
    }
    if bridge.is_local() {
        #[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
        {
            return bridge.open_local_stream(&app, thread_id, after).await;
        }
        #[cfg(not(all(any(test, feature = "local-dev"), target_os = "linux")))]
        {
            return Err(ApiError::transport("Unsupported bridge request"));
        }
    }
    bridge.cancel_stream(&thread_id);

    let (cancel_tx, mut cancel_rx) = tokio::sync::watch::channel(false);
    if let Ok(mut streams) = bridge.streams.lock() {
        streams.insert(thread_id.clone(), cancel_tx);
    }

    let hosted = bridge
        .hosted()
        .ok_or_else(|| ApiError::transport("Unsupported bridge request"))?;
    let client = hosted.client.clone();
    let url = format!(
        "{}/api/threads/{}/stream?after={}",
        hosted.server,
        encode_path_segment(&thread_id),
        after
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

        let _ = handle.emit(
            "chat-stream-state",
            json!({ "threadId": id, "state": "open" }),
        );

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

        let _ = handle.emit(
            "chat-stream-state",
            json!({ "threadId": id, "state": "closed" }),
        );
    });

    Ok(())
}

#[tauri::command]
pub fn bridge_close_stream(bridge: State<'_, Bridge>, thread_id: String) {
    if bridge.is_local() {
        #[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
        bridge.close_local_stream(&thread_id);
        return;
    }
    bridge.cancel_stream(&thread_id);
}

/// Turn one SSE frame into a `chat-event`. Comments (heartbeats) carry no data
/// and are dropped.
fn forward_frame(app: &tauri::AppHandle, thread_id: &str, frame: &str) {
    if let Some(event) = frame_event(thread_id, frame) {
        let _ = app.emit("chat-event", event);
    }
}

/// Parse one SSE frame into the shared `chat-event` payload. Local callers use
/// this directly so the emission can be guarded by stream ownership.
fn frame_event(thread_id: &str, frame: &str) -> Option<Value> {
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
        return None;
    }

    let parsed: Value = serde_json::from_str(&data).unwrap_or(Value::Null);
    let kind = event.or_else(|| {
        parsed
            .get("type")
            .and_then(Value::as_str)
            .map(str::to_string)
    });

    Some(json!({
        "threadId": thread_id,
        "id": id,
        "event": kind,
        "data": parsed,
    }))
}

#[cfg(all(test, target_os = "linux"))]
mod tests;

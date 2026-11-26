//! Headless tests for backend selection, Local configuration, and the owned
//! child lifecycle. Selection/configuration tests pass explicit values, so no
//! test mutates process environment. The process tests use a disposable
//! synthetic sidecar or the opt-in installed wheel.

use super::*;

use std::collections::BTreeSet;
use std::ffi::OsStr;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use futures_util::StreamExt;
use reqwest::Method;
use serde_json::{json, Value};

const PROVIDER_URL: &str = "http://127.0.0.1:9/v1";

static TEMP_COUNTER: AtomicUsize = AtomicUsize::new(0);

struct TempDir(PathBuf);

impl TempDir {
    fn new() -> Self {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "kelp-local-runtime-{}-{}-{}",
            std::process::id(),
            TEMP_COUNTER.fetch_add(1, Ordering::Relaxed),
            nanos
        ));
        std::fs::DirBuilder::new()
            .mode(0o700)
            .create(&path)
            .expect("create private temp dir");
        Self(path)
    }

    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn trusted_python() -> Option<PathBuf> {
    if let Some(value) = std::env::var_os("KELP_TEST_SYSTEM_PYTHON") {
        let path = PathBuf::from(value);
        if path.is_absolute() && path.is_file() {
            return Some(path);
        }
    }
    ["/usr/bin/python3", "/usr/local/bin/python3", "/bin/python3"]
        .into_iter()
        .map(PathBuf::from)
        .find(|path| path.is_file())
}

/// Direct children of the current test thread. The sidecar is spawned from
/// this thread; a reaped child disappears from this file.
fn child_pids() -> BTreeSet<i32> {
    std::fs::read_to_string("/proc/thread-self/children")
        .unwrap_or_default()
        .split_whitespace()
        .filter_map(|pid| pid.parse().ok())
        .collect()
}

const SELECT_CHILD: &str = "KELP_LOCAL_RUNTIME_SELECT_CHILD";
const SELECT_EXPECT: &str = "KELP_LOCAL_RUNTIME_SELECT_EXPECT";

#[test]
fn environment_selection_uses_the_explicit_literal() {
    if std::env::var_os(SELECT_CHILD).is_some() {
        let expected = std::env::var(SELECT_EXPECT).expect("child expectation");
        let actual = select_backend();
        match expected.as_str() {
            "hosted" => assert_eq!(actual, Ok(DesktopBackend::Hosted)),
            "local" => assert_eq!(actual, Ok(DesktopBackend::Local)),
            "local-unsupported" => assert_eq!(actual, Err(RuntimeError::LocalUnsupported)),
            "unknown" => assert_eq!(actual, Err(RuntimeError::UnknownBackend)),
            other => panic!("unknown child expectation {other}"),
        }
        return;
    }

    let exe = std::env::current_exe().expect("current test binary");
    let run = |backend: Option<&str>, expected: &str| {
        let mut command = std::process::Command::new(&exe);
        command.args([
            "--exact",
            "local_runtime::tests::environment_selection_uses_the_explicit_literal",
            "--nocapture",
        ]);
        command.env(SELECT_CHILD, "1").env(SELECT_EXPECT, expected);
        match backend {
            Some(value) => {
                command.env("KELP_DESKTOP_BACKEND", value);
            }
            None => {
                command.env_remove("KELP_DESKTOP_BACKEND");
            }
        }
        let output = command.output().expect("spawn selection child");
        assert!(
            output.status.success(),
            "selection child failed for {backend:?}\nstdout:\n{}\nstderr:\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
    };

    run(None, "hosted");
    run(Some("hosted"), "hosted");
    run(Some(""), "unknown");
    run(Some("Hosted"), "unknown");
    run(Some("staging"), "unknown");
    let local_expected = if cfg!(all(feature = "local-dev", target_os = "linux")) {
        "local"
    } else {
        "local-unsupported"
    };
    run(Some("local"), local_expected);
}

#[test]
fn backend_selection_is_exact_and_fails_closed() {
    assert_eq!(parse_backend(None), Ok(BackendKind::Hosted));
    assert_eq!(
        parse_backend(Some(OsStr::new("hosted"))),
        Ok(BackendKind::Hosted)
    );
    assert_eq!(
        parse_backend(Some(OsStr::new("local"))),
        Ok(BackendKind::Local)
    );

    for value in ["", "Hosted", "LOCAL", " hosted", "local ", "true", "1"] {
        assert_eq!(
            parse_backend(Some(OsStr::new(value))),
            Err(RuntimeError::UnknownBackend),
            "unexpectedly accepted {value:?}"
        );
    }
    assert_eq!(
        parse_backend(Some(OsStr::from_bytes(b"\xff"))),
        Err(RuntimeError::UnknownBackend)
    );

    assert_eq!(
        resolve_backend(BackendKind::Hosted, false),
        Ok(DesktopBackend::Hosted)
    );
    assert_eq!(
        resolve_backend(BackendKind::Hosted, true),
        Ok(DesktopBackend::Hosted)
    );
    assert_eq!(
        resolve_backend(BackendKind::Local, false),
        Err(RuntimeError::LocalUnsupported)
    );
    assert_eq!(
        resolve_backend(BackendKind::Local, true),
        Ok(DesktopBackend::Local)
    );
    assert_eq!(DesktopBackend::Hosted.name(), "hosted");
    assert_eq!(DesktopBackend::Local.name(), "local");

    for error in [
        RuntimeError::UnknownBackend,
        RuntimeError::LocalUnsupported,
        RuntimeError::MissingPython,
        RuntimeError::InvalidStateDir,
        RuntimeError::MissingProviderBaseUrl,
        RuntimeError::InvalidProviderBaseUrl,
        RuntimeError::GuardProbe,
        RuntimeError::SidecarStart,
    ] {
        let rendered = error.to_string();
        assert!(!rendered.contains(PROVIDER_URL));
        assert!(!rendered.contains("Bearer"));
        assert!(!rendered.contains("local-fixture"));
    }
}

#[test]
fn provider_base_url_is_the_canonical_loopback_v1_fixture() {
    for valid in [
        "http://127.0.0.1:1/v1",
        "http://127.0.0.1:9/v1",
        "http://127.0.0.1:8000/v1",
        "http://127.0.0.1:65535/v1",
    ] {
        assert_eq!(
            validate_provider_base_url(valid),
            Ok(()),
            "unexpectedly rejected {valid}"
        );
    }

    for invalid in [
        "",
        "https://127.0.0.1:9000/v1",
        "http://localhost:9000/v1",
        "http://127.0.0.2:9000/v1",
        "http://0.0.0.0:9000/v1",
        "http://127.0.0.1/v1",
        "http://127.0.0.1:0/v1",
        "http://127.0.0.1:65536/v1",
        "http://127.0.0.1:80/v1",
        "http://127.0.0.1:9000/v2",
        "http://127.0.0.1:9000/v1/",
        "http://127.0.0.1:9000//v1",
        "http://127.0.0.1:9000/v1?x=1",
        "http://127.0.0.1:9000/v1#fragment",
        "http://user@127.0.0.1:9000/v1",
        "http://user:pass@127.0.0.1:9000/v1",
        "http://127.1:9000/v1",
        "HTTP://127.0.0.1:9000/v1",
        "http://127.0.0.1:09000/v1",
        "http://127.0.0.1:9000/v1 ",
        " http://127.0.0.1:9000/v1",
        "http://127.0.0.1:9000/%76%31",
        "http://127.0.0.1:9000/v1/../v1",
        "http://[::1]:9000/v1",
        "ftp://127.0.0.1:9000/v1",
    ] {
        assert_eq!(
            validate_provider_base_url(invalid),
            Err(RuntimeError::InvalidProviderBaseUrl),
            "unexpectedly accepted {invalid:?}"
        );
    }
}

#[test]
fn config_requires_explicit_absolute_values_and_optional_model() {
    let python = OsStr::new("/opt/kelp/venv/bin/python");
    let state = OsStr::new("/tmp/kelp-local-runtime-state");
    let provider = OsStr::new(PROVIDER_URL);

    assert_eq!(
        LocalConfig::from_values(None, Some(state), Some(provider), None).unwrap_err(),
        RuntimeError::MissingPython
    );
    assert_eq!(
        LocalConfig::from_values(Some(OsStr::new("")), Some(state), Some(provider), None)
            .unwrap_err(),
        RuntimeError::MissingPython
    );
    assert_eq!(
        LocalConfig::from_values(
            Some(OsStr::new("venv/bin/python")),
            Some(state),
            Some(provider),
            None
        )
        .unwrap_err(),
        RuntimeError::InvalidPython
    );
    assert_eq!(
        LocalConfig::from_values(Some(python), None, Some(provider), None).unwrap_err(),
        RuntimeError::MissingStateDir
    );
    for invalid in [
        OsStr::new("state"),
        OsStr::new("/"),
        OsStr::new("/tmp/one/../two"),
    ] {
        assert_eq!(
            LocalConfig::from_values(Some(python), Some(invalid), Some(provider), None)
                .unwrap_err(),
            RuntimeError::InvalidStateDir,
            "unexpectedly accepted state {invalid:?}"
        );
    }
    assert_eq!(
        LocalConfig::from_values(Some(python), Some(state), None, None).unwrap_err(),
        RuntimeError::MissingProviderBaseUrl
    );
    assert_eq!(
        LocalConfig::from_values(
            Some(python),
            Some(state),
            Some(OsStr::new("http://localhost:9/v1")),
            None
        )
        .unwrap_err(),
        RuntimeError::InvalidProviderBaseUrl
    );

    let config = LocalConfig::from_values(
        Some(python),
        Some(state),
        Some(provider),
        Some(OsStr::new("kelp-test-model")),
    )
    .expect("valid config");
    assert_eq!(config.primary_model.as_deref(), Some("kelp-test-model"));

    let blank = LocalConfig::from_values(
        Some(python),
        Some(state),
        Some(provider),
        Some(OsStr::new("   ")),
    )
    .expect("blank model is absent");
    assert_eq!(blank.primary_model, None);

    for invalid in [" model", "model ", "model\n", "m\u{7f}"] {
        assert_eq!(
            LocalConfig::from_values(
                Some(python),
                Some(state),
                Some(provider),
                Some(OsStr::new(invalid))
            )
            .unwrap_err(),
            RuntimeError::InvalidPrimaryModel,
            "unexpectedly accepted model {invalid:?}"
        );
    }
    let long = "m".repeat(201);
    assert_eq!(
        LocalConfig::from_values(
            Some(python),
            Some(state),
            Some(provider),
            Some(OsStr::new(&long))
        )
        .unwrap_err(),
        RuntimeError::InvalidPrimaryModel
    );
}

#[test]
fn supervisor_settings_use_the_fixed_fixture_key_and_explicit_values() {
    let config = LocalConfig::from_values(
        Some(OsStr::new("/opt/kelp/venv/bin/python")),
        Some(OsStr::new("/tmp/kelp-local-runtime-state")),
        Some(OsStr::new(PROVIDER_URL)),
        Some(OsStr::new("kelp-test-model")),
    )
    .expect("valid config");
    let settings = config.settings();
    assert_eq!(settings.provider_api_key, LOCAL_FIXTURE_PROVIDER_KEY);
    assert_eq!(settings.provider_base_url, Some(PROVIDER_URL));
    assert_eq!(settings.primary_model, Some("kelp-test-model"));
    assert_eq!(settings.python, config.python.as_path());
    assert_eq!(settings.state_dir, config.state_dir.as_path());
    assert!(!LOCAL_FIXTURE_PROVIDER_KEY.is_empty());
}

/// A synthetic sidecar that reports readiness but exposes an unguarded HTTP
/// server. The guard probe must refuse it, and the owned child must be reaped
/// before `LocalSession::start` returns.
const BROKEN_GUARD_FIXTURE: &str = r#"
import json
import os
import struct
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def read_exact(size):
    chunks = []
    while size:
        chunk = os.read(0, size)
        if not chunk:
            raise EOFError
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


header = read_exact(4)
size = struct.unpack(">I", header)[0]
if size <= 0 or size > 1_000_000:
    raise SystemExit(3)
config = json.loads(read_exact(size))
nonce = config["nonce"]

server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()

payload = json.dumps({
    "v": 1,
    "type": "ready",
    "nonce": nonce,
    "host": "127.0.0.1",
    "port": server.server_port,
}, separators=(",", ":")).encode("utf-8")
os.write(0, struct.pack(">I", len(payload)) + payload)
with open("started", "w", encoding="ascii") as marker:
    marker.write(str(os.getpid()))

while True:
    if not os.read(0, 4096):
        break
"#;

#[test]
fn broken_guard_is_refused_and_the_owned_child_is_reaped() {
    let Some(python) = trusted_python() else {
        eprintln!("skipping local-runtime fixture test: no trusted absolute Python");
        return;
    };
    let dir = TempDir::new();
    let fixture = dir.path().join("fixture.py");
    std::fs::write(&fixture, BROKEN_GUARD_FIXTURE).expect("write fixture");
    let fake_python = dir.path().join("fake-python");
    std::fs::write(
        &fake_python,
        format!(
            "#!/bin/sh\nexec '{}' '{}' \"$@\"\n",
            python.display(),
            fixture.display()
        ),
    )
    .expect("write fake interpreter");
    std::fs::set_permissions(&fake_python, std::fs::Permissions::from_mode(0o700))
        .expect("make fake interpreter executable");

    let state = dir.path().join("state");
    let mut config = LocalConfig::from_values(
        Some(fake_python.as_os_str()),
        Some(state.as_os_str()),
        Some(OsStr::new(PROVIDER_URL)),
        None,
    )
    .expect("valid fixture config");
    config.handshake_timeout = Duration::from_secs(3);
    config.shutdown_timeout = Duration::from_millis(500);

    let before = child_pids();
    let error = match LocalSession::start(&config) {
        Ok(_) => panic!("unguarded fixture unexpectedly passed the guard"),
        Err(error) => error,
    };
    assert_eq!(error, RuntimeError::GuardProbe);
    assert!(
        state.join("started").is_file(),
        "fixture never reached readiness"
    );
    assert!(
        child_pids().difference(&before).next().is_none(),
        "owned sidecar survived guard failure: before {before:?} after {:?}",
        child_pids()
    );
}

#[test]
#[ignore = "requires KELP_TEST_UNGUARDED_PYTHON=/abs/venv/bin/python from a pre-activation commit"]
fn unguarded_control_interpreter_is_refused_and_reaped() {
    let value = std::env::var_os("KELP_TEST_UNGUARDED_PYTHON").unwrap_or_else(|| {
        panic!(
            "KELP_TEST_UNGUARDED_PYTHON must name an absolute pre-activation interpreter; \
             this ignored test fails closed instead of skipping"
        )
    });
    let python = PathBuf::from(value);
    assert!(
        python.is_absolute(),
        "KELP_TEST_UNGUARDED_PYTHON must be an absolute interpreter path"
    );
    assert!(
        python.is_file(),
        "KELP_TEST_UNGUARDED_PYTHON must point at an existing interpreter file"
    );

    let dir = TempDir::new();
    let state = dir.path().join("state");
    let mut config = LocalConfig::from_values(
        Some(python.as_os_str()),
        Some(state.as_os_str()),
        Some(OsStr::new(PROVIDER_URL)),
        None,
    )
    .expect("valid unguarded-control config");
    config.handshake_timeout = Duration::from_secs(90);
    config.shutdown_timeout = Duration::from_secs(5);

    let before = child_pids();
    let error = match LocalSession::start(&config) {
        Ok(_) => panic!("pre-activation sidecar unexpectedly passed the guard"),
        Err(error) => error,
    };
    assert_eq!(error, RuntimeError::GuardProbe);
    assert!(
        child_pids().difference(&before).next().is_none(),
        "unguarded control child survived: before {before:?} after {:?}",
        child_pids()
    );
}

const LOCAL_TURN_TEXT: &str = "rust-local-runtime-turn";
const LOCAL_PROVIDER_TEXT: &str = "synthetic-rust-local-text-9f3a";

#[derive(Clone, Debug)]
struct ProviderRequest {
    path: String,
    headers: Vec<(String, String)>,
    body: Value,
}

impl ProviderRequest {
    fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case(name))
            .map(|(_, value)| value.as_str())
    }
}

/// One private loopback OpenAI-compatible provider. It records the request and
/// returns a fixed streaming answer; no external endpoint or credential is
/// involved.
struct FakeProvider {
    port: u16,
    requests: Arc<Mutex<Vec<ProviderRequest>>>,
    stop: Arc<AtomicBool>,
    handle: Option<JoinHandle<()>>,
}

impl FakeProvider {
    fn start() -> Self {
        let listener = TcpListener::bind(("127.0.0.1", 0)).expect("bind fake provider");
        listener
            .set_nonblocking(true)
            .expect("nonblocking fake provider");
        let port = listener.local_addr().expect("provider address").port();
        let requests = Arc::new(Mutex::new(Vec::new()));
        let stop = Arc::new(AtomicBool::new(false));
        let accept_requests = Arc::clone(&requests);
        let accept_stop = Arc::clone(&stop);
        let handle = thread::spawn(move || {
            while !accept_stop.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok((stream, _)) => {
                        let requests = Arc::clone(&accept_requests);
                        thread::spawn(move || {
                            let _ = serve_provider(stream, &requests);
                        });
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(2));
                    }
                    Err(_) => break,
                }
            }
        });
        Self {
            port,
            requests,
            stop,
            handle: Some(handle),
        }
    }

    fn base_url(&self) -> String {
        format!("http://127.0.0.1:{}/v1", self.port)
    }

    fn requests(&self) -> Vec<ProviderRequest> {
        self.requests.lock().expect("provider requests").clone()
    }
}

impl Drop for FakeProvider {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(handle) = self.handle.take() {
            let _ = handle.join();
        }
    }
}

fn serve_provider(
    mut stream: TcpStream,
    requests: &Mutex<Vec<ProviderRequest>>,
) -> std::io::Result<()> {
    let (path, headers, body) = read_http_request(&mut stream).ok_or_else(|| {
        std::io::Error::new(std::io::ErrorKind::InvalidData, "invalid provider request")
    })?;
    let value: Value = serde_json::from_slice(&body).unwrap_or(Value::Null);
    requests
        .lock()
        .expect("provider requests")
        .push(ProviderRequest {
            path,
            headers,
            body: value,
        });

    let sse = provider_sse();
    write!(
        stream,
        "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        sse.len()
    )?;
    stream.write_all(&sse)?;
    stream.flush()
}

fn read_http_request(stream: &mut TcpStream) -> Option<(String, Vec<(String, String)>, Vec<u8>)> {
    stream.set_read_timeout(Some(Duration::from_secs(5))).ok()?;
    let mut buffer = Vec::new();
    let mut chunk = [0u8; 1024];
    let header_end;
    loop {
        let read = stream.read(&mut chunk).ok()?;
        if read == 0 {
            return None;
        }
        buffer.extend_from_slice(&chunk[..read]);
        if let Some(position) = buffer.windows(4).position(|window| window == b"\r\n\r\n") {
            header_end = position + 4;
            break;
        }
        if buffer.len() > 64 * 1024 {
            return None;
        }
    }

    let head = std::str::from_utf8(&buffer[..header_end - 4]).ok()?;
    let mut lines = head.split("\r\n");
    let request_line = lines.next()?;
    let mut words = request_line.split_whitespace();
    let _method = words.next()?;
    let path = words.next()?.to_string();
    let _version = words.next()?;
    let mut headers = Vec::new();
    let mut content_length = 0usize;
    for line in lines {
        let (name, value) = line.split_once(':')?;
        let value = value.trim();
        if name.eq_ignore_ascii_case("content-length") {
            content_length = value.parse().ok()?;
        }
        headers.push((name.to_string(), value.to_string()));
    }

    let mut body = buffer[header_end..].to_vec();
    while body.len() < content_length {
        let read = stream.read(&mut chunk).ok()?;
        if read == 0 {
            return None;
        }
        body.extend_from_slice(&chunk[..read]);
    }
    body.truncate(content_length);
    Some((path, headers, body))
}

fn provider_chunk(delta: Value, finish_reason: Option<&str>) -> String {
    json!({
        "id": "chatcmpl-rust-local",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "kelp-test-model",
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason,
        }],
    })
    .to_string()
}

fn provider_sse() -> Vec<u8> {
    let mut body = String::new();
    body.push_str(&format!(
        "data: {}\n\n",
        provider_chunk(json!({"role": "assistant", "content": ""}), None)
    ));
    body.push_str(&format!(
        "data: {}\n\n",
        provider_chunk(json!({"content": "synthetic-rust-local-"}), None)
    ));
    body.push_str(&format!(
        "data: {}\n\n",
        provider_chunk(json!({"content": "text-9f3a"}), None)
    ));
    body.push_str(&format!(
        "data: {}\n\n",
        provider_chunk(json!({}), Some("stop"))
    ));
    body.push_str(&format!(
        "data: {}\n\n",
        json!({
            "choices": [],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        })
    ));
    body.push_str("data: [DONE]\n\n");
    body.into_bytes()
}

/// Read the replayed Local SSE until the turn terminates, then return every
/// parsed data object.
async fn collect_replay(transport: &LocalTransport, thread_id: &str) -> Vec<Value> {
    let (_cancel_tx, mut cancel_rx) = tokio::sync::watch::channel(false);
    let response = transport
        .open_stream(thread_id, 0, &mut cancel_rx)
        .await
        .expect("open replay stream");
    assert_eq!(response.status(), 200);

    let mut body = response.bytes_stream();
    let mut buffer = String::new();
    let mut events = Vec::new();
    let deadline = tokio::time::Instant::now() + Duration::from_secs(30);
    while let Ok(Some(chunk)) = tokio::time::timeout_at(deadline, body.next()).await {
        let chunk = chunk.expect("replay chunk");
        buffer.push_str(&String::from_utf8_lossy(&chunk));
        while let Some(index) = buffer.find("\n\n") {
            let frame: String = buffer.drain(..index + 2).collect();
            let mut data = String::new();
            for line in frame.lines() {
                if let Some(rest) = line.strip_prefix("data:") {
                    if !data.is_empty() {
                        data.push('\n');
                    }
                    data.push_str(rest.trim_start());
                }
            }
            if data.trim().is_empty() || data.trim() == "[DONE]" {
                continue;
            }
            if let Ok(value) = serde_json::from_str::<Value>(&data) {
                let kind = value
                    .get("type")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_owned();
                events.push(value);
                if matches!(kind.as_str(), "turn_finished" | "turn_failed" | "error") {
                    return events;
                }
            }
        }
    }
    events
}

#[test]
#[ignore = "requires KELP_TEST_INSTALLED_PYTHON=/abs/venv/bin/python with an installed wheel"]
fn installed_wheel_local_runtime_lifecycle() {
    let value = std::env::var_os("KELP_TEST_INSTALLED_PYTHON").unwrap_or_else(|| {
        panic!(
            "KELP_TEST_INSTALLED_PYTHON must name an absolute installed-wheel interpreter; \
             this ignored test fails closed instead of skipping"
        )
    });
    let python = PathBuf::from(value);
    assert!(
        python.is_absolute(),
        "KELP_TEST_INSTALLED_PYTHON must be an absolute interpreter path"
    );
    assert!(
        python.is_file(),
        "KELP_TEST_INSTALLED_PYTHON must point at an existing interpreter file"
    );

    let dir = TempDir::new();
    let state = dir.path().join("state");
    let provider = FakeProvider::start();
    let provider_url = provider.base_url();
    let mut config = LocalConfig::from_values(
        Some(python.as_os_str()),
        Some(state.as_os_str()),
        Some(OsStr::new(&provider_url)),
        Some(OsStr::new("kelp-test-model")),
    )
    .expect("valid installed-wheel config");
    config.handshake_timeout = Duration::from_secs(90);
    config.shutdown_timeout = Duration::from_secs(5);

    let before = child_pids();
    let session = match LocalSession::start(&config) {
        Ok(session) => session,
        Err(error) => panic!("installed sidecar did not become ready: {error:?}"),
    };
    let spawned: Vec<i32> = child_pids().difference(&before).copied().collect();
    assert_eq!(
        spawned.len(),
        1,
        "expected exactly one owned child, before {before:?} after {:?}",
        child_pids()
    );
    let child = spawned[0];

    let threads = tauri::async_runtime::block_on(session.transport.send_api(
        Method::GET,
        "/api/threads?limit=5",
        None,
    ))
    .expect("thread list");
    assert_eq!(threads.status(), 200);
    let value: Value = serde_json::from_slice(threads.body()).expect("thread list json");
    assert!(value.get("threads").is_some());

    let created = tauri::async_runtime::block_on(session.transport.send_api(
        Method::POST,
        "/api/threads",
        Some(json!({"title": "rust-local-runtime"})),
    ))
    .expect("thread create");
    assert!(
        created.status() == 200 || created.status() == 201,
        "unexpected create status {}",
        created.status()
    );
    let value: Value = serde_json::from_slice(created.body()).expect("thread create json");
    let id = value["thread"]["id"]
        .as_str()
        .expect("created thread id")
        .to_owned();

    let detail = tauri::async_runtime::block_on(session.transport.send_api(
        Method::GET,
        &format!("/api/threads/{id}"),
        None,
    ))
    .expect("thread detail");
    assert_eq!(detail.status(), 200);

    let turn = tauri::async_runtime::block_on(session.transport.send_api(
        Method::POST,
        &format!("/api/threads/{id}/turns"),
        Some(json!({
            "client_turn_id": "ct-rust-local-runtime",
            "text": LOCAL_TURN_TEXT,
        })),
    ))
    .expect("send synthetic turn");
    assert!(
        turn.status() == 200 || turn.status() == 202,
        "unexpected turn status {}",
        turn.status()
    );
    let turn_value: Value = serde_json::from_slice(turn.body()).expect("turn json");
    assert!(
        turn_value.get("turn_id").is_some(),
        "turn response had no turn_id: {turn_value}"
    );

    let events = tauri::async_runtime::block_on(collect_replay(&session.transport, &id));
    let text: String = events
        .iter()
        .filter(|event| event.get("type").and_then(Value::as_str) == Some("token_delta"))
        .filter_map(|event| event.get("text").and_then(Value::as_str))
        .collect();
    assert_eq!(text, LOCAL_PROVIDER_TEXT, "replayed token text: {events:?}");
    assert!(
        events
            .iter()
            .any(|event| event.get("type").and_then(Value::as_str) == Some("turn_finished")),
        "missing turn_finished in replay: {events:?}"
    );
    for event in &events {
        let kind = event
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or_default();
        assert!(
            !matches!(kind, "tool_call_started" | "tool_call_finished"),
            "Local replay contained a tool event: {event}"
        );
    }

    let requests = provider.requests();
    let wire = requests
        .iter()
        .find(|request| {
            request.path == "/v1/chat/completions"
                && request.body["messages"]
                    .as_array()
                    .map(|messages| {
                        messages.iter().any(|message| {
                            message["role"] == "user" && message["content"] == LOCAL_TURN_TEXT
                        })
                    })
                    .unwrap_or(false)
        })
        .unwrap_or_else(|| panic!("provider never saw the turn: {requests:?}"));
    let expected_auth = format!("Bearer {LOCAL_FIXTURE_PROVIDER_KEY}");
    assert_eq!(wire.header("authorization"), Some(expected_auth.as_str()));
    assert_eq!(wire.body["stream"], true);
    assert_eq!(wire.body["model"], "kelp-test-model");
    assert!(wire.body.get("tools").is_none(), "tools reached the wire");
    assert!(
        wire.body.get("tool_choice").is_none(),
        "tool_choice reached the wire"
    );
    for request in &requests {
        assert!(request.body.get("tools").is_none());
        assert!(request.body.get("tool_choice").is_none());
    }

    let outcome = session.runtime.shutdown().expect("first shutdown");
    assert!(outcome.clean_exit(), "unclean sidecar exit: {outcome:?}");
    assert!(
        !Path::new(&format!("/proc/{child}")).exists(),
        "sidecar child {child} was not reaped"
    );
    assert!(
        session.runtime.shutdown().is_none(),
        "shutdown must be idempotent"
    );
}

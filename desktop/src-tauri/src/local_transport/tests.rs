//! Socket-level tests for the loopback-local transport.
//!
//! Every listener binds `127.0.0.1:0`. No test reaches a hosted service, a
//! provider, or the Python fixture owned by another slice. The server records
//! the exact request bytes it received so header policy can be asserted
//! independently of reqwest internals.

use super::*;

use std::io::{Read, Write};
use std::net::{Shutdown, TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use reqwest::Method;
use serde_json::{json, Value};

const OWNER: &str = "owner-token-0123456789abcdef";
const CSRF: &str = "csrf-token-0123456789abcdef";
const PROXY_CHILD: &str = "KELP_LOCAL_TRANSPORT_PROXY_CHILD";

#[derive(Clone, Debug)]
struct Recorded {
    method: String,
    target: String,
    headers: Vec<(String, String)>,
    body: Vec<u8>,
}

impl Recorded {
    fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case(name))
            .map(|(_, value)| value.as_str())
    }

    fn header_all(&self, name: &str) -> Vec<&str> {
        self.headers
            .iter()
            .filter(|(key, _)| key.eq_ignore_ascii_case(name))
            .map(|(_, value)| value.as_str())
            .collect()
    }
}

struct TestReply {
    status: u16,
    headers: Vec<(String, String)>,
    body: Vec<u8>,
}

impl TestReply {
    fn json(status: u16, body: Value) -> Self {
        Self {
            status,
            headers: vec![("content-type".into(), "application/json".into())],
            body: serde_json::to_vec(&body).expect("serialize test reply"),
        }
    }
}

struct TestServer {
    port: NonZeroU16,
    requests: Arc<Mutex<Vec<Recorded>>>,
    stop: Arc<AtomicBool>,
    handle: Option<JoinHandle<()>>,
}

impl TestServer {
    fn start<F>(handler: F) -> Self
    where
        F: Fn(&Recorded) -> TestReply + Send + Sync + 'static,
    {
        let listener = TcpListener::bind((LOOPBACK_HOST, 0)).expect("bind loopback");
        listener
            .set_nonblocking(true)
            .expect("nonblocking listener");
        let port = listener.local_addr().expect("listener address").port();
        let requests = Arc::new(Mutex::new(Vec::new()));
        let stop = Arc::new(AtomicBool::new(false));
        let handler: Arc<dyn Fn(&Recorded) -> TestReply + Send + Sync> = Arc::new(handler);

        let accept_requests = Arc::clone(&requests);
        let accept_stop = Arc::clone(&stop);
        let handle = thread::spawn(move || {
            while !accept_stop.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok((stream, _)) => {
                        let handler = Arc::clone(&handler);
                        let requests = Arc::clone(&accept_requests);
                        thread::spawn(move || serve_one(stream, handler.as_ref(), &requests));
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(2));
                    }
                    Err(_) => break,
                }
            }
        });

        Self {
            port: NonZeroU16::new(port).expect("nonzero listener port"),
            requests,
            stop,
            handle: Some(handle),
        }
    }

    fn transport(&self) -> LocalTransport {
        LocalTransport::new(self.port, OWNER, CSRF).expect("valid test credentials")
    }

    fn requests(&self) -> Vec<Recorded> {
        self.requests.lock().expect("request log").clone()
    }
}

impl Drop for TestServer {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(handle) = self.handle.take() {
            let _ = handle.join();
        }
    }
}

fn serve_one(
    mut stream: TcpStream,
    handler: &dyn Fn(&Recorded) -> TestReply,
    requests: &Mutex<Vec<Recorded>>,
) {
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(2)));
    let Some(request) = read_request(&mut stream) else {
        return;
    };
    let reply = handler(&request);
    requests.lock().expect("request log").push(request);
    let _ = write_reply(&mut stream, &reply);
    let _ = stream.shutdown(Shutdown::Both);
}

fn read_request(stream: &mut TcpStream) -> Option<Recorded> {
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
        if buffer.len() > 32 * 1024 {
            return None;
        }
    }

    let head = std::str::from_utf8(&buffer[..header_end - 4]).ok()?;
    let mut lines = head.split("\r\n");
    let request_line = lines.next()?;
    let mut words = request_line.split_whitespace();
    let method = words.next()?.to_string();
    let target = words.next()?.to_string();
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

    Some(Recorded {
        method,
        target,
        headers,
        body,
    })
}

fn write_reply(stream: &mut TcpStream, reply: &TestReply) -> std::io::Result<()> {
    let reason = match reply.status {
        200 => "OK",
        201 => "Created",
        302 => "Found",
        401 => "Unauthorized",
        403 => "Forbidden",
        404 => "Not Found",
        _ => "OK",
    };
    write!(
        stream,
        "HTTP/1.1 {} {}\r\nContent-Length: {}\r\nConnection: close\r\n",
        reply.status,
        reason,
        reply.body.len()
    )?;
    for (name, value) in &reply.headers {
        write!(stream, "{name}: {value}\r\n")?;
    }
    write!(stream, "\r\n")?;
    stream.write_all(&reply.body)?;
    stream.flush()
}

fn expected_origin(server: &TestServer) -> String {
    format!("http://127.0.0.1:{}", server.port)
}

fn assert_single(record: &Recorded, name: &str, expected: &str) {
    let values = record.header_all(name);
    assert_eq!(
        values.len(),
        1,
        "expected exactly one {name} header, got {:?}",
        record.headers
    );
    assert_eq!(values[0], expected);
}

#[tokio::test]
async fn reads_carry_origin_and_bearer_only() {
    let server = TestServer::start(|_| TestReply::json(200, json!({"ok": true})));
    let transport = server.transport();

    let response = transport
        .api_request(Method::GET, "/api/threads?limit=25", None)
        .expect("allowlisted list")
        .send()
        .await
        .expect("loopback response");
    assert_eq!(response.status(), 200);
    let response = transport
        .api_request(Method::GET, "/api/threads/thread_1", None)
        .expect("allowlisted detail")
        .send()
        .await
        .expect("loopback response");
    assert_eq!(response.status(), 200);

    let requests = server.requests();
    assert_eq!(requests.len(), 2);
    assert_eq!(requests[0].method, "GET");
    assert_eq!(requests[0].target, "/api/threads?limit=25");
    assert_eq!(requests[1].target, "/api/threads/thread_1");
    let origin = expected_origin(&server);
    let bearer = format!("Bearer {OWNER}");
    for request in &requests {
        assert_single(request, "origin", &origin);
        assert_single(request, "authorization", &bearer);
        assert!(request.header("x-csrf-token").is_none());
        assert!(request.header("cookie").is_none());
    }
}

#[tokio::test]
async fn mutations_carry_csrf_and_json_only_where_allowed() {
    let server = TestServer::start(|_| TestReply::json(201, json!({"ok": true})));
    let transport = server.transport();

    transport
        .api_request(
            Method::POST,
            "/api/threads",
            Some(json!({"title": "hello"})),
        )
        .expect("allowlisted create")
        .send()
        .await
        .expect("loopback response");
    transport
        .api_request(
            Method::POST,
            "/api/threads/thread_1/turns",
            Some(json!({"client_turn_id": "ct-1", "text": "hi"})),
        )
        .expect("allowlisted send")
        .send()
        .await
        .expect("loopback response");
    transport
        .api_request(Method::POST, "/api/turns/turn_1/stop", None)
        .expect("allowlisted stop")
        .send()
        .await
        .expect("loopback response");
    transport
        .api_request(Method::POST, "/api/turns/turn_1/retry", None)
        .expect("allowlisted retry")
        .send()
        .await
        .expect("loopback response");

    let requests = server.requests();
    assert_eq!(requests.len(), 4);
    let origin = expected_origin(&server);
    let bearer = format!("Bearer {OWNER}");
    for request in &requests {
        assert_eq!(request.method, "POST");
        assert_single(request, "origin", &origin);
        assert_single(request, "authorization", &bearer);
        assert_single(request, "x-csrf-token", CSRF);
        assert!(request.header("cookie").is_none());
    }

    let created: Value = serde_json::from_slice(&requests[0].body).expect("json create body");
    assert_eq!(created["title"], "hello");
    let sent: Value = serde_json::from_slice(&requests[1].body).expect("json send body");
    assert_eq!(sent["text"], "hi");
    assert!(requests[0]
        .header("content-type")
        .expect("json content type")
        .starts_with("application/json"));
    assert!(requests[1]
        .header("content-type")
        .expect("json content type")
        .starts_with("application/json"));
    assert!(requests[2].body.is_empty());
    assert!(requests[3].body.is_empty());
}

#[tokio::test]
async fn stream_uses_validated_thread_id_and_after() {
    let server = TestServer::start(|_| TestReply {
        status: 200,
        headers: vec![("content-type".into(), "text/event-stream".into())],
        body: Vec::new(),
    });
    let transport = server.transport();

    transport
        .stream_request("thread_1", 42)
        .expect("allowlisted stream")
        .send()
        .await
        .expect("loopback response");
    transport
        .stream_request("a-b_C9", 0)
        .expect("allowlisted stream")
        .send()
        .await
        .expect("loopback response");

    let requests = server.requests();
    assert_eq!(requests.len(), 2);
    assert_eq!(requests[0].target, "/api/threads/thread_1/stream?after=42");
    assert_eq!(requests[1].target, "/api/threads/a-b_C9/stream?after=0");
    let origin = expected_origin(&server);
    let bearer = format!("Bearer {OWNER}");
    for request in &requests {
        assert_eq!(request.method, "GET");
        assert_single(request, "origin", &origin);
        assert_single(request, "authorization", &bearer);
        assert_single(request, "accept", "text/event-stream");
        assert!(request.header("x-csrf-token").is_none());
        assert!(request.header("cookie").is_none());
    }
}

#[tokio::test]
async fn send_api_returns_a_bounded_reply() {
    let server = TestServer::start(|_| TestReply::json(200, json!({"ok": true})));
    let transport = server.transport();

    let response = transport
        .send_api(Method::GET, "/api/threads?limit=5", None)
        .await
        .expect("loopback reply");
    assert_eq!(response.status(), 200);
    let value: Value = serde_json::from_slice(response.body()).expect("json body");
    assert_eq!(value["ok"], true);
}

#[tokio::test]
async fn rest_replies_over_the_body_bound_are_refused() {
    let server = TestServer::start(|_| TestReply {
        status: 200,
        headers: Vec::new(),
        body: vec![b'x'; REST_BODY_LIMIT + 1],
    });
    let transport = server.transport();
    let error = transport
        .send_api(Method::GET, "/api/threads", None)
        .await
        .expect_err("oversize REST body must fail");
    assert_eq!(error, LocalTransportError::RequestFailed);
    assert!(!error.to_string().contains(&expected_origin(&server)));
}

#[tokio::test]
async fn stream_header_wait_is_bounded_for_a_hanging_server() {
    let server = TestServer::start(|_| {
        thread::sleep(Duration::from_millis(800));
        TestReply {
            status: 200,
            headers: vec![("content-type".into(), "text/event-stream".into())],
            body: Vec::new(),
        }
    });
    let mut transport = server.transport();
    transport.stream_header_timeout = Duration::from_millis(100);
    let (_cancel_tx, mut cancel_rx) = tokio::sync::watch::channel(false);

    let started = std::time::Instant::now();
    let error = match transport.open_stream("thread_1", 0, &mut cancel_rx).await {
        Ok(_) => panic!("hanging headers must not be accepted"),
        Err(error) => error,
    };
    assert_eq!(error, LocalTransportError::HeaderTimeout);
    assert!(
        started.elapsed() < Duration::from_millis(500),
        "header deadline did not bound the wait: {:?}",
        started.elapsed()
    );
}

#[tokio::test]
async fn stream_open_cancels_while_waiting_for_headers() {
    let server = TestServer::start(|_| {
        thread::sleep(Duration::from_millis(600));
        TestReply {
            status: 200,
            headers: vec![("content-type".into(), "text/event-stream".into())],
            body: Vec::new(),
        }
    });
    let transport = server.transport();
    let (cancel_tx, mut cancel_rx) = tokio::sync::watch::channel(false);
    let started = std::time::Instant::now();
    let canceller = thread::spawn(move || {
        thread::sleep(Duration::from_millis(60));
        let _ = cancel_tx.send(true);
    });

    let error = tokio::time::timeout(
        Duration::from_secs(3),
        transport.open_stream("thread_1", 0, &mut cancel_rx),
    )
    .await
    .expect("cancellation must not wait for headers")
    .expect_err("cancel before headers must refuse the stream");
    assert_eq!(error, LocalTransportError::Cancelled);
    assert!(started.elapsed() < Duration::from_millis(500));
    let _ = canceller.join();

    // The server eventually gets a request; the transport must not have sent
    // the authoritative headers twice or a body.
    thread::sleep(Duration::from_millis(50));
    assert!(server
        .requests()
        .iter()
        .all(|request| request.header("accept") == Some("text/event-stream")));
}

#[test]
fn invalid_profile_requests_fail_before_any_connection() {
    let listener = TcpListener::bind((LOOPBACK_HOST, 0)).expect("bind loopback");
    listener
        .set_nonblocking(true)
        .expect("nonblocking listener");
    let port = NonZeroU16::new(listener.local_addr().expect("listener address").port())
        .expect("nonzero port");
    let transport = LocalTransport::new(port, OWNER, CSRF).expect("valid credentials");

    let mut rejected: Vec<(Method, &str, Option<Value>)> = Vec::new();
    for path in [
        "",
        "/",
        "api/threads",
        "//api/threads",
        "/api/threads/",
        "/api/threads//thread_1",
        "/api/health",
        "/api/healthz",
        "/api/readyz",
        "/api/auth/devices",
        "/api/phone",
        "/api/wallet",
        "/api/approvals",
        "/api/threads/thread_1/stream",
        "/api/threads/thread_1/stream?after=1",
        "/api/threads?limit=1&after=2",
        "/api/threads?after=1",
        "/api/threads?limit=",
        "/api/threads?limit=-1",
        "/api/threads?limit=1=2",
        "/api/threads?limit=1?x=2",
        "/api/threads/thread_1?limit=1",
        "/api/threads/..",
        "/api/threads/%2E%2E",
        "/api/threads/a%2Fb",
        "/api/threads/a b",
        "/api/threads/a+b",
        "/api/turns/turn_1",
        "/api/turns/turn_1/stop",
        "/api/turns/turn_1/retry",
        "http://127.0.0.1/api/threads",
        "/api/threads#fragment",
        "/api/threads?limit=1#fragment",
    ] {
        rejected.push((Method::GET, path, None));
    }
    for path in [
        "/api/health",
        "/api/healthz",
        "/api/readyz",
        "/api/auth/devices",
        "/api/turns/turn_1",
        "/api/threads/thread_1",
        "/api/threads/thread_1/turns?x=1",
        "/api/turns/turn_1/stop?x=1",
        "/api/threads/thread_1/stream",
    ] {
        rejected.push((Method::POST, path, None));
    }
    rejected.push((Method::PATCH, "/api/threads", None));
    rejected.push((Method::DELETE, "/api/threads/thread_1", None));
    rejected.push((Method::GET, "/api/threads", Some(json!({}))));
    rejected.push((Method::GET, "/api/threads/thread_1", Some(Value::Null)));
    rejected.push((Method::POST, "/api/threads/thread_1/turns", None));
    rejected.push((Method::POST, "/api/turns/turn_1/stop", Some(json!({}))));
    rejected.push((Method::POST, "/api/turns/turn_1/retry", Some(json!({}))));

    for (method, path, body) in rejected {
        assert!(
            transport.api_request(method.clone(), path, body).is_err(),
            "unexpectedly allowed {method} {path}"
        );
    }

    let long_id = "a".repeat(MAX_ID_LEN + 1);
    for id in ["", "a/b", "../thread", "a.b", "a b", long_id.as_str()] {
        assert!(matches!(
            transport.stream_request(id, 0),
            Err(LocalTransportError::UnsupportedRequest)
        ));
    }

    thread::sleep(Duration::from_millis(50));
    assert!(matches!(
        listener.accept().expect_err("no connection").kind(),
        std::io::ErrorKind::WouldBlock
    ));
}

#[test]
fn credentials_are_validated_sensitive_and_never_displayed() {
    let port = NonZeroU16::new(9).expect("nonzero port");
    assert!(matches!(
        LocalTransport::new(port, "", CSRF),
        Err(LocalTransportError::InvalidOwnerCredential)
    ));
    assert!(matches!(
        LocalTransport::new(port, OWNER, ""),
        Err(LocalTransportError::InvalidCsrfCredential)
    ));
    assert!(matches!(
        LocalTransport::new(port, "bad\r\nInjected: 1", CSRF),
        Err(LocalTransportError::InvalidOwnerCredential)
    ));
    assert!(matches!(
        LocalTransport::new(port, "bad space", CSRF),
        Err(LocalTransportError::InvalidOwnerCredential)
    ));
    assert!(matches!(
        LocalTransport::new(port, "non-ascii-é", CSRF),
        Err(LocalTransportError::InvalidOwnerCredential)
    ));
    assert!(matches!(
        LocalTransport::new(port, &"a".repeat(CREDENTIAL_LIMIT + 1), CSRF),
        Err(LocalTransportError::InvalidOwnerCredential)
    ));

    let secret = "s3cr3t-owner-value";
    let error = match LocalTransport::new(port, &format!("{secret}\n"), CSRF) {
        Err(error) => error,
        Ok(_) => panic!("control character must be refused"),
    };
    assert_eq!(error.to_string(), "local owner credential is not usable");
    assert!(!error.to_string().contains(secret));
    assert!(!format!("{error:?}").contains(secret));

    let transport = LocalTransport::new(port, OWNER, CSRF).expect("valid credentials");
    assert!(transport.owner.is_sensitive());
    assert!(transport.csrf.is_sensitive());
    assert!(!transport.origin.is_sensitive());
    assert_eq!(
        transport.origin.to_str().expect("ascii origin"),
        "http://127.0.0.1:9"
    );
}

#[tokio::test]
async fn redirects_are_not_followed() {
    let server = TestServer::start(|_| TestReply {
        status: 302,
        headers: vec![("location".into(), "http://127.0.0.1:1/not-a-route".into())],
        body: Vec::new(),
    });
    let transport = server.transport();
    let response = transport
        .api_request(Method::GET, "/api/threads", None)
        .expect("allowlisted list")
        .send()
        .await
        .expect("302 is returned, not followed");
    assert_eq!(response.status(), 302);
    let requests = server.requests();
    assert_eq!(requests.len(), 1);
    assert_eq!(requests[0].target, "/api/threads");
}

#[tokio::test]
async fn server_cookies_are_not_replayed() {
    let server = TestServer::start(|_| TestReply {
        status: 200,
        headers: vec![("set-cookie".into(), "sid=should-not-return".into())],
        body: br#"{"ok":true}"#.to_vec(),
    });
    let transport = server.transport();
    for _ in 0..2 {
        transport
            .api_request(Method::GET, "/api/threads", None)
            .expect("allowlisted list")
            .send()
            .await
            .expect("loopback response");
    }
    let requests = server.requests();
    assert_eq!(requests.len(), 2);
    assert!(requests[0].header("cookie").is_none());
    assert!(requests[1].header("cookie").is_none());
}

#[tokio::test]
async fn guard_probe_accepts_the_committed_contract() {
    let server = TestServer::start(|request| {
        let has_origin = request.header("origin").is_some();
        let has_bearer = request.header("authorization").is_some();
        match (request.target.as_str(), has_origin, has_bearer) {
            ("/api/healthz", false, true) => TestReply::json(
                403,
                json!({"ok": false, "error": {"code": "invalid_origin"}}),
            ),
            ("/api/healthz", true, false) => {
                TestReply::json(401, json!({"ok": false, "error": {"code": "unauthorized"}}))
            }
            ("/api/health", true, true) => TestReply::json(200, json!({"ok": true})),
            ("/api/auth/devices", true, true) => {
                TestReply::json(404, json!({"ok": false, "error": {"code": "not_found"}}))
            }
            _ => TestReply::json(
                500,
                json!({"ok": false, "error": {"code": "unexpected_probe"}}),
            ),
        }
    });
    let transport = server.transport();
    transport
        .verify_guard()
        .await
        .expect("committed guard contract");

    let requests = server.requests();
    let targets: Vec<&str> = requests
        .iter()
        .map(|request| request.target.as_str())
        .collect();
    assert_eq!(
        targets,
        [
            "/api/healthz",
            "/api/healthz",
            "/api/health",
            "/api/auth/devices"
        ]
    );
    assert!(requests.iter().all(|request| request.method == "GET"));
    assert!(requests
        .iter()
        .all(|request| request.header("x-csrf-token").is_none()));
    assert!(requests
        .iter()
        .all(|request| request.header("cookie").is_none()));
    assert!(requests
        .iter()
        .all(|request| !request.target.contains("readyz")));
    assert!(requests[0].header("origin").is_none());
    assert!(requests[0].header("authorization").is_some());
    assert!(requests[1].header("origin").is_some());
    assert!(requests[1].header("authorization").is_none());
    let origin = expected_origin(&server);
    let bearer = format!("Bearer {OWNER}");
    for request in &requests[2..] {
        assert_single(request, "origin", &origin);
        assert_single(request, "authorization", &bearer);
    }
}

#[tokio::test]
async fn guard_probe_collapses_mismatch_to_one_fixed_error() {
    let server = TestServer::start(|_| TestReply::json(200, json!({"ok": true})));
    let transport = server.transport();
    let error = transport
        .verify_guard()
        .await
        .expect_err("must reject an unguarded 200");
    assert_eq!(error, LocalTransportError::GuardProbeFailed);
    assert_eq!(
        error.to_string(),
        "local transport guard verification failed"
    );
    assert!(!error.to_string().contains(OWNER));
    assert!(!error.to_string().contains(&expected_origin(&server)));
}

#[tokio::test]
async fn guard_probe_caps_the_status_envelope() {
    let server = TestServer::start(|_| TestReply {
        status: 403,
        headers: Vec::new(),
        body: vec![b'x'; GUARD_BODY_LIMIT + 1],
    });
    let transport = server.transport();
    assert_eq!(
        transport
            .verify_guard()
            .await
            .expect_err("oversize envelope must fail"),
        LocalTransportError::GuardProbeFailed
    );
}

#[tokio::test]
async fn proxy_environment_cannot_redirect_loopback() {
    if std::env::var_os(PROXY_CHILD).is_some() {
        let server = TestServer::start(|_| TestReply::json(200, json!({"ok": true})));
        let transport = server.transport();
        let response = transport
            .api_request(Method::GET, "/api/threads", None)
            .expect("allowlisted list")
            .send()
            .await
            .expect("loopback must bypass proxy settings");
        assert_eq!(response.status(), 200);
        return;
    }

    // Proxy variables are process-global. Prove the behaviour in a fresh test
    // subprocess instead of racing the other tests in this process.
    let exe = std::env::current_exe().expect("current test binary");
    let output = std::process::Command::new(exe)
        .args([
            "--exact",
            "local_transport::tests::proxy_environment_cannot_redirect_loopback",
            "--nocapture",
        ])
        .env(PROXY_CHILD, "1")
        .env("HTTP_PROXY", "http://127.0.0.1:9")
        .env("HTTPS_PROXY", "http://127.0.0.1:9")
        .env("ALL_PROXY", "http://127.0.0.1:9")
        .env("http_proxy", "http://127.0.0.1:9")
        .env("https_proxy", "http://127.0.0.1:9")
        .env("all_proxy", "http://127.0.0.1:9")
        .env("NO_PROXY", "")
        .env("no_proxy", "")
        .output()
        .expect("spawn the proxy test child");
    assert!(
        output.status.success(),
        "proxy child failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}

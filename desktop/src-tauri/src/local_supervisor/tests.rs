//! Process-level tests for the headless local supervisor.
//!
//! Protocol cases use a disposable Python fixture that owns the child end of a
//! real private socketpair. The fixture never receives a secret on argv or in
//! its environment: credentials only travel in the config frame, exactly as in
//! production. The opt-in test at the bottom runs an installed sidecar wheel.

use super::*;

#[test]
fn status_requires_json_object_root() {
    let reply = b" \t\r\n[1,\"ready\",\"0123456789abcdef0123456789abcdef\",\"127.0.0.1\",43210]";
    assert!(matches!(
        parse_status(reply, "0123456789abcdef0123456789abcdef"),
        Err(SupervisorError::MalformedStatus)
    ));
}

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const API_KEY: &str = "fake-local-provider-key-0123456789";
const WRONG_TOKEN: &str = "wrong-token";
const FIXTURE_PORT: u16 = 43_210;

/// Fixed fixture program. Modes are selected with argv[1]; argv[2] is the
/// state directory, only for assertions. No credential ever appears in argv.
const FIXTURE: &str = r#"
import json
import os
import struct
import sys
import time


def read_exact(size):
    chunks = []
    while size:
        chunk = os.read(0, size)
        if not chunk:
            raise EOFError
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def read_frame():
    header = read_exact(4)
    size = struct.unpack(">I", header)[0]
    if size > 1_000_000:
        raise SystemExit(3)
    return read_exact(size)


def send_bytes(payload):
    os.write(0, struct.pack(">I", len(payload)) + payload)


def send(payload):
    send_bytes(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


mode = sys.argv[1]
state = sys.argv[2]
config = json.loads(read_frame())
nonce = config["nonce"]


def ready(**overrides):
    payload = {
        "v": 1,
        "type": "ready",
        "nonce": nonce,
        "host": "127.0.0.1",
        "port": 43210,
    }
    payload.update(overrides)
    return payload


def hold():
    while True:
        if not os.read(0, 4096):
            return


if mode == "ready":
    send(ready())
    hold()
elif mode == "ignore-eof":
    send(ready())
    time.sleep(30)
elif mode == "assert-env":
    allowed = {"KELP_LOCAL_STATE_DIR", "KELP_PROVIDER_BASE_URL", "KELP_PRIMARY_MODEL"}
    # CPython may coerce the C locale and reflect LC_CTYPE in its own
    # os.environ; the supervisor never supplies it.
    if set(os.environ) - allowed - {"LC_CTYPE"}:
        raise SystemExit(4)
    if any(name.startswith("KELP_") and name not in allowed for name in os.environ):
        raise SystemExit(16)
    if os.environ.get("KELP_LOCAL_STATE_DIR") != state:
        raise SystemExit(5)
    if os.getcwd() != state:
        raise SystemExit(6)
    if os.environ.get("KELP_PROVIDER_BASE_URL") != "http://127.0.0.1:9/v1":
        raise SystemExit(7)
    if os.environ.get("KELP_PRIMARY_MODEL") != "kelp-test-model":
        raise SystemExit(8)
    if set(config) != {"v", "type", "nonce", "secrets"}:
        raise SystemExit(9)
    if type(config["v"]) is not int or config["v"] != 1:
        raise SystemExit(10)
    if config["type"] != "config":
        raise SystemExit(11)
    if len(nonce) != 32 or any(c not in "0123456789abcdef" for c in nonce):
        raise SystemExit(12)
    secrets = config["secrets"]
    expected = {
        "KELP_AUTH_TOKEN",
        "KELP_PROVIDER_API_KEY",
        "KELP_CSRF_TOKEN",
        "KELP_SESSION_SECRET",
    }
    if set(secrets) != expected:
        raise SystemExit(13)
    if not all(isinstance(secrets[name], str) and secrets[name] for name in secrets):
        raise SystemExit(14)
    def is_hex(value, length):
        return (
            isinstance(value, str)
            and len(value) == length
            and all(c in "0123456789abcdef" for c in value)
        )
    if not is_hex(secrets["KELP_AUTH_TOKEN"], 64):
        raise SystemExit(17)
    if not is_hex(secrets["KELP_CSRF_TOKEN"], 32):
        raise SystemExit(18)
    if not is_hex(secrets["KELP_SESSION_SECRET"], 64):
        raise SystemExit(19)
    send(ready())
    hold()
elif mode == "ready-exit-nonzero":
    send(ready())
    hold()
    sys.exit(1)
elif mode == "wrong-nonce":
    bad = "0" * 32 if nonce != "0" * 32 else "1" * 32
    send(ready(nonce=bad))
elif mode == "wrong-host":
    send(ready(host="0.0.0.0"))
elif mode == "port-zero":
    send(ready(port=0))
elif mode == "port-high":
    send(ready(port=65536))
elif mode == "port-float":
    send(ready(port=43210.0))
elif mode == "port-bool":
    send(ready(port=True))
elif mode == "version-bool":
    send(ready(v=True))
elif mode == "version-float":
    send(ready(v=1.0))
elif mode == "type-wrong":
    send(ready(type="config"))
elif mode == "unknown-key":
    send(ready(extra=1))
elif mode == "missing-key":
    payload = ready()
    del payload["host"]
    send(payload)
elif mode == "ready-code-null":
    send(ready(code=None))
elif mode == "ready-host-null":
    send(ready(host=None))
elif mode == "ready-port-null":
    send(ready(port=None))
elif mode == "error-host-null":
    send({"v": 1, "type": "error", "nonce": nonce, "code": "startup_failed", "host": None})
elif mode == "error-port-null":
    send({"v": 1, "type": "error", "nonce": nonce, "code": "startup_failed", "port": None})
elif mode == "duplicate":
    payload = (
        '{"v":1,"v":1,"type":"ready","nonce":"%s",'
        '"host":"127.0.0.1","port":43210}' % nonce
    )
    send_bytes(payload.encode("utf-8"))
elif mode == "duplicate-type":
    payload = (
        '{"v":1,"type":"ready","type":"ready","nonce":"%s",'
        '"host":"127.0.0.1","port":43210}' % nonce
    )
    send_bytes(payload.encode("utf-8"))
elif mode == "malformed-json":
    send_bytes(b"{")
elif mode == "bad-utf8":
    send_bytes(b"\xff")
elif mode == "error":
    send({"v": 1, "type": "error", "nonce": nonce, "code": "invalid_config"})
elif mode == "error-null":
    send({"v": 1, "type": "error", "nonce": None, "code": "startup_failed"})
elif mode == "error-missing-nonce":
    send({"v": 1, "type": "error", "code": "startup_failed"})
elif mode == "error-unknown":
    send({"v": 1, "type": "error", "nonce": nonce, "code": "not_a_real_code"})
elif mode == "error-wrong-nonce":
    bad = "0" * 32 if nonce != "0" * 32 else "1" * 32
    send({"v": 1, "type": "error", "nonce": bad, "code": "invalid_config"})
elif mode == "oversize":
    os.write(0, struct.pack(">I", 4097))
    time.sleep(30)
elif mode == "zero-length":
    os.write(0, struct.pack(">I", 0))
    time.sleep(30)
elif mode == "partial":
    payload = json.dumps(ready(), separators=(",", ":")).encode("utf-8")
    os.write(0, struct.pack(">I", len(payload)) + payload[:5])
    time.sleep(30)
elif mode == "silent":
    time.sleep(30)
elif mode == "eof":
    pass
else:
    raise SystemExit(15)
"#;

/// Import probe for the opt-in installed-wheel test. It proves the module
/// comes from the interpreter's installed purelib/site-packages, that the
/// distribution metadata owns the module file, and that the install is not an
/// editable checkout pointing back at a working tree.
const INSTALL_PROBE: &str = r#"
import importlib.metadata as metadata
import os
import sysconfig

import chat.local_serve as local_serve

purelib = os.path.realpath(sysconfig.get_path("purelib"))
module_path = os.path.realpath(local_serve.__file__)
assert module_path.startswith(purelib + os.sep), (
    "chat.local_serve is outside the installed purelib: "
    + module_path
    + " (purelib "
    + purelib
    + ")"
)
distribution = metadata.distribution("kelp-assistant")
files = {str(item) for item in (distribution.files or [])}
assert "chat/local_serve.py" in files, (
    "installed distribution metadata does not own chat/local_serve.py"
)
direct_url = (distribution.read_text("direct_url.json") or "").replace(" ", "")
assert '"editable":true' not in direct_url, "editable checkout is not an installed wheel"
print(module_path)
"#;

static TEMP_COUNTER: AtomicUsize = AtomicUsize::new(0);

struct TempDir(PathBuf);

impl TempDir {
    fn new() -> Self {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "kelp-local-supervisor-{}-{}-{}",
            std::process::id(),
            TEMP_COUNTER.fetch_add(1, Ordering::Relaxed),
            nanos
        ));
        // `create` is one atomic mkdir and fails if the path already exists,
        // so a test never adopts a directory it did not create. The explicit
        // 0700 mode keeps the state root private even under a permissive umask.
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
        eprintln!("KELP_TEST_SYSTEM_PYTHON is not an absolute file; probing defaults");
    }
    ["/usr/bin/python3", "/usr/local/bin/python3", "/bin/python3"]
        .into_iter()
        .map(PathBuf::from)
        .find(|path| path.is_file())
}

struct Fixture {
    _dir: TempDir,
    python: PathBuf,
    script: PathBuf,
    state: PathBuf,
}

impl Fixture {
    fn new() -> Option<Self> {
        let python = trusted_python()?;
        let dir = TempDir::new();
        let script = dir.path().join("fixture.py");
        std::fs::write(&script, FIXTURE).expect("write fixture");
        let state = dir.path().join("state");
        Some(Self {
            _dir: dir,
            python,
            script,
            state,
        })
    }

    fn settings(&self) -> SupervisorSettings<'_> {
        let mut settings = SupervisorSettings::new(&self.python, &self.state, API_KEY);
        settings.provider_base_url = Some("http://127.0.0.1:9/v1");
        settings.primary_model = Some("kelp-test-model");
        settings.handshake_timeout = Duration::from_secs(2);
        settings.shutdown_timeout = Duration::from_millis(300);
        settings
    }

    fn spawn_mode(
        &self,
        mode: &str,
        handshake: Duration,
        shutdown: Duration,
    ) -> Result<Supervisor, SupervisorError> {
        let mut settings = self.settings();
        settings.handshake_timeout = handshake;
        settings.shutdown_timeout = shutdown;
        let mut command = Command::new(&self.python);
        command.arg(&self.script).arg(mode).arg(&self.state);
        spawn_command(&settings, command)
    }

    fn expect_error(&self, mode: &str, handshake: Duration) -> SupervisorError {
        match self.spawn_mode(mode, handshake, Duration::from_millis(200)) {
            Ok(_) => panic!("fixture mode {mode} unexpectedly produced readiness"),
            Err(error) => error,
        }
    }
}

fn skip_if_no_fixture() -> Option<Fixture> {
    match Fixture::new() {
        Some(fixture) => Some(fixture),
        None => {
            eprintln!("skipping local-supervisor fixture test: no trusted absolute Python");
            None
        }
    }
}

fn child_pid(supervisor: &Supervisor) -> u32 {
    supervisor
        .guard
        .child
        .as_ref()
        .expect("owned child before shutdown")
        .id()
}

fn process_exists(pid: u32) -> bool {
    Path::new("/proc").join(pid.to_string()).exists()
}

fn assert_hex(value: &str, length: usize) {
    assert_eq!(value.len(), length, "unexpected credential length");
    assert!(value
        .bytes()
        .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)));
}

#[test]
fn nonce_is_32_lowercase_hex_and_random_per_launch() {
    let first = random_nonce().expect("entropy");
    let second = random_nonce().expect("entropy");
    assert_eq!(first.len(), 32);
    assert!(first
        .bytes()
        .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)));
    assert_eq!(second.len(), 32);
    assert_ne!(first, second);
}

#[test]
fn generated_credentials_are_os_random_hex() {
    let first = LocalCredentials::generate().expect("credentials");
    let second = LocalCredentials::generate().expect("credentials");
    assert_hex(first.owner_token(), 64);
    assert_hex(first.csrf_token(), 32);
    assert_hex(first.session_secret(), 64);
    assert_ne!(first.owner_token(), second.owner_token());
    assert_ne!(first.csrf_token(), second.csrf_token());
    assert_ne!(first.session_secret(), second.session_secret());
}

#[test]
fn config_frame_has_the_frozen_shape_and_only_the_frame_carries_secrets() {
    let dir = TempDir::new();
    let settings = SupervisorSettings::new(Path::new("/usr/bin/python3"), dir.path(), API_KEY);
    let credentials = LocalCredentials {
        owner_token: "a".repeat(64),
        csrf_token: "b".repeat(32),
        session_secret: "c".repeat(64),
    };
    let nonce = "0123456789abcdef0123456789abcdef";
    let bytes = build_config(&settings, nonce, &credentials).expect("config");
    assert!(bytes.len() <= CONFIG_MAX_BYTES);

    let value: Value = serde_json::from_slice(&bytes).expect("json");
    assert_eq!(
        value,
        serde_json::json!({
            "v": 1,
            "type": "config",
            "nonce": nonce,
            "secrets": {
                "KELP_AUTH_TOKEN": credentials.owner_token(),
                "KELP_PROVIDER_API_KEY": API_KEY,
                "KELP_CSRF_TOKEN": credentials.csrf_token(),
                "KELP_SESSION_SECRET": credentials.session_secret(),
            },
        })
    );

    // Errors never carry frame contents; these are the only reachable debug
    // strings for post-frame failures.
    for error in [
        SupervisorError::Timeout,
        SupervisorError::MalformedStatus,
        SupervisorError::NonceMismatch,
        SupervisorError::Sidecar(SidecarCode::InvalidConfig),
    ] {
        let rendered = format!("{error:?} {error}");
        assert!(!rendered.contains(credentials.owner_token()));
        assert!(!rendered.contains(credentials.csrf_token()));
        assert!(!rendered.contains(credentials.session_secret()));
        assert!(!rendered.contains(API_KEY));
    }
}

#[test]
fn settings_reject_untrusted_paths_and_invalid_secrets() {
    let dir = TempDir::new();
    let valid_python = Path::new("/usr/bin/python3");
    let valid_state = dir.path();

    let relative_python = SupervisorSettings::new(Path::new("python3"), valid_state, API_KEY);
    assert!(validate_settings(&relative_python).is_err());

    let relative_state = SupervisorSettings::new(valid_python, Path::new("state"), API_KEY);
    assert!(validate_settings(&relative_state).is_err());

    let root_state = SupervisorSettings::new(valid_python, Path::new("/"), API_KEY);
    assert!(validate_settings(&root_state).is_err());

    let empty_key = SupervisorSettings::new(valid_python, valid_state, "");
    assert!(validate_settings(&empty_key).is_err());

    let long = "a".repeat(SECRET_MAX_BYTES + 1);
    let oversize = SupervisorSettings::new(valid_python, valid_state, &long);
    assert!(validate_settings(&oversize).is_err());

    let control = SupervisorSettings::new(valid_python, valid_state, "bad\nsecret");
    assert!(validate_settings(&control).is_err());

    let mut no_deadline = SupervisorSettings::new(valid_python, valid_state, API_KEY);
    no_deadline.handshake_timeout = Duration::ZERO;
    assert!(validate_settings(&no_deadline).is_err());

    let traversal = valid_state.join("child").join("..").join("state");
    let parent_traversal = SupervisorSettings::new(valid_python, &traversal, API_KEY);
    assert!(matches!(
        validate_settings(&parent_traversal),
        Err(SupervisorError::InvalidSettings(_))
    ));
}

#[test]
fn state_dir_is_created_private_and_public_existing_dir_is_rejected() {
    let dir = TempDir::new();
    let created = dir.path().join("state");
    prepare_state_dir(&created).expect("create state");
    assert_eq!(
        std::fs::symlink_metadata(&created).expect("stat").mode() & 0o7777,
        0o700
    );
    prepare_state_dir(&created).expect("reuse private state");

    let public = dir.path().join("public");
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&public)
        .expect("create public state");
    std::fs::set_permissions(&public, std::fs::Permissions::from_mode(0o755))
        .expect("make state public");
    let error = prepare_state_dir(&public).expect_err("public state must be rejected");
    assert!(
        matches!(error, SupervisorError::InvalidSettings(_)),
        "unexpected error: {error:?}"
    );
    // Existing directories are never repaired or chmodded in place.
    assert_eq!(
        std::fs::symlink_metadata(&public).expect("stat").mode() & 0o7777,
        0o755
    );
}

#[test]
fn state_dir_symlink_to_root_is_rejected_before_spawn() {
    let dir = TempDir::new();
    let link = dir.path().join("root-alias");
    std::os::unix::fs::symlink("/", &link).expect("symlink to root");

    // A private fake provider: if the supervisor wrongly spawned it, the
    // marker would appear next to the symlink. Nothing writes to `/`.
    let marker = dir.path().join("spawned");
    let script = dir.path().join("fake-provider.sh");
    std::fs::write(
        &script,
        format!("#!/bin/sh\ntouch '{}'\n", marker.display()),
    )
    .expect("write fake provider");
    std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o700))
        .expect("make fake provider executable");

    let settings = SupervisorSettings::new(Path::new("/usr/bin/python3"), &link, API_KEY);
    let error = match spawn_command(&settings, Command::new(&script)) {
        Ok(_) => panic!("state symlink to / unexpectedly spawned"),
        Err(error) => error,
    };
    assert!(
        matches!(error, SupervisorError::InvalidSettings(_)),
        "unexpected error: {error:?}"
    );
    assert_eq!(std::fs::read_link(&link).expect("readlink"), Path::new("/"));
    assert!(!marker.exists(), "supervisor spawned the fake provider");
    assert_eq!(
        std::fs::read_dir(dir.path())
            .expect("read temp dir")
            .count(),
        2,
        "only the symlink and fake provider should exist"
    );
}

#[test]
fn owned_child_reaches_readiness_and_is_reaped_on_shutdown() {
    let Some(fixture) = skip_if_no_fixture() else {
        return;
    };
    let mut supervisor =
        match fixture.spawn_mode("ready", Duration::from_secs(3), Duration::from_secs(2)) {
            Ok(supervisor) => supervisor,
            Err(error) => panic!("ready fixture failed: {error:?}"),
        };
    assert_eq!(supervisor.endpoint().host(), "127.0.0.1");
    assert_eq!(supervisor.endpoint().port(), FIXTURE_PORT);
    assert_eq!(
        supervisor.endpoint().origin(),
        format!("http://127.0.0.1:{FIXTURE_PORT}")
    );
    assert_hex(supervisor.credentials().owner_token(), 64);
    assert_hex(supervisor.credentials().csrf_token(), 32);
    assert_hex(supervisor.credentials().session_secret(), 64);
    let pid = child_pid(&supervisor);

    let started = Instant::now();
    supervisor.shutdown().expect("clean shutdown");
    assert!(started.elapsed() < Duration::from_secs(5));
    let outcome = supervisor.shutdown_outcome().expect("shutdown outcome");
    assert!(outcome.clean_exit());
    assert!(!outcome.forced());
    assert_eq!(outcome.code(), Some(0));
    assert_eq!(outcome.signal(), None);
    assert!(!process_exists(pid));
}

#[test]
fn shutdown_reports_nonzero_exit_as_not_clean_without_forcing() {
    let Some(fixture) = skip_if_no_fixture() else {
        return;
    };
    let mut supervisor = match fixture.spawn_mode(
        "ready-exit-nonzero",
        Duration::from_secs(3),
        Duration::from_secs(2),
    ) {
        Ok(supervisor) => supervisor,
        Err(error) => panic!("nonzero-exit fixture failed: {error:?}"),
    };
    let pid = child_pid(&supervisor);

    supervisor.shutdown().expect("clean shutdown");
    let outcome = supervisor.shutdown_outcome().expect("shutdown outcome");
    assert!(!outcome.clean_exit());
    assert!(!outcome.forced());
    assert_eq!(outcome.code(), Some(1));
    assert_eq!(outcome.signal(), None);
    assert!(!process_exists(pid));
}

#[test]
fn clean_exit_requires_zero_code_without_signal_or_force() {
    let clean = ShutdownOutcome {
        code: Some(0),
        signal: None,
        forced: false,
    };
    assert!(clean.clean_exit());
    assert!(!ShutdownOutcome {
        code: Some(1),
        ..clean
    }
    .clean_exit());
    assert!(!ShutdownOutcome {
        code: None,
        signal: Some(15),
        ..clean
    }
    .clean_exit());
    assert!(!ShutdownOutcome {
        forced: true,
        ..clean
    }
    .clean_exit());
}

#[test]
fn child_environment_is_cleared_except_explicit_nonsecret_values() {
    let Some(fixture) = skip_if_no_fixture() else {
        return;
    };
    let mut supervisor =
        match fixture.spawn_mode("assert-env", Duration::from_secs(3), Duration::from_secs(2)) {
            Ok(supervisor) => supervisor,
            Err(error) => panic!("environment fixture failed: {error:?}"),
        };
    assert_eq!(supervisor.endpoint().port(), FIXTURE_PORT);
    supervisor.shutdown().expect("clean shutdown");
}

#[test]
fn handshake_rejects_wrong_nonce_host_and_port_shapes() {
    let Some(fixture) = skip_if_no_fixture() else {
        return;
    };
    let cases = [
        ("wrong-nonce", SupervisorError::NonceMismatch),
        ("wrong-host", SupervisorError::HostNotLoopback),
        ("port-zero", SupervisorError::PortOutOfRange),
        ("port-high", SupervisorError::PortOutOfRange),
        ("port-float", SupervisorError::MalformedStatus),
        ("port-bool", SupervisorError::MalformedStatus),
        ("version-bool", SupervisorError::MalformedStatus),
        ("version-float", SupervisorError::MalformedStatus),
        ("type-wrong", SupervisorError::MalformedStatus),
    ];
    for (mode, expected) in cases {
        let error = fixture.expect_error(mode, Duration::from_secs(2));
        assert_eq!(error, expected, "fixture mode {mode}");
    }
}

#[test]
fn handshake_rejects_unknown_duplicate_and_missing_keys() {
    let Some(fixture) = skip_if_no_fixture() else {
        return;
    };
    assert_eq!(
        fixture.expect_error("unknown-key", Duration::from_secs(2)),
        SupervisorError::MalformedStatus
    );
    assert_eq!(
        fixture.expect_error("missing-key", Duration::from_secs(2)),
        SupervisorError::MalformedStatus
    );
    assert_eq!(
        fixture.expect_error("duplicate", Duration::from_secs(2)),
        SupervisorError::MalformedStatus
    );
    assert_eq!(
        fixture.expect_error("duplicate-type", Duration::from_secs(2)),
        SupervisorError::MalformedStatus
    );
}

#[test]
fn handshake_rejects_explicit_nulls_in_wrong_variant_fields() {
    let Some(fixture) = skip_if_no_fixture() else {
        return;
    };
    for mode in [
        "ready-code-null",
        "ready-host-null",
        "ready-port-null",
        "error-host-null",
        "error-port-null",
    ] {
        let error = fixture.expect_error(mode, Duration::from_secs(2));
        assert_eq!(
            error,
            SupervisorError::MalformedStatus,
            "fixture mode {mode}"
        );
    }
}

#[test]
fn handshake_rejects_malformed_and_oversize_status_frames() {
    let Some(fixture) = skip_if_no_fixture() else {
        return;
    };
    assert_eq!(
        fixture.expect_error("malformed-json", Duration::from_secs(2)),
        SupervisorError::InvalidStatusJson
    );
    assert_eq!(
        fixture.expect_error("bad-utf8", Duration::from_secs(2)),
        SupervisorError::InvalidStatusUtf8
    );
    assert_eq!(
        fixture.expect_error("oversize", Duration::from_secs(2)),
        SupervisorError::BadStatusLength
    );
    assert_eq!(
        fixture.expect_error("zero-length", Duration::from_secs(2)),
        SupervisorError::BadStatusLength
    );
}

#[test]
fn handshake_maps_known_sidecar_errors_without_echoing_contents() {
    let Some(fixture) = skip_if_no_fixture() else {
        return;
    };
    assert_eq!(
        fixture.expect_error("error", Duration::from_secs(2)),
        SupervisorError::Sidecar(SidecarCode::InvalidConfig)
    );
    assert_eq!(
        fixture.expect_error("error-null", Duration::from_secs(2)),
        SupervisorError::Sidecar(SidecarCode::StartupFailed)
    );
    assert_eq!(
        fixture.expect_error("error-missing-nonce", Duration::from_secs(2)),
        SupervisorError::MalformedStatus
    );
    assert_eq!(
        fixture.expect_error("error-unknown", Duration::from_secs(2)),
        SupervisorError::UnknownErrorCode
    );
    assert_eq!(
        fixture.expect_error("error-wrong-nonce", Duration::from_secs(2)),
        SupervisorError::NonceMismatch
    );
}

#[test]
fn missing_readiness_eof_and_silence_fail_with_bounded_deadline() {
    let Some(fixture) = skip_if_no_fixture() else {
        return;
    };
    assert_eq!(
        fixture.expect_error("eof", Duration::from_secs(2)),
        SupervisorError::ChannelClosed
    );
    assert_eq!(
        fixture.expect_error("silent", Duration::from_millis(500)),
        SupervisorError::Timeout
    );
    assert_eq!(
        fixture.expect_error("partial", Duration::from_millis(500)),
        SupervisorError::Timeout
    );
}

#[test]
fn shutdown_kills_and_reaps_a_child_that_ignores_eof() {
    let Some(fixture) = skip_if_no_fixture() else {
        return;
    };
    let mut supervisor = match fixture.spawn_mode(
        "ignore-eof",
        Duration::from_secs(3),
        Duration::from_millis(200),
    ) {
        Ok(supervisor) => supervisor,
        Err(error) => panic!("stubborn fixture failed: {error:?}"),
    };
    let pid = child_pid(&supervisor);

    let started = Instant::now();
    supervisor.shutdown().expect("shutdown fallback");
    let elapsed = started.elapsed();
    assert!(elapsed >= Duration::from_millis(150));
    assert!(elapsed < Duration::from_secs(5));
    let outcome = supervisor.shutdown_outcome().expect("shutdown outcome");
    assert!(outcome.forced());
    assert!(!outcome.clean_exit());
    assert_eq!(outcome.signal(), Some(9));
    assert_eq!(outcome.code(), None);
    assert!(!process_exists(pid));
}

#[test]
fn spawn_failure_is_reported_without_leaking_a_child() {
    let dir = TempDir::new();
    let python = dir.path().join("missing-python");
    let settings = SupervisorSettings::new(&python, dir.path(), API_KEY);
    let command = Command::new(&python);
    match spawn_command(&settings, command) {
        Ok(_) => panic!("missing interpreter unexpectedly spawned"),
        Err(error) => {
            assert_eq!(error, SupervisorError::Spawn(std::io::ErrorKind::NotFound));
        }
    }
}

fn http_health(port: u16, token: Option<&str>) -> std::io::Result<u16> {
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_secs(5))?;
    stream.set_read_timeout(Some(Duration::from_secs(15)))?;
    stream.set_write_timeout(Some(Duration::from_secs(5)))?;
    let mut request =
        format!("GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n");
    if let Some(token) = token {
        request.push_str(&format!("Authorization: Bearer {token}\r\n"));
    }
    request.push_str("\r\n");
    stream.write_all(request.as_bytes())?;
    let mut response = Vec::new();
    stream.read_to_end(&mut response)?;
    let text = String::from_utf8_lossy(&response);
    let status = text
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|code| code.parse().ok())
        .ok_or_else(|| {
            std::io::Error::new(std::io::ErrorKind::InvalidData, "missing HTTP status")
        })?;
    Ok(status)
}

#[test]
#[ignore = "requires KELP_TEST_INSTALLED_PYTHON=/abs/venv/bin/python with an installed wheel"]
fn installed_wheel_health_and_stop() {
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
    let probe = Command::new(&python)
        .args(["-I", "-B", "-c", INSTALL_PROBE])
        .env_clear()
        .output()
        .unwrap_or_else(|error| panic!("KELP_TEST_INSTALLED_PYTHON is not executable: {error}"));
    assert!(
        probe.status.success(),
        "installed interpreter failed the installed-wheel import probe\nstdout: {}\nstderr: {}",
        String::from_utf8_lossy(&probe.stdout),
        String::from_utf8_lossy(&probe.stderr),
    );

    let dir = TempDir::new();
    let state = dir.path().join("state");
    let mut settings = SupervisorSettings::new(&python, &state, API_KEY);
    settings.provider_base_url = Some("http://127.0.0.1:9/v1");
    settings.primary_model = Some("kelp-test-model");
    settings.handshake_timeout = Duration::from_secs(90);
    settings.shutdown_timeout = Duration::from_secs(5);

    let mut supervisor = match Supervisor::spawn(&settings) {
        Ok(supervisor) => supervisor,
        Err(error) => panic!("installed sidecar did not become ready: {error:?}"),
    };
    let port = supervisor.endpoint().port();
    let owner_token = supervisor.credentials().owner_token().to_owned();
    assert_hex(&owner_token, 64);
    assert_eq!(supervisor.endpoint().host(), "127.0.0.1");
    assert_eq!(http_health(port, None).expect("unauth health"), 401);
    assert_eq!(
        http_health(port, Some(WRONG_TOKEN)).expect("wrong token health"),
        401
    );
    assert_eq!(
        http_health(port, Some(&owner_token)).expect("authenticated health"),
        200
    );

    supervisor.shutdown().expect("normal shutdown");
    let outcome = supervisor.shutdown_outcome().expect("shutdown outcome");
    assert!(outcome.clean_exit());
    assert!(!outcome.forced());
    assert_eq!(outcome.code(), Some(0));
    assert_eq!(outcome.signal(), None);
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    assert!(
        TcpStream::connect_timeout(&address, Duration::from_millis(500)).is_err(),
        "listener still accepted connections after shutdown"
    );
}

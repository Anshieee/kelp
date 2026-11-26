//! Headless Linux supervisor for the KELP local sidecar (v1 channel).
//!
//! This module owns a child process exactly as described by
//! `docs/LOCAL_SUPERVISOR.md`: a trusted absolute Python interpreter is
//! launched with `-I -B -m chat.local_serve`, its fd 0 is one end of a private
//! `AF_UNIX`/`SOCK_STREAM` socketpair, and the parent writes one bounded config
//! frame and reads one bounded ready/error frame. Stdout and stderr are
//! discarded; the status frame is the only readiness signal.
//!
//! Secrets are passed only inside the config frame. They never appear on the
//! command line, in the child environment, or in an error/debug string. The
//! process environment is cleared and rebuilt from a fixed nonsecret allowlist
//! plus the required local state root.
//!
//! The module is compiled only for Linux tests in this stage; no desktop
//! entrypoint is bound to it.

use std::fmt;
use std::fs::{DirBuilder, File, Metadata};
use std::io::{self, Read, Write};
use std::os::fd::OwnedFd;
use std::os::unix::fs::{DirBuilderExt, MetadataExt};
use std::os::unix::net::UnixStream;
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::time::{Duration, Instant};

use serde::de::{self, Visitor};
use serde::Deserialize;
use serde_json::{Map, Value};

/// Frozen protocol version carried by both frame types.
pub const PROTOCOL_VERSION: u32 = 1;
/// Upper bound for the single config frame payload.
pub const CONFIG_MAX_BYTES: usize = 65_536;
/// Upper bound for one ready/error status frame payload.
pub const STATUS_MAX_BYTES: usize = 4_096;
/// Upper bound for one secret value.
pub const SECRET_MAX_BYTES: usize = 1_024;
/// Absolute handshake deadline shared by the config write and status read.
pub const DEFAULT_HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(30);
/// Bounded wait for a cooperative child exit before `kill` + `wait`.
pub const DEFAULT_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(2);

const LOOPBACK_HOST: &str = "127.0.0.1";
/// Owner-only mode for a state root the supervisor creates. Existing roots
/// must already have exactly this mode; the supervisor never chmods them.
const STATE_DIR_MODE: u32 = 0o700;

/// Everything the supervisor needs to launch and configure one sidecar.
///
/// Deliberately not `Debug`: it holds credentials. Secrets travel only in the
/// config frame produced by [`build_config`].
pub struct SupervisorSettings<'a> {
    /// Trusted absolute interpreter path.
    pub python: &'a Path,
    /// Absolute, non-root state root. It becomes the child cwd and
    /// `KELP_LOCAL_STATE_DIR`.
    pub state_dir: &'a Path,
    /// Nonsecret provider endpoint copied only when non-empty.
    pub provider_base_url: Option<&'a str>,
    /// Nonsecret model name copied only when non-empty.
    pub primary_model: Option<&'a str>,
    /// Required `KELP_PROVIDER_API_KEY` frame secret. The owner, CSRF, and
    /// session credentials are minted in Rust per launch.
    pub provider_api_key: &'a str,
    /// Absolute deadline covering the config write and status read.
    pub handshake_timeout: Duration,
    /// Bounded EOF wait before a forced kill.
    pub shutdown_timeout: Duration,
}

impl<'a> SupervisorSettings<'a> {
    pub fn new(python: &'a Path, state_dir: &'a Path, provider_api_key: &'a str) -> Self {
        Self {
            python,
            state_dir,
            provider_base_url: None,
            primary_model: None,
            provider_api_key,
            handshake_timeout: DEFAULT_HANDSHAKE_TIMEOUT,
            shutdown_timeout: DEFAULT_SHUTDOWN_TIMEOUT,
        }
    }
}

/// Per-launch credentials minted from OS entropy and retained for the next
/// Local bridge slice.
///
/// Deliberately not `Debug`/`Display`: access requires explicit accessors.
pub struct LocalCredentials {
    owner_token: String,
    csrf_token: String,
    session_secret: String,
}

impl LocalCredentials {
    fn generate() -> Result<Self, SupervisorError> {
        Ok(Self {
            owner_token: random_hex::<32>()?,
            csrf_token: random_hex::<16>()?,
            session_secret: random_hex::<32>()?,
        })
    }

    pub fn owner_token(&self) -> &str {
        &self.owner_token
    }

    pub fn csrf_token(&self) -> &str {
        &self.csrf_token
    }

    pub fn session_secret(&self) -> &str {
        &self.session_secret
    }
}

/// The loopback endpoint reported by a validated ready frame.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct LocalEndpoint {
    port: u16,
}

impl LocalEndpoint {
    pub fn host(self) -> &'static str {
        LOOPBACK_HOST
    }

    pub fn port(self) -> u16 {
        self.port
    }

    pub fn origin(self) -> String {
        format!("http://{LOOPBACK_HOST}:{}", self.port)
    }
}

/// How the child ended when the supervisor last stopped it.
///
/// `forced` distinguishes an unforced EOF exit (including a nonzero
/// application exit or an external signal) from the bounded-wait fallback that
/// sends `SIGKILL`. [`Self::clean_exit`] is the stricter success check.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ShutdownOutcome {
    code: Option<i32>,
    signal: Option<i32>,
    forced: bool,
}

impl ShutdownOutcome {
    fn from_status(status: ExitStatus, forced: bool) -> Self {
        Self {
            code: status.code(),
            signal: status.signal(),
            forced,
        }
    }

    /// A clean sidecar exit: the child returned code 0 on its own, without a
    /// terminating signal and without the bounded-wait `SIGKILL` fallback.
    ///
    /// A nonzero application exit or an external signal is still an
    /// unforced exit, so [`Self::forced`], [`Self::code`], and
    /// [`Self::signal`] stay independent of this check.
    pub fn clean_exit(self) -> bool {
        !self.forced && self.code == Some(0) && self.signal.is_none()
    }

    pub fn forced(self) -> bool {
        self.forced
    }

    pub fn code(self) -> Option<i32> {
        self.code
    }

    pub fn signal(self) -> Option<i32> {
        self.signal
    }
}

/// Fixed sidecar error codes, mirroring the frozen v1 set.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SidecarCode {
    BadFrame,
    BadLength,
    DuplicateKey,
    InheritedSecret,
    InternalError,
    InvalidConfig,
    InvalidJson,
    InvalidNonce,
    InvalidSecret,
    InvalidStateDir,
    InvalidUtf8,
    MissingSecret,
    OversizeSecret,
    StartupFailed,
    Timeout,
    UnknownKey,
    UnknownSecret,
    UnsupportedInheritedConfig,
}

impl SidecarCode {
    fn parse(value: &str) -> Option<Self> {
        Some(match value {
            "bad_frame" => Self::BadFrame,
            "bad_length" => Self::BadLength,
            "duplicate_key" => Self::DuplicateKey,
            "inherited_secret" => Self::InheritedSecret,
            "internal_error" => Self::InternalError,
            "invalid_config" => Self::InvalidConfig,
            "invalid_json" => Self::InvalidJson,
            "invalid_nonce" => Self::InvalidNonce,
            "invalid_secret" => Self::InvalidSecret,
            "invalid_state_dir" => Self::InvalidStateDir,
            "invalid_utf8" => Self::InvalidUtf8,
            "missing_secret" => Self::MissingSecret,
            "oversize_secret" => Self::OversizeSecret,
            "startup_failed" => Self::StartupFailed,
            "timeout" => Self::Timeout,
            "unknown_key" => Self::UnknownKey,
            "unknown_secret" => Self::UnknownSecret,
            "unsupported_inherited_config" => Self::UnsupportedInheritedConfig,
            _ => return None,
        })
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::BadFrame => "bad_frame",
            Self::BadLength => "bad_length",
            Self::DuplicateKey => "duplicate_key",
            Self::InheritedSecret => "inherited_secret",
            Self::InternalError => "internal_error",
            Self::InvalidConfig => "invalid_config",
            Self::InvalidJson => "invalid_json",
            Self::InvalidNonce => "invalid_nonce",
            Self::InvalidSecret => "invalid_secret",
            Self::InvalidStateDir => "invalid_state_dir",
            Self::InvalidUtf8 => "invalid_utf8",
            Self::MissingSecret => "missing_secret",
            Self::OversizeSecret => "oversize_secret",
            Self::StartupFailed => "startup_failed",
            Self::Timeout => "timeout",
            Self::UnknownKey => "unknown_key",
            Self::UnknownSecret => "unknown_secret",
            Self::UnsupportedInheritedConfig => "unsupported_inherited_config",
        }
    }
}

impl fmt::Display for SidecarCode {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

/// Safe, fixed supervisor failures. No variant carries frame contents.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SupervisorError {
    InvalidSettings(&'static str),
    EntropyUnavailable,
    Spawn(io::ErrorKind),
    Io(io::ErrorKind),
    ConfigTooLarge,
    Timeout,
    ChannelClosed,
    BadStatusLength,
    InvalidStatusUtf8,
    InvalidStatusJson,
    MalformedStatus,
    NonceMismatch,
    HostNotLoopback,
    PortOutOfRange,
    UnknownErrorCode,
    Sidecar(SidecarCode),
}

impl fmt::Display for SupervisorError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidSettings(reason) => {
                write!(formatter, "invalid supervisor settings: {reason}")
            }
            Self::EntropyUnavailable => formatter.write_str("cannot read OS entropy"),
            Self::Spawn(kind) => write!(formatter, "cannot spawn sidecar: {kind:?}"),
            Self::Io(kind) => write!(formatter, "supervisor I/O error: {kind:?}"),
            Self::ConfigTooLarge => formatter.write_str("config frame exceeds its bound"),
            Self::Timeout => formatter.write_str("sidecar handshake timed out"),
            Self::ChannelClosed => formatter.write_str("sidecar channel closed before readiness"),
            Self::BadStatusLength => formatter.write_str("status frame has an invalid length"),
            Self::InvalidStatusUtf8 => formatter.write_str("status frame is not valid UTF-8"),
            Self::InvalidStatusJson => formatter.write_str("status frame is not valid JSON"),
            Self::MalformedStatus => formatter.write_str("status frame does not match v1"),
            Self::NonceMismatch => formatter.write_str("status nonce does not match"),
            Self::HostNotLoopback => formatter.write_str("sidecar endpoint is not loopback"),
            Self::PortOutOfRange => formatter.write_str("sidecar port is not 1..=65535"),
            Self::UnknownErrorCode => formatter.write_str("sidecar reported an unknown code"),
            Self::Sidecar(code) => write!(formatter, "sidecar reported {}", code.as_str()),
        }
    }
}

impl std::error::Error for SupervisorError {}

/// A ready sidecar and its owned child process.
pub struct Supervisor {
    guard: ChildGuard,
    endpoint: LocalEndpoint,
    credentials: LocalCredentials,
}

impl Supervisor {
    /// Launch the production command and complete the v1 handshake.
    pub fn spawn(settings: &SupervisorSettings<'_>) -> Result<Self, SupervisorError> {
        validate_settings(settings)?;
        // Never canonicalize the interpreter: a virtualenv launcher is
        // commonly a symlink, and resolving it would drop the venv's
        // `sys.prefix`, making `-I -m chat.local_serve` miss the wheel.
        let mut command = Command::new(settings.python);
        command.args(["-I", "-B", "-m", "chat.local_serve"]);
        spawn_command(settings, command)
    }

    pub fn endpoint(&self) -> LocalEndpoint {
        self.endpoint
    }

    /// Owner, CSRF, and session credentials minted for this launch. The next
    /// Local bridge slice consumes them; no caller/UI supplies them.
    pub fn credentials(&self) -> &LocalCredentials {
        &self.credentials
    }

    /// Exit details from the last [`Self::shutdown`] or drop.
    pub fn shutdown_outcome(&self) -> Option<ShutdownOutcome> {
        self.guard.last_shutdown
    }

    /// Close the channel (normal EOF), wait a bounded interval, then kill and
    /// reap. Idempotent; [`Drop`] calls the same path.
    pub fn shutdown(&mut self) -> Result<(), SupervisorError> {
        self.guard.terminate()
    }
}

impl Drop for Supervisor {
    fn drop(&mut self) {
        let _ = self.guard.terminate();
    }
}

struct ChildGuard {
    child: Option<Child>,
    channel: Option<UnixStream>,
    shutdown_timeout: Duration,
    last_shutdown: Option<ShutdownOutcome>,
}

impl ChildGuard {
    fn handshake(
        &mut self,
        config: &[u8],
        nonce: &str,
        timeout: Duration,
    ) -> Result<LocalEndpoint, SupervisorError> {
        let channel = self
            .channel
            .as_mut()
            .ok_or(SupervisorError::ChannelClosed)?;
        exchange(channel, config, nonce, timeout)
    }

    fn terminate(&mut self) -> Result<(), SupervisorError> {
        // Closing the parent end is the child's normal EOF shutdown request.
        self.channel.take();

        let Some(mut child) = self.child.take() else {
            return Ok(());
        };

        let deadline = Instant::now()
            .checked_add(self.shutdown_timeout)
            .unwrap_or_else(Instant::now);
        loop {
            match child.try_wait() {
                Ok(Some(status)) => {
                    self.last_shutdown = Some(ShutdownOutcome::from_status(status, false));
                    return Ok(());
                }
                Ok(None) => {}
                Err(error) => {
                    let _ = child.kill();
                    if let Ok(status) = child.wait() {
                        self.last_shutdown = Some(ShutdownOutcome::from_status(status, true));
                    }
                    return Err(SupervisorError::Io(error.kind()));
                }
            }
            let now = Instant::now();
            if now >= deadline {
                break;
            }
            std::thread::sleep((deadline - now).min(Duration::from_millis(10)));
        }

        let _ = child.kill();
        let status = child
            .wait()
            .map_err(|error| SupervisorError::Io(error.kind()))?;
        self.last_shutdown = Some(ShutdownOutcome::from_status(status, true));
        Ok(())
    }
}

impl Drop for ChildGuard {
    fn drop(&mut self) {
        let _ = self.terminate();
    }
}

fn validate_settings(settings: &SupervisorSettings<'_>) -> Result<(), SupervisorError> {
    if !settings.python.is_absolute() {
        return Err(SupervisorError::InvalidSettings(
            "python interpreter must be an absolute path",
        ));
    }
    if !settings.state_dir.is_absolute() || settings.state_dir == Path::new("/") {
        return Err(SupervisorError::InvalidSettings(
            "state directory must be absolute and not the filesystem root",
        ));
    }
    if settings
        .state_dir
        .components()
        .any(|component| matches!(component, std::path::Component::ParentDir))
    {
        return Err(SupervisorError::InvalidSettings(
            "state directory must not contain parent traversal",
        ));
    }
    if settings.handshake_timeout.is_zero() {
        return Err(SupervisorError::InvalidSettings(
            "handshake timeout must be positive",
        ));
    }
    if settings.shutdown_timeout.is_zero() {
        return Err(SupervisorError::InvalidSettings(
            "shutdown timeout must be positive",
        ));
    }

    validate_secret(settings.provider_api_key)?;
    Ok(())
}

/// Resolve, validate, and (when absent) create the private state root.
///
/// An existing root is never repaired or chmodded in place: it must already be
/// a real directory with exactly mode `0700`, otherwise the launch fails
/// closed before any child is spawned.
fn prepare_state_dir(path: &Path) -> Result<(), SupervisorError> {
    match std::fs::symlink_metadata(path) {
        Ok(metadata) => {
            if metadata.file_type().is_symlink() {
                return Err(SupervisorError::InvalidSettings(
                    "state directory must not be a symlink",
                ));
            }
            let resolved =
                std::fs::canonicalize(path).map_err(|error| SupervisorError::Io(error.kind()))?;
            reject_root_state_dir(&resolved)?;
            return ensure_private_state_dir(&metadata);
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(SupervisorError::Io(error.kind())),
    }

    // The leaf is absent. Resolve the nearest existing ancestor before
    // creating anything, so a parent symlink to `/` cannot redirect creation
    // outside the caller's tree.
    let ancestor = nearest_existing_ancestor(path)?;
    let resolved_ancestor =
        std::fs::canonicalize(&ancestor).map_err(|error| SupervisorError::Io(error.kind()))?;
    if resolved_ancestor == Path::new("/") {
        return Err(SupervisorError::InvalidSettings(
            "state directory would be created directly under the filesystem root",
        ));
    }

    DirBuilder::new()
        .recursive(true)
        .mode(STATE_DIR_MODE)
        .create(path)
        .map_err(|error| SupervisorError::Io(error.kind()))?;

    let metadata =
        std::fs::symlink_metadata(path).map_err(|error| SupervisorError::Io(error.kind()))?;
    if metadata.file_type().is_symlink() {
        return Err(SupervisorError::InvalidSettings(
            "state directory must not be a symlink",
        ));
    }
    let resolved =
        std::fs::canonicalize(path).map_err(|error| SupervisorError::Io(error.kind()))?;
    reject_root_state_dir(&resolved)?;
    ensure_private_state_dir(&metadata)
}

fn nearest_existing_ancestor(path: &Path) -> Result<PathBuf, SupervisorError> {
    let mut candidate = path.parent();
    while let Some(ancestor) = candidate {
        match std::fs::symlink_metadata(ancestor) {
            Ok(_) => return Ok(ancestor.to_path_buf()),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                candidate = ancestor.parent();
            }
            Err(error) => return Err(SupervisorError::Io(error.kind())),
        }
    }
    Err(SupervisorError::InvalidSettings(
        "state directory has no existing ancestor",
    ))
}

fn reject_root_state_dir(resolved: &Path) -> Result<(), SupervisorError> {
    if resolved == Path::new("/") {
        return Err(SupervisorError::InvalidSettings(
            "state directory must not resolve to the filesystem root",
        ));
    }
    Ok(())
}

fn ensure_private_state_dir(metadata: &Metadata) -> Result<(), SupervisorError> {
    if !metadata.is_dir() {
        return Err(SupervisorError::InvalidSettings(
            "state directory must be a directory",
        ));
    }
    if metadata.mode() & 0o7777 != STATE_DIR_MODE {
        return Err(SupervisorError::InvalidSettings(
            "state directory must have private 0700 permissions",
        ));
    }
    Ok(())
}

fn validate_secret(value: &str) -> Result<(), SupervisorError> {
    if value.is_empty() {
        return Err(SupervisorError::InvalidSettings("secret must not be empty"));
    }
    if value.len() > SECRET_MAX_BYTES {
        return Err(SupervisorError::InvalidSettings(
            "secret exceeds 1024 bytes",
        ));
    }
    if value.chars().any(char::is_control) {
        return Err(SupervisorError::InvalidSettings(
            "secret contains control characters",
        ));
    }
    Ok(())
}

fn random_nonce() -> Result<String, SupervisorError> {
    random_hex::<16>()
}

fn random_hex<const N: usize>() -> Result<String, SupervisorError> {
    let mut bytes = [0_u8; N];
    File::open("/dev/urandom")
        .and_then(|mut source| source.read_exact(&mut bytes))
        .map_err(|_| SupervisorError::EntropyUnavailable)?;
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut value = String::with_capacity(N * 2);
    for byte in bytes {
        value.push(HEX[(byte >> 4) as usize] as char);
        value.push(HEX[(byte & 0x0f) as usize] as char);
    }
    Ok(value)
}

fn build_config(
    settings: &SupervisorSettings<'_>,
    nonce: &str,
    credentials: &LocalCredentials,
) -> Result<Vec<u8>, SupervisorError> {
    let mut secrets = Map::new();
    secrets.insert(
        "KELP_AUTH_TOKEN".to_owned(),
        Value::String(credentials.owner_token.clone()),
    );
    secrets.insert(
        "KELP_PROVIDER_API_KEY".to_owned(),
        Value::String(settings.provider_api_key.to_owned()),
    );
    secrets.insert(
        "KELP_CSRF_TOKEN".to_owned(),
        Value::String(credentials.csrf_token.clone()),
    );
    secrets.insert(
        "KELP_SESSION_SECRET".to_owned(),
        Value::String(credentials.session_secret.clone()),
    );

    let mut root = Map::new();
    root.insert("v".to_owned(), Value::from(PROTOCOL_VERSION));
    root.insert("type".to_owned(), Value::String("config".to_owned()));
    root.insert("nonce".to_owned(), Value::String(nonce.to_owned()));
    root.insert("secrets".to_owned(), Value::Object(secrets));

    let bytes =
        serde_json::to_vec(&Value::Object(root)).map_err(|_| SupervisorError::ConfigTooLarge)?;
    if bytes.is_empty() || bytes.len() > CONFIG_MAX_BYTES {
        return Err(SupervisorError::ConfigTooLarge);
    }
    Ok(bytes)
}

fn spawn_command(
    settings: &SupervisorSettings<'_>,
    mut command: Command,
) -> Result<Supervisor, SupervisorError> {
    validate_settings(settings)?;
    prepare_state_dir(settings.state_dir)?;
    let credentials = LocalCredentials::generate()?;
    let nonce = random_nonce()?;
    let config = build_config(settings, &nonce, &credentials)?;

    let (parent, child_end) =
        UnixStream::pair().map_err(|error| SupervisorError::Io(error.kind()))?;

    command
        .env_clear()
        .env("KELP_LOCAL_STATE_DIR", settings.state_dir)
        .stdin(Stdio::from(OwnedFd::from(child_end)))
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .current_dir(settings.state_dir);
    if let Some(value) = settings
        .provider_base_url
        .map(str::trim)
        .filter(|value| !value.is_empty())
    {
        command.env("KELP_PROVIDER_BASE_URL", value);
    }
    if let Some(value) = settings
        .primary_model
        .map(str::trim)
        .filter(|value| !value.is_empty())
    {
        command.env("KELP_PRIMARY_MODEL", value);
    }

    let child = command
        .spawn()
        .map_err(|error| SupervisorError::Spawn(error.kind()))?;
    // The child owns fd 0 now; do not let `Command` outlive the spawn.
    drop(command);

    // Ownership exists before any handshake byte is written, so every failure
    // below closes the channel and reaps this child through `Drop`.
    let mut guard = ChildGuard {
        child: Some(child),
        channel: Some(parent),
        shutdown_timeout: settings.shutdown_timeout,
        last_shutdown: None,
    };
    let endpoint = guard.handshake(&config, &nonce, settings.handshake_timeout)?;
    Ok(Supervisor {
        guard,
        endpoint,
        credentials,
    })
}

fn exchange(
    channel: &mut UnixStream,
    config: &[u8],
    nonce: &str,
    timeout: Duration,
) -> Result<LocalEndpoint, SupervisorError> {
    let deadline = Instant::now()
        .checked_add(timeout)
        .ok_or(SupervisorError::Timeout)?;

    write_frame(channel, config, deadline)?;
    let payload = read_status_frame(channel, deadline)?;
    match parse_status(&payload, nonce)? {
        Status::Ready { port } => Ok(LocalEndpoint { port }),
        Status::Error(code) => Err(SupervisorError::Sidecar(code)),
    }
}

fn remaining(deadline: Instant) -> Result<Duration, SupervisorError> {
    let now = Instant::now();
    if now >= deadline {
        Err(SupervisorError::Timeout)
    } else {
        Ok(deadline - now)
    }
}

fn write_frame(
    channel: &mut UnixStream,
    payload: &[u8],
    deadline: Instant,
) -> Result<(), SupervisorError> {
    if payload.is_empty() || payload.len() > CONFIG_MAX_BYTES {
        return Err(SupervisorError::ConfigTooLarge);
    }
    let length = (payload.len() as u32).to_be_bytes();
    write_all(channel, &length, deadline)?;
    write_all(channel, payload, deadline)
}

fn write_all(
    channel: &mut UnixStream,
    mut data: &[u8],
    deadline: Instant,
) -> Result<(), SupervisorError> {
    while !data.is_empty() {
        let remaining = remaining(deadline)?;
        channel
            .set_write_timeout(Some(remaining))
            .map_err(|error| SupervisorError::Io(error.kind()))?;
        match channel.write(data) {
            Ok(0) => return Err(SupervisorError::ChannelClosed),
            Ok(written) => data = &data[written..],
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error)
                if error.kind() == io::ErrorKind::WouldBlock
                    || error.kind() == io::ErrorKind::TimedOut =>
            {
                return Err(SupervisorError::Timeout);
            }
            Err(error) => return Err(SupervisorError::Io(error.kind())),
        }
    }
    Ok(())
}

fn read_exact(
    channel: &mut UnixStream,
    buffer: &mut [u8],
    deadline: Instant,
) -> Result<(), SupervisorError> {
    let mut offset = 0;
    while offset < buffer.len() {
        let remaining = remaining(deadline)?;
        channel
            .set_read_timeout(Some(remaining))
            .map_err(|error| SupervisorError::Io(error.kind()))?;
        match channel.read(&mut buffer[offset..]) {
            Ok(0) => return Err(SupervisorError::ChannelClosed),
            Ok(read) => offset += read,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error)
                if error.kind() == io::ErrorKind::WouldBlock
                    || error.kind() == io::ErrorKind::TimedOut =>
            {
                return Err(SupervisorError::Timeout);
            }
            Err(error) => return Err(SupervisorError::Io(error.kind())),
        }
    }
    Ok(())
}

fn read_status_frame(
    channel: &mut UnixStream,
    deadline: Instant,
) -> Result<Vec<u8>, SupervisorError> {
    let mut header = [0_u8; 4];
    read_exact(channel, &mut header, deadline)?;
    let size = u32::from_be_bytes(header) as usize;
    if size == 0 || size > STATUS_MAX_BYTES {
        return Err(SupervisorError::BadStatusLength);
    }
    let mut payload = vec![0_u8; size];
    read_exact(channel, &mut payload, deadline)?;
    Ok(payload)
}

enum Status {
    Ready { port: u16 },
    Error(SidecarCode),
}

fn parse_status(payload: &[u8], expected_nonce: &str) -> Result<Status, SupervisorError> {
    let text = std::str::from_utf8(payload).map_err(|_| SupervisorError::InvalidStatusUtf8)?;
    // Serde's derived struct visitor also understands positional sequences;
    // v1 has only object-shaped status messages.
    let trimmed = text.trim_start_matches([' ', '\t', '\r', '\n']);
    if trimmed.starts_with('[') {
        return Err(SupervisorError::MalformedStatus);
    }
    let raw: RawStatus = match serde_json::from_str(text) {
        Ok(raw) => raw,
        Err(error) if error.is_data() => return Err(SupervisorError::MalformedStatus),
        Err(_) => return Err(SupervisorError::InvalidStatusJson),
    };

    if raw.v != PROTOCOL_VERSION {
        return Err(SupervisorError::MalformedStatus);
    }

    match raw.kind.as_str() {
        "ready" => {
            if !matches!(raw.code, Field::Absent) {
                return Err(SupervisorError::MalformedStatus);
            }
            let nonce = match raw.nonce {
                Field::Value(nonce) => nonce,
                Field::Absent | Field::Null => return Err(SupervisorError::MalformedStatus),
            };
            if nonce != expected_nonce {
                return Err(SupervisorError::NonceMismatch);
            }
            let host = match raw.host {
                Field::Value(host) => host,
                Field::Absent | Field::Null => return Err(SupervisorError::MalformedStatus),
            };
            if host != LOOPBACK_HOST {
                return Err(SupervisorError::HostNotLoopback);
            }
            let port = match raw.port {
                Field::Value(port) => port,
                Field::Absent => return Err(SupervisorError::PortOutOfRange),
                Field::Null => return Err(SupervisorError::MalformedStatus),
            };
            if !(1..=u64::from(u16::MAX)).contains(&port) {
                return Err(SupervisorError::PortOutOfRange);
            }
            Ok(Status::Ready { port: port as u16 })
        }
        "error" => {
            if !matches!(raw.host, Field::Absent) || !matches!(raw.port, Field::Absent) {
                return Err(SupervisorError::MalformedStatus);
            }
            match raw.nonce {
                Field::Null => {}
                Field::Value(nonce) if nonce == expected_nonce => {}
                Field::Value(_) => return Err(SupervisorError::NonceMismatch),
                Field::Absent => return Err(SupervisorError::MalformedStatus),
            }
            let code = match raw.code {
                Field::Value(code) => code,
                Field::Absent | Field::Null => return Err(SupervisorError::MalformedStatus),
            };
            let code = SidecarCode::parse(&code).ok_or(SupervisorError::UnknownErrorCode)?;
            Ok(Status::Error(code))
        }
        _ => Err(SupervisorError::MalformedStatus),
    }
}

/// Strict typed fields for the two flat status messages. Serde's derive
/// rejects duplicate and unknown fields; wrong-variant fields are rejected
/// explicitly below. `nonce` is required, while [`Field`] distinguishes a
/// present `null` from a missing field so a wrong-variant null cannot be read
/// as absence.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawStatus {
    v: u32,
    #[serde(rename = "type")]
    kind: String,
    nonce: Field<String>,
    #[serde(default)]
    host: Field<String>,
    #[serde(default)]
    port: Field<u64>,
    #[serde(default)]
    code: Field<String>,
}

/// Whether a status key was absent, explicitly `null`, or carried a value.
///
/// Serde's `Option` collapses absence and `null`; that would accept
/// `"code": null` in a ready frame and `"host": null`/`"port": null` in an
/// error frame. The derive only calls this implementation for a key that is
/// actually present, so `Absent` stays reserved for `#[serde(default)]`.
enum Field<T> {
    Absent,
    Null,
    Value(T),
}

impl<T> Default for Field<T> {
    fn default() -> Self {
        Self::Absent
    }
}

impl<'de> Deserialize<'de> for Field<String> {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        deserializer.deserialize_any(StringFieldVisitor)
    }
}

struct StringFieldVisitor;

impl<'de> Visitor<'de> for StringFieldVisitor {
    type Value = Field<String>;

    fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("null or a string")
    }

    fn visit_unit<E>(self) -> Result<Self::Value, E>
    where
        E: de::Error,
    {
        Ok(Field::Null)
    }

    fn visit_str<E>(self, value: &str) -> Result<Self::Value, E>
    where
        E: de::Error,
    {
        Ok(Field::Value(value.to_owned()))
    }

    fn visit_string<E>(self, value: String) -> Result<Self::Value, E>
    where
        E: de::Error,
    {
        Ok(Field::Value(value))
    }
}

impl<'de> Deserialize<'de> for Field<u64> {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        deserializer.deserialize_any(U64FieldVisitor)
    }
}

struct U64FieldVisitor;

impl<'de> Visitor<'de> for U64FieldVisitor {
    type Value = Field<u64>;

    fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("null or a non-negative integer")
    }

    fn visit_unit<E>(self) -> Result<Self::Value, E>
    where
        E: de::Error,
    {
        Ok(Field::Null)
    }

    fn visit_u64<E>(self, value: u64) -> Result<Self::Value, E>
    where
        E: de::Error,
    {
        Ok(Field::Value(value))
    }
}

#[cfg(test)]
mod tests;

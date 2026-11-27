//! Explicit desktop backend selection and the Linux Local sidecar binding.
//!
//! The default desktop path is hosted. `KELP_DESKTOP_BACKEND` may name
//! `hosted` (or be absent) to keep that path, or `local` to start the sealed
//! loopback sidecar. Every other value, an unreadable value, or `local` in a
//! build without the non-default `local-dev` feature on Linux fails closed
//! before Tauri is built and before any hosted client exists.
//!
//! Local configuration is read only from explicit variables:
//!
//! * `KELP_LOCAL_PYTHON` — trusted absolute interpreter path. A virtualenv
//!   launcher is executed as written and never canonicalized.
//! * `KELP_LOCAL_STATE_DIR` — disposable absolute state root. The supervisor
//!   requires or creates it with private `0700` permissions.
//! * `KELP_LOCAL_PROVIDER_BASE_URL` — exactly
//!   `http://127.0.0.1:<nonzero-port>/v1`; no other origin, path, query,
//!   fragment, userinfo, or redirect surface is accepted.
//! * `KELP_LOCAL_PRIMARY_MODEL` — optional nonsecret model label.
//!
//! The Local provider credential is a fixed synthetic fixture key owned by
//! this module. No provider credential is read from the environment, logged,
//! or shown. The owner, CSRF, and session credentials stay Rust-owned: they
//! are minted by the supervisor, handed to the transport, and never cross the
//! Tauri command surface.
//!
//! This is a binding entry, not a sandbox. It does not create OS confinement:
//! the child runs as the desktop user, the socketpair is the control channel,
//! and the loopback HTTP guard is the only request boundary. The bind to
//! `127.0.0.1:<port>` is not an atomic peer-identity check, so a port reuse
//! race is accepted for this development path rather than papered over with a
//! protocol change.

#![cfg_attr(
    not(all(any(test, feature = "local-dev"), target_os = "linux")),
    allow(dead_code)
)]

use std::ffi::OsStr;
use std::fmt;
use std::path::{Component, Path, PathBuf};
use std::time::Duration;

use reqwest::Url;

#[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
use std::num::NonZeroU16;
#[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
use std::sync::atomic::{AtomicBool, Ordering};
#[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
use std::sync::{Arc, Mutex};

#[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
use crate::local_supervisor::{ShutdownOutcome, Supervisor, SupervisorSettings};
#[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
use crate::local_transport::LocalTransport;

/// The only environment selector for the desktop backend.
pub(crate) const BACKEND_ENV: &str = "KELP_DESKTOP_BACKEND";
/// Trusted absolute interpreter path.
#[cfg_attr(not(feature = "local-dev"), allow(dead_code))]
pub(crate) const PYTHON_ENV: &str = "KELP_LOCAL_PYTHON";
/// Disposable absolute state root.
#[cfg_attr(not(feature = "local-dev"), allow(dead_code))]
pub(crate) const STATE_DIR_ENV: &str = "KELP_LOCAL_STATE_DIR";
/// Required loopback v1 provider endpoint.
#[cfg_attr(not(feature = "local-dev"), allow(dead_code))]
pub(crate) const PROVIDER_BASE_URL_ENV: &str = "KELP_LOCAL_PROVIDER_BASE_URL";
/// Optional nonsecret model label.
#[cfg_attr(not(feature = "local-dev"), allow(dead_code))]
pub(crate) const PRIMARY_MODEL_ENV: &str = "KELP_LOCAL_PRIMARY_MODEL";

/// Fixed synthetic provider key for the Local development fixture.
///
/// This module never reads `KELP_PROVIDER_API_KEY` or any other provider
/// credential. Local development points the sidecar at a fake provider; a real
/// provider credential has no path into this build.
pub(crate) const LOCAL_FIXTURE_PROVIDER_KEY: &str = "kelp-local-provider-fixture-key";

/// How the desktop binary was asked to run.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum DesktopBackend {
    Hosted,
    Local,
}

impl DesktopBackend {
    /// The literal the bridge reports through `bridge_backend`.
    #[cfg_attr(not(test), allow(dead_code))]
    pub(crate) fn name(self) -> &'static str {
        match self {
            Self::Hosted => "hosted",
            Self::Local => "local",
        }
    }
}

/// A fixed, nonsecret failure. Display text never includes a configured path,
/// URL, credential, or environment value.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum RuntimeError {
    UnknownBackend,
    LocalUnsupported,
    MissingPython,
    InvalidPython,
    MissingStateDir,
    InvalidStateDir,
    MissingProviderBaseUrl,
    InvalidProviderBaseUrl,
    InvalidPrimaryModel,
    SidecarStart,
    Transport,
    GuardProbe,
}

impl fmt::Display for RuntimeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::UnknownBackend => "KELP_DESKTOP_BACKEND must be \"hosted\" or \"local\"",
            Self::LocalUnsupported => {
                "local backend requires a Linux build with the local-dev feature"
            }
            Self::MissingPython => "KELP_LOCAL_PYTHON must name an absolute interpreter",
            Self::InvalidPython => "KELP_LOCAL_PYTHON must name an absolute interpreter",
            Self::MissingStateDir => "KELP_LOCAL_STATE_DIR must name a private absolute directory",
            Self::InvalidStateDir => "KELP_LOCAL_STATE_DIR must name a private absolute directory",
            Self::MissingProviderBaseUrl => {
                "KELP_LOCAL_PROVIDER_BASE_URL must name http://127.0.0.1:<port>/v1"
            }
            Self::InvalidProviderBaseUrl => {
                "KELP_LOCAL_PROVIDER_BASE_URL must name http://127.0.0.1:<port>/v1"
            }
            Self::InvalidPrimaryModel => "KELP_LOCAL_PRIMARY_MODEL is not a usable model label",
            Self::SidecarStart => "local sidecar could not be started",
            Self::Transport => "local transport could not be built",
            Self::GuardProbe => "local sidecar did not pass guard verification",
        })
    }
}

impl std::error::Error for RuntimeError {}

/// The unvalidated backend request.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum BackendKind {
    Hosted,
    Local,
}

/// Parse the explicit selector without touching process state.
///
/// An absent variable is the hosted default. Only the exact lowercase words
/// are accepted; empty, padded, non-UTF-8, or unknown values are errors.
fn parse_backend(value: Option<&OsStr>) -> Result<BackendKind, RuntimeError> {
    let Some(value) = value else {
        return Ok(BackendKind::Hosted);
    };
    match value.to_str() {
        Some("hosted") => Ok(BackendKind::Hosted),
        Some("local") => Ok(BackendKind::Local),
        _ => Err(RuntimeError::UnknownBackend),
    }
}

/// Resolve a parsed request against whether this binary actually compiled the
/// Local path. Split from environment access so tests need no env mutation.
fn resolve_backend(
    kind: BackendKind,
    local_compiled: bool,
) -> Result<DesktopBackend, RuntimeError> {
    match kind {
        BackendKind::Hosted => Ok(DesktopBackend::Hosted),
        BackendKind::Local if local_compiled => Ok(DesktopBackend::Local),
        BackendKind::Local => Err(RuntimeError::LocalUnsupported),
    }
}

/// Read `KELP_DESKTOP_BACKEND` once with `var_os`.
pub(crate) fn select_backend() -> Result<DesktopBackend, RuntimeError> {
    let value = std::env::var_os(BACKEND_ENV);
    let kind = parse_backend(value.as_deref())?;
    resolve_backend(kind, cfg!(all(feature = "local-dev", target_os = "linux")))
}

/// Validated Local launch configuration. Holds no credential.
#[derive(Debug)]
pub(crate) struct LocalConfig {
    pub(crate) python: PathBuf,
    pub(crate) state_dir: PathBuf,
    pub(crate) provider_base_url: String,
    pub(crate) primary_model: Option<String>,
    pub(crate) handshake_timeout: Duration,
    pub(crate) shutdown_timeout: Duration,
}

impl LocalConfig {
    /// Read the four explicit Local variables with `var_os`.
    #[cfg_attr(not(feature = "local-dev"), allow(dead_code))]
    pub(crate) fn from_env() -> Result<Self, RuntimeError> {
        let python = std::env::var_os(PYTHON_ENV);
        let state_dir = std::env::var_os(STATE_DIR_ENV);
        let provider = std::env::var_os(PROVIDER_BASE_URL_ENV);
        let model = std::env::var_os(PRIMARY_MODEL_ENV);
        Self::from_values(
            python.as_deref(),
            state_dir.as_deref(),
            provider.as_deref(),
            model.as_deref(),
        )
    }

    /// Validate explicit values. No environment is consulted, so tests can
    /// exercise every branch without races.
    pub(crate) fn from_values(
        python: Option<&OsStr>,
        state_dir: Option<&OsStr>,
        provider_base_url: Option<&OsStr>,
        primary_model: Option<&OsStr>,
    ) -> Result<Self, RuntimeError> {
        let python = python.ok_or(RuntimeError::MissingPython)?;
        if python.is_empty() {
            return Err(RuntimeError::MissingPython);
        }
        let python = PathBuf::from(python);
        if !python.is_absolute() {
            return Err(RuntimeError::InvalidPython);
        }

        let state_dir = state_dir.ok_or(RuntimeError::MissingStateDir)?;
        if state_dir.is_empty() {
            return Err(RuntimeError::MissingStateDir);
        }
        let state_dir = PathBuf::from(state_dir);
        validate_state_dir(&state_dir)?;

        let provider_base_url = provider_base_url.ok_or(RuntimeError::MissingProviderBaseUrl)?;
        let provider_base_url = provider_base_url
            .to_str()
            .ok_or(RuntimeError::InvalidProviderBaseUrl)?;
        validate_provider_base_url(provider_base_url)?;

        let primary_model = match primary_model {
            None => None,
            Some(value) => {
                let value = value.to_str().ok_or(RuntimeError::InvalidPrimaryModel)?;
                validate_primary_model(value)?
            }
        };

        Ok(Self {
            python,
            state_dir,
            provider_base_url: provider_base_url.to_owned(),
            primary_model,
            handshake_timeout: Duration::from_secs(30),
            shutdown_timeout: Duration::from_secs(2),
        })
    }

    /// Supervisor settings for this configuration. The provider key is always
    /// the fixed synthetic fixture value, never process environment.
    #[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
    fn settings(&self) -> SupervisorSettings<'_> {
        let mut settings =
            SupervisorSettings::new(&self.python, &self.state_dir, LOCAL_FIXTURE_PROVIDER_KEY);
        settings.provider_base_url = Some(self.provider_base_url.as_str());
        settings.primary_model = self.primary_model.as_deref();
        settings.handshake_timeout = self.handshake_timeout;
        settings.shutdown_timeout = self.shutdown_timeout;
        settings
    }
}

/// Reject anything that is not an absolute, non-root, traversal-free root.
/// The supervisor repeats the authoritative private-mode check.
fn validate_state_dir(path: &Path) -> Result<(), RuntimeError> {
    if !path.is_absolute() || path == Path::new("/") {
        return Err(RuntimeError::InvalidStateDir);
    }
    if path
        .components()
        .any(|component| matches!(component, Component::ParentDir))
    {
        return Err(RuntimeError::InvalidStateDir);
    }
    Ok(())
}

/// Accept exactly the canonical loopback v1 fixture endpoint.
///
/// The raw string must equal the re-serialized canonical form, so alternate
/// IPv4 spellings, uppercase hosts, default ports, duplicate slashes, and
/// encoded forms are refused even when the URL parser would normalize them.
pub(crate) fn validate_provider_base_url(value: &str) -> Result<(), RuntimeError> {
    let url = Url::parse(value).map_err(|_| RuntimeError::InvalidProviderBaseUrl)?;
    if url.scheme() != "http"
        || url.host_str() != Some("127.0.0.1")
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || url.path() != "/v1"
    {
        return Err(RuntimeError::InvalidProviderBaseUrl);
    }
    let port = url
        .port()
        .filter(|port| *port != 0)
        .ok_or(RuntimeError::InvalidProviderBaseUrl)?;
    let canonical = format!("http://127.0.0.1:{port}/v1");
    if value != canonical {
        return Err(RuntimeError::InvalidProviderBaseUrl);
    }
    Ok(())
}

/// Optional model label: blank is absent; otherwise a bounded printable ASCII
/// string with no surrounding whitespace or control bytes.
fn validate_primary_model(value: &str) -> Result<Option<String>, RuntimeError> {
    if value.trim().is_empty() {
        return Ok(None);
    }
    if value != value.trim()
        || value.len() > 200
        || !value.bytes().all(|byte| (0x20..=0x7e).contains(&byte))
    {
        return Err(RuntimeError::InvalidPrimaryModel);
    }
    Ok(Some(value.to_owned()))
}

/// The owned sidecar child. `shutdown` is idempotent and `Drop` repeats it.
#[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
pub(crate) struct LocalRuntime {
    child: Mutex<Option<Supervisor>>,
    closed: Arc<AtomicBool>,
}

#[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
impl LocalRuntime {
    /// Best-effort liveness flag shared with the bridge. It is set on explicit
    /// shutdown only; a spontaneous child death is discovered by the failed
    /// request itself.
    #[cfg_attr(not(feature = "local-dev"), allow(dead_code))]
    pub(crate) fn closed_flag(&self) -> Arc<AtomicBool> {
        Arc::clone(&self.closed)
    }

    /// Mark the child closed without reaping yet. `RunEvent::Exit` calls this
    /// before cancelling registrations so a new stream cannot be inserted
    /// between the drain and the child teardown.
    #[cfg_attr(not(feature = "local-dev"), allow(dead_code))]
    pub(crate) fn mark_closed(&self) {
        self.closed.store(true, Ordering::SeqCst);
    }

    /// Close the control channel, wait a bounded interval, then kill and reap.
    /// The first call wins; later calls are no-ops.
    pub(crate) fn shutdown(&self) -> Option<ShutdownOutcome> {
        self.mark_closed();
        let mut slot = self.child.lock().ok()?;
        let mut supervisor = slot.take()?;
        let _ = supervisor.shutdown();
        supervisor.shutdown_outcome()
    }
}

#[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
impl Drop for LocalRuntime {
    fn drop(&mut self) {
        let _ = self.shutdown();
    }
}

/// A ready Local sidecar: owned child plus its loopback transport.
#[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
pub(crate) struct LocalSession {
    pub(crate) runtime: LocalRuntime,
    pub(crate) transport: LocalTransport,
}

#[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
impl LocalSession {
    /// Spawn the sidecar, bind the transport to its reported port, and require
    /// the bounded guard probe to pass. Every failure path drops the owned
    /// child, which closes the channel and reaps it.
    pub(crate) fn start(config: &LocalConfig) -> Result<Self, RuntimeError> {
        let settings = config.settings();
        let mut supervisor =
            Supervisor::spawn(&settings).map_err(|_| RuntimeError::SidecarStart)?;

        let port = match NonZeroU16::new(supervisor.endpoint().port()) {
            Some(port) => port,
            None => {
                let _ = supervisor.shutdown();
                return Err(RuntimeError::GuardProbe);
            }
        };
        let transport = match LocalTransport::new(
            port,
            supervisor.credentials().owner_token(),
            supervisor.credentials().csrf_token(),
        ) {
            Ok(transport) => transport,
            Err(_) => {
                let _ = supervisor.shutdown();
                return Err(RuntimeError::Transport);
            }
        };

        match verify_guard(&transport) {
            Ok(()) => {}
            Err(error) => {
                let _ = supervisor.shutdown();
                return Err(error);
            }
        }

        Ok(Self {
            runtime: LocalRuntime {
                child: Mutex::new(Some(supervisor)),
                closed: Arc::new(AtomicBool::new(false)),
            },
            transport,
        })
    }
}

/// Run the read-only guard probe on Tauri's global async runtime. Tauri serves
/// the IPC commands on that same runtime, so the transport's pooled connection
/// stays valid after startup; no short-lived runtime can invalidate it.
#[cfg(all(any(test, feature = "local-dev"), target_os = "linux"))]
fn verify_guard(transport: &LocalTransport) -> Result<(), RuntimeError> {
    tauri::async_runtime::block_on(transport.verify_guard()).map_err(|_| RuntimeError::GuardProbe)
}

#[cfg(all(test, target_os = "linux"))]
mod tests;

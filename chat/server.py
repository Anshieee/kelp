"""Single-process chat server entrypoint (KEL-11, M2-A).

``python -m chat.server`` turns the KEL-8 slice into one deployable process:

* read and validate environment configuration before touching state;
* take a single-process lock on the chat database;
* build ``KernelConfig``, ``ChatStore``, ``ArtifactRegistry``,
  ``SkillRegistry``, ``ApprovalStore``, ``UsageStore``, ``ChatService``, and
  ``ChatAPI``;
* run boot recovery exactly once, before the socket starts accepting;
* serve the authenticated ``/api/*`` surface until ``SIGTERM`` / ``SIGINT``;
* cancel and settle in-flight turns as ``interrupted`` (§10.4), then close the
  HTTP server, event loop, database, and lock.

``--check`` validates the same configuration and skill registry without
opening the database, binding a port, or creating any directory: the tree is
left byte-identical. Errors are one line, never include a secret value, and
exit non-zero so a supervisor can fail fast. The process refuses to start when
another live process holds the database lock.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import os
import signal
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from kernel import JsonlUsageStore, KernelConfig
from kernel.config import API_KEY_ENV, BASE_URL_ENV, TENANT_SECRETS_FILE_ENV
from kernel.providers import OpenAICompatibleClient, build_provider
from kernel.secrets import (
    EnvSecretResolver,
    MissingSecret,
    SecretFileError,
    TenantSecretResolver,
)
from kernel.usage import SpendCaps, SpendGuard
from openshell.approvals import ApprovalStore
from openshell.audit import AuditLog, SecretScrubber
from openshell.egress_ledger import EgressLedger
from skills.errors import SkillManifestError
from skills.loader import SkillRegistry

from .api import (
    SESSION_SECRET_ENV,
    SESSION_SECURE_ENV,
    AuthFailureTracker,
    ChatAPI,
    HealthProbe,
    SessionStore,
    make_server,
)
from .checkout import CheckoutAPI
from .artifacts import ArtifactRegistry
from .logging import configure_logging, log
from .metrics import LoggedUsageStore, Metrics
from .proactive import ProactiveConfig, ProactiveRunner
from .recovery import RecoveryReport, settle_interrupted
from .retention import RetentionMaintenance, RetentionPolicy
from .scheduler import (
    DEFAULT_GRACE_SECONDS,
    DEFAULT_TICK_SECONDS,
    ENABLED_ENV,
    GRACE_ENV,
    TICK_ENV,
    ScheduleStore,
    SchedulerRuntime,
)
from .credentials import CredentialBroker, validate_env as validate_credential_env
from .service import ChatService
from .store import ChatStore
from .tenancy import OWNER_TENANT_ID, StaticTenantResolver
from .wallet import WalletAPI
from payments.razorpay_checkout import RazorpayCheckout
from payments.razorpay_gateway import RazorpayWallet
from payments.wallet import WalletLedger

__all__ = [
    "Application",
    "ConfigError",
    "DatabaseLock",
    "DatabaseLocked",
    "ServerConfig",
    "ServerError",
    "build_app",
    "main",
    "serve",
]

DB_PATH_ENV = "KELP_DB_PATH"
ARTIFACT_DIR_ENV = "KELP_ARTIFACT_DIR"
AUDIT_DIR_ENV = "KELP_AUDIT_DIR"
USAGE_DIR_ENV = "KELP_USAGE_DIR"
PORT_ENV = "KELP_PORT"
HOST_ENV = "KELP_HOST"
AUTH_TOKEN_ENV = "KELP_AUTH_TOKEN"
CSRF_TOKEN_ENV = "KELP_CSRF_TOKEN"
WORKSPACE_ROOT_ENV = "KELP_WORKSPACE_ROOT"
SKILL_ROOT_ENV = "KELP_SKILL_ROOT"
LOG_LEVEL_ENV = "KELP_LOG_LEVEL"

DEFAULT_DB_PATH = "var/chat.db"
DEFAULT_ARTIFACT_DIR = "var/workspace/var/artifacts"
DEFAULT_AUDIT_DIR = "var/audit"
DEFAULT_USAGE_DIR = "var/usage"
DEFAULT_WORKSPACE_ROOT = "var/workspace"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
_AUDIT_FILENAME = "audit.jsonl"
_USAGE_FILENAME = "usage.jsonl"
# Generous enough for approval unwinding, short enough to never hang a stop.
_TURN_SHUTDOWN_SECONDS = 10.0

#: The one module-level patchable provider symbol the service contract names.
#: Replacing this class changes the default runtime provider; ``build_app``'s
#: ``provider=`` keyword remains the per-call injection point.
ProviderClient = OpenAICompatibleClient


class ServerError(Exception):
    """A startup/configuration failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str, *, exit_code: int = 2):
        self.code = code
        self.message = message
        self.exit_code = exit_code
        super().__init__(message)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class ConfigError(ServerError):
    """Invalid or incomplete environment configuration."""


class DatabaseLocked(ServerError):
    """Another live process already owns this database."""

    def __init__(self, message: str):
        super().__init__("database_locked", message, exit_code=3)


def _text(source: Mapping[str, str], name: str, default: str = "") -> str:
    raw = source.get(name)
    return raw.strip() if isinstance(raw, str) else default


def _path(source: Mapping[str, str], name: str, default: str) -> Path:
    raw = _text(source, name) or default
    return Path(raw).expanduser().resolve()


def _parse_port(raw: str) -> int:
    if raw == "":
        return DEFAULT_PORT
    try:
        port = int(raw, 10)
    except ValueError:
        raise ConfigError(
            "invalid_config", f"{PORT_ENV} must be an integer between 0 and 65535."
        ) from None
    if not 0 <= port <= 65535:
        raise ConfigError(
            "invalid_config", f"{PORT_ENV} must be an integer between 0 and 65535."
        )
    return port


def _flag(source: Mapping[str, str], name: str, default: bool) -> bool:
    raw = source.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"", "0", "false", "no", "off", "disabled"}


def _env_int_value(
    source: Mapping[str, str], name: str, default: int, *, minimum: int
) -> int:
    """Read one bounded integer from the same mapping the rest of boot uses.

    ``SchedulerRuntime`` also reads its knobs from ``os.environ`` when they are
    not passed; passing them explicitly keeps ``build_app(environ=...)`` and
    ``main(env=...)`` honest instead of silently ignoring the caller's map.
    """

    raw = _text(source, name)
    if not raw:
        return default
    try:
        value = int(raw, 10)
    except ValueError:
        raise ConfigError("invalid_config", f"{name} must be an integer.") from None
    if value < minimum:
        raise ConfigError("invalid_config", f"{name} must be >= {minimum}.")
    return value


def _mkdir_private(directory: Path) -> None:
    """Create ``directory`` (and missing parents), setting ``0o700`` only there.

    Directories that already exist keep their own mode: the server adopts a
    pre-existing layout, it does not re-permission the owner's filesystem.
    """

    missing: list[Path] = []
    current = directory
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:  # pragma: no cover - filesystem root
            break
        current = parent
    directory.mkdir(parents=True, exist_ok=True)
    for created in reversed(missing):
        try:
            os.chmod(created, 0o700)
        except OSError:  # pragma: no cover - platform dependent
            pass


@dataclass(frozen=True)
class ServerConfig:
    """Validated process configuration. Secrets are excluded from ``repr``."""

    db_path: Path = Path(DEFAULT_DB_PATH)
    artifact_dir: Path = Path(DEFAULT_ARTIFACT_DIR)
    audit_dir: Path = Path(DEFAULT_AUDIT_DIR)
    usage_dir: Path = Path(DEFAULT_USAGE_DIR)
    workspace_root: Path = Path(DEFAULT_WORKSPACE_ROOT)
    skill_root: Path | None = None
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    auth_token: str = field(default="", repr=False)
    csrf_token: str | None = field(default=None, repr=False)
    kernel: KernelConfig = field(default_factory=KernelConfig, repr=False)
    spend_caps: SpendCaps = field(default_factory=SpendCaps, repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ServerConfig":
        source = os.environ if env is None else env
        try:
            validate_credential_env(source)
        except ValueError as exc:
            raise ConfigError("invalid_credential_config", str(exc)) from None

        try:
            kernel = KernelConfig.from_env(source)
        except ValueError as exc:
            raise ConfigError("invalid_config", str(exc)) from None

        if not kernel.api_key and not kernel.tenant_secrets_file:
            raise ConfigError(
                "missing_api_key",
                f"{API_KEY_ENV} is required; the server will not start without it.",
            )

        if not kernel.base_url:
            raise ConfigError(
                "missing_base_url",
                f"{BASE_URL_ENV} is required; the server will not start without "
                "a model provider endpoint.",
            )

        auth_token = _text(source, AUTH_TOKEN_ENV)
        if not auth_token:
            raise ConfigError(
                "missing_auth_token",
                f"{AUTH_TOKEN_ENV} is required so /api/* stays owner-only.",
            )

        csrf_token = _text(source, CSRF_TOKEN_ENV) or None

        host = _text(source, HOST_ENV) or DEFAULT_HOST
        if any(character.isspace() for character in host):
            raise ConfigError("invalid_config", f"{HOST_ENV} must be a hostname or IP.")

        port = _parse_port(_text(source, PORT_ENV))
        try:
            spend_caps = SpendCaps.from_env(source)
        except ValueError as exc:
            raise ConfigError("invalid_config", str(exc)) from None

        skill_root_text = _text(source, SKILL_ROOT_ENV)
        skill_root = (
            Path(skill_root_text).expanduser().resolve() if skill_root_text else None
        )
        workspace_root = _path(source, WORKSPACE_ROOT_ENV, DEFAULT_WORKSPACE_ROOT)
        artifact_text = _text(source, ARTIFACT_DIR_ENV)
        artifact_dir = (
            Path(artifact_text).expanduser().resolve()
            if artifact_text
            else workspace_root / "var" / "artifacts"
        )

        return cls(
            db_path=_path(source, DB_PATH_ENV, DEFAULT_DB_PATH),
            artifact_dir=artifact_dir,
            audit_dir=_path(source, AUDIT_DIR_ENV, DEFAULT_AUDIT_DIR),
            usage_dir=_path(source, USAGE_DIR_ENV, DEFAULT_USAGE_DIR),
            workspace_root=workspace_root,
            skill_root=skill_root,
            host=host,
            port=port,
            auth_token=auth_token,
            csrf_token=csrf_token,
            kernel=kernel,
            spend_caps=spend_caps,
        )

    @property
    def audit_path(self) -> Path:
        return self.audit_dir / _AUDIT_FILENAME

    @property
    def usage_path(self) -> Path:
        return self.usage_dir / _USAGE_FILENAME

    def check_layout(self) -> None:
        """Validate the state layout without creating or modifying anything.

        ``--check`` is a dry run: a supervisor can call it on a machine where
        the server is not allowed to write yet, and it must leave the tree
        byte-identical.
        """

        if self.db_path.exists() and self.db_path.is_dir():
            raise ConfigError(
                "invalid_config",
                f"{DB_PATH_ENV} points at a directory, not a database file.",
            )
        for directory in self._state_directories():
            if directory.exists() and not directory.is_dir():
                raise ConfigError(
                    "invalid_config",
                    f"{directory} exists but is not a directory.",
                )
        if self.kernel.tenant_secrets_file:
            # Validate the mode and JSON shape without printing anything from
            # the file. ``--check`` must not report a green boot for a secrets
            # file the server would refuse a second later.
            resolver = _secret_resolver(self, os.environ)
            try:
                resolver.resolve(API_KEY_ENV)
            except MissingSecret:
                raise ConfigError(
                    "missing_api_key",
                    f"{API_KEY_ENV} is not present for tenant "
                    f"{self.kernel.tenant_id!r} in {TENANT_SECRETS_FILE_ENV}.",
                ) from None

    def _state_directories(self) -> tuple[Path, ...]:
        return (
            self.db_path.parent,
            self.artifact_dir,
            self.audit_dir,
            self.usage_dir,
            self.workspace_root,
        )

    def ensure_directories(self) -> None:
        """Create the state layout now; tighten only what this call created.

        A directory that already exists is the owner's: its mode is left
        untouched, so a pre-existing ``0755`` directory stays ``0755``. Only
        directories missing at call time are created and given ``0o700``.
        """

        self.check_layout()
        for directory in self._state_directories():
            _mkdir_private(directory)

    def __repr__(self) -> str:
        return (
            "ServerConfig("
            f"db_path={str(self.db_path)!r}, "
            f"artifact_dir={str(self.artifact_dir)!r}, "
            f"audit_dir={str(self.audit_dir)!r}, "
            f"usage_dir={str(self.usage_dir)!r}, "
            f"workspace_root={str(self.workspace_root)!r}, "
            f"host={self.host!r}, port={self.port}, "
            "auth_token_configured=True, "
            f"csrf_token_configured={self.csrf_token is not None}, "
            "tenant_secrets_file_configured="
            f"{self.kernel.tenant_secrets_file is not None}, "
            f"api_key_configured={self.kernel.api_key_configured})"
        )

    __str__ = __repr__


def _secret_resolver(
    config: ServerConfig, source: Mapping[str, str]
) -> EnvSecretResolver | TenantSecretResolver:
    """Build the boot resolver: per-tenant file when configured, else the env.

    ``KELP_TENANT_SECRETS_FILE`` is optional. When it is unset the explicit
    environment mapping is authoritative, so single-tenant behaviour is
    unchanged. A bad file (wrong mode, malformed JSON, missing path) becomes a
    one-line configuration error that names the path and never echoes content.
    """

    path_text = config.kernel.tenant_secrets_file
    if not path_text:
        return EnvSecretResolver(source)
    try:
        return TenantSecretResolver(
            Path(path_text).expanduser().resolve(),
            tenant_id=config.kernel.tenant_id,
        )
    except SecretFileError as exc:
        raise ConfigError("invalid_secret_file", str(exc)) from None


class DatabaseLock:
    """Advisory ``flock`` held for the lifetime of the process.

    SQLite allows two processes to open one file; this lock makes the
    single-writer assumption explicit and fails the second instance with a
    named error instead of letting both write.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.path = self.db_path.with_name(self.db_path.name + ".lock")
        self._fd: int | None = None

    def acquire(self) -> "DatabaseLock":
        _mkdir_private(self.path.parent)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise DatabaseLocked(
                    f"Another KELP server already owns {self.db_path} "
                    f"(lock: {self.path})."
                ) from None
            raise
        self._fd = fd
        try:
            os.chmod(self.path, 0o600)
        except OSError:  # pragma: no cover - platform dependent
            pass
        return self

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @property
    def held(self) -> bool:
        return self._fd is not None


@dataclass
class Application:
    """Everything one server process owns, closed in dependency order."""

    config: ServerConfig
    store: ChatStore
    service: ChatService
    api: ChatAPI
    audit: AuditLog
    lock: DatabaseLock
    recovery: RecoveryReport
    wallet_api: WalletAPI | None = field(default=None, repr=False)
    checkout_api: CheckoutAPI | None = field(default=None, repr=False)
    wallet_ledger: WalletLedger | None = field(default=None, repr=False)
    provider: Any | None = None
    owns_provider: bool = False
    maintenance: RetentionMaintenance | None = field(default=None, repr=False)
    # KEL-27: the cron ticker and the runner it dispatches into. Closed before
    # the store so a fire cannot race the shutdown, and after boot recovery.
    scheduler: SchedulerRuntime | None = field(default=None, repr=False)
    proactive: ProactiveRunner | None = field(default=None, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # No new scheduled work may start once shutdown begins. The ticker
        # stops first, then in-flight turns (including proactive ones) are
        # cancelled and awaited while the loop and the store are still alive,
        # so each one ends ``interrupted`` (§10.4) instead of vanishing.
        if self.scheduler is not None:
            self.scheduler.stop()
        self._close_turns()
        if self.proactive is not None:
            self.proactive.close()
        if self.maintenance is not None:
            self.maintenance.stop()
        if self.owns_provider and self.provider is not None:
            aclose = getattr(self.provider, "aclose", None)
            if aclose is not None:
                try:
                    self.api.runner.run(aclose(), timeout=5.0)
                except Exception:  # noqa: BLE001 - shutdown must not mask exit
                    pass
        payment_wallets = (
            self.wallet_api.wallet if self.wallet_api is not None else None,
            self.checkout_api.checkout.wallet if self.checkout_api is not None else None,
        )
        for payment_wallet in payment_wallets:
            gateway = getattr(payment_wallet, "_gateway", None)
            close = getattr(gateway, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - shutdown must not mask exit
                    pass
        if self.wallet_ledger is not None:
            self.wallet_ledger.close()
        try:
            self.api.close()
        finally:
            try:
                self.store.close()
            finally:
                self.lock.release()

    def _close_turns(self) -> None:
        aclose = getattr(self.service, "aclose", None)
        if aclose is None:  # pragma: no cover - ChatService always has aclose
            return
        try:
            self.api.runner.run(aclose(), timeout=_TURN_SHUTDOWN_SECONDS)
        except Exception:  # noqa: BLE001 - shutdown must not mask exit
            pass

    def __enter__(self) -> "Application":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class _TenantAudit:
    """Audit facade for work that runs outside a request.

    Boot recovery and the scheduler have no credential, but their rows must
    still name a tenant. This adapter supplies the tenant resolved at boot
    (the owner today) unless the caller already supplied one. Everything else
    delegates to the real log, so rotation, scrubbing, and reads are unchanged.
    """

    def __init__(self, audit: AuditLog, tenant_id: str = OWNER_TENANT_ID):
        self._audit = audit
        self._tenant_id = tenant_id

    def record(self, event: str, **fields: Any) -> dict[str, Any]:
        fields.setdefault("tenant_id", self._tenant_id)
        return self._audit.record(event, **fields)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._audit, name)


def _tool_specs(registry: SkillRegistry) -> tuple[dict[str, Any], ...]:
    """Model-facing tool list derived from manifests, in stable order."""

    specs: list[dict[str, Any]] = []
    for skill_name in registry.names():
        skill = registry.get(skill_name)
        parameters = dict(skill.manifest.get("input_schema") or {})
        parameters.pop("$schema", None)
        for tool in skill.tool_names:
            specs.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool,
                        "description": skill.description,
                        "parameters": parameters,
                    },
                }
            )
    return tuple(specs)


def _service_secrets(
    registry: SkillRegistry,
    config: ServerConfig,
    environ: Mapping[str, str],
) -> dict[str, str]:
    """Only declared skill secrets plus this process's own credentials."""

    values: dict[str, str] = {}
    for skill_name in registry.names():
        for name in registry.get(skill_name).declared_secrets:
            value = environ.get(name)
            if isinstance(value, str) and value:
                values[name] = value
    if config.kernel.api_key:
        values["KELP_PROVIDER_API_KEY"] = config.kernel.api_key
    values["KELP_AUTH_TOKEN"] = config.auth_token
    # The per-device CSRF half is an HMAC under a key derived from this value,
    # so an operator echoing the environment must not leak it into a log or
    # audit line either.
    session_secret = environ.get(SESSION_SECRET_ENV)
    if isinstance(session_secret, str) and session_secret.strip():
        values[SESSION_SECRET_ENV] = session_secret.strip()
    if config.csrf_token:
        values["KELP_CSRF_TOKEN"] = config.csrf_token
    # Payment credentials are never included in request metadata, but keeping
    # them in the shared scrubber protects accidental log/audit/metric values.
    for name in (
        "STRIPE_SECRET_KEY",
        "RAZORPAY_KEY_ID",
        "RAZORPAY_KEY_SECRET",
        "RAZORPAY_WEBHOOK_SECRET",
    ):
        value = environ.get(name)
        if isinstance(value, str) and value:
            values[name] = value
    return values


def build_app(
    config: ServerConfig,
    *,
    provider: Any | None = None,
    environ: Mapping[str, str] | None = None,
) -> Application:
    """Build the full runtime and run boot recovery exactly once."""

    source = os.environ if environ is None else environ
    try:
        retention_policy = RetentionPolicy.from_env(source)
    except ValueError as exc:
        raise ConfigError("invalid_config", str(exc)) from None
    config.ensure_directories()
    lock = DatabaseLock(config.db_path).acquire()
    store: ChatStore | None = None
    maintenance: RetentionMaintenance | None = None
    scheduler: SchedulerRuntime | None = None
    proactive: ProactiveRunner | None = None
    credential_broker: CredentialBroker | None = None
    wallet_ledger: WalletLedger | None = None
    owns_provider = provider is None
    runtime_provider = provider
    try:
        registry = SkillRegistry.load(config.skill_root)
        # P4-1: one resolver for the process. A tenant file resolves this
        # tenant's provider key; otherwise the explicit boot mapping does,
        # preserving the single-tenant path. Values never leave this scope
        # except as the provider client's private key field and the scrubber
        # set below.
        secret_resolver = _secret_resolver(config, source)
        secrets = _service_secrets(registry, config, source)
        resolver_values = getattr(secret_resolver, "secret_values", None)
        tenant_values = resolver_values() if callable(resolver_values) else ()
        secret_values = tuple(dict.fromkeys([*secrets.values(), *tenant_values, *[
            source[name] for name in ("KELP_VAULT_KEY", "KELP_BROWSER_WORKER_TOKEN")
            if source.get(name)
        ]]))
        # Redaction and level are process-wide: one scrubber source for logs,
        # metrics labels, and the audit trail.
        configure_logging(
            level=_text(source, LOG_LEVEL_ENV) or "info",
            secret_values=secret_values,
        )
        metrics = Metrics(scrubber=SecretScrubber(secret_values))
        audit = AuditLog(config.audit_path, secret_values=secret_values)
        # Work outside a request (boot recovery, the scheduler) has no
        # credential to resolve; this facade names the owner tenant explicitly
        # instead of relying on the log's default.
        boot_audit = _TenantAudit(audit, OWNER_TENANT_ID)
        # KEL-29: the store shares the process audit trail, so session mint
        # and revoke land in the same scrubbed JSONL as every other decision.
        store = ChatStore(config.db_path, audit=audit)
        # KEL-22: one per-recipient egress account, written into the same audit
        # trail (so its retention and scrubbing apply) and shared by the model
        # client and every skill's egress client.
        egress = EgressLedger(audit)
        if runtime_provider is None:
            # The service config is authoritative: pass its key, environ,
            # resolver, and tenant explicitly so build_provider never has to
            # guess from the process environment. When a tenant secrets file is
            # configured its resolver is the key source; otherwise the boot
            # environment's key is passed directly, unchanged from P1-2.
            # ``factory`` keeps the module-level ``ProviderClient`` seam a
            # one-argument callable.
            runtime_provider = build_provider(
                config.kernel,
                factory=ProviderClient,
                api_key=(
                    None
                    if config.kernel.tenant_secrets_file
                    else config.kernel.api_key
                ),
                resolver=secret_resolver,
                environ=source,
                tenant_id=config.kernel.tenant_id,
            )
            # Attach after construction on purpose: ``ProviderClient(config)``
            # stays a one-argument call, which the serve harness relies on.
            try:
                runtime_provider.ledger = egress
            except AttributeError:  # pragma: no cover - slotted replacement
                pass
        usage_store = LoggedUsageStore(JsonlUsageStore(config.usage_path))
        spend_guard = SpendGuard(
            usage_store,
            config.spend_caps,
            state_path=config.usage_dir / "spend-state.json",
        )
        artifacts = ArtifactRegistry(
            store,
            config.artifact_dir,
            workspace_root=config.workspace_root,
        )
        service = ChatService(
            store=store,
            provider=runtime_provider,
            registry=registry,
            artifacts=artifacts,
            approvals=ApprovalStore(),
            usage_store=usage_store,
            model=config.kernel.primary_model,
            config=config.kernel,
            secrets=secrets,
            workspace_root=str(config.workspace_root),
            tools=_tool_specs(registry),
            audit=audit,
            metrics=metrics,
            spend_guard=spend_guard,
        )
        # Publish persisted spend before the first scrape: the gauge is derived
        # from the records on disk, so a restart shows the real total.
        service.refresh_spend_metrics()
        # Exactly one recovery pass, before the API can accept a request.
        recovery = settle_interrupted(service, store, boot_audit)
        recovered = len(recovery.closed_turns) + len(recovery.expired_approvals)
        if recovered:
            metrics.add_recoveries(recovered)
        # Boot orphan sweep + rotation, then the same pass daily. A retention
        # failure is logged and must not keep the assistant offline.
        maintenance = RetentionMaintenance(
            store=store,
            artifacts=artifacts,
            audit_path=config.audit_path,
            usage_path=config.usage_path,
            policy=retention_policy,
        )
        try:
            maintenance.run_once()
        except Exception as exc:  # noqa: BLE001 - retention is not traffic
            log(
                "retention_boot_failed",
                error_code=type(exc).__name__,
                status="error",
            )
        maintenance.start()
        if validate_credential_env(source):
            credential_broker = CredentialBroker(
                config.db_path.parent / "credentials.db",
                source["KELP_VAULT_KEY"], source["KELP_BROWSER_WORKER_TOKEN"],
                [x.strip() for x in source["KELP_BROWSER_ALLOWED_ORIGINS"].split(",")],
                audit=audit,
            )
        # Wallet and hosted Checkout share one durable ledger. Razorpay's
        # transport stays lazy inside RazorpayWallet and is never constructed
        # unless both server-side keys are present and a payment call is made.
        wallet_ledger = WalletLedger(
            config.db_path.parent / "wallet" / "wallet.db"
        )
        wallet = RazorpayWallet.from_env(source, ledger=wallet_ledger)
        wallet_api = WalletAPI(wallet)
        checkout_api = CheckoutAPI(
            RazorpayCheckout.from_env(source, ledger=wallet_ledger)
        )
        api = ChatAPI(
            service,
            credentials=credential_broker,
            wallet_api=wallet_api,
            checkout_api=checkout_api,
            auth_token=config.auth_token,
            csrf_token=config.csrf_token,
            # One resolver for the process: the configured root token maps to
            # the owner tenant, exactly as the direct ``auth_token=`` check
            # always has. Session rows are minted under that same tenant.
            tenant_resolver=StaticTenantResolver(config.auth_token),
            health=HealthProbe.from_service(service, audit=audit),
            metrics=metrics,
            # Sessions persist in the chat database (KEL-24): several devices
            # survive a restart, and the root bearer token remains recovery.
            # The CSRF half is derived from KELP_SESSION_SECRET, falling back
            # to the root token so a restart keeps existing pairs valid.
            sessions=SessionStore.from_env(
                source,
                store=store,
                fallback_secret=config.auth_token,
                tenant_id=OWNER_TENANT_ID,
            ),
            auth_failures=AuthFailureTracker.from_env(source),
            cookie_secure=_flag(source, SESSION_SECURE_ENV, True),
        )
        # KEL-27: wire the M4-A ticker to a real turn runner. The scheduler
        # starts only after recovery above has settled every interrupted turn,
        # so a proactive turn killed with the process is never retried; the
        # only thing added here is the run row the kill prevented.
        try:
            proactive_config = ProactiveConfig.from_env(source)
        except ValueError as exc:
            raise ConfigError("invalid_config", str(exc)) from None
        schedule_store = ScheduleStore(config.workspace_root)
        proactive = ProactiveRunner(
            service=service,
            store=store,
            schedule_store=schedule_store,
            audit=boot_audit,
            runner=api.runner,
            config=proactive_config,
        )
        proactive.after_recovery(recovery.closed_turns)
        scheduler = SchedulerRuntime(
            schedule_store,
            on_fire=proactive.fire,
            audit=proactive.audit_gate,
            enabled=_flag(source, ENABLED_ENV, True),
            tick_seconds=_env_int_value(
                source, TICK_ENV, DEFAULT_TICK_SECONDS, minimum=1
            ),
            grace_seconds=_env_int_value(
                source, GRACE_ENV, DEFAULT_GRACE_SECONDS, minimum=0
            ),
        )
        scheduler.start()
        return Application(
            config=config,
            store=store,
            service=service,
            api=api,
            audit=audit,
            lock=lock,
            recovery=recovery,
            wallet_api=wallet_api,
            checkout_api=checkout_api,
            wallet_ledger=wallet_ledger,
            provider=runtime_provider,
            owns_provider=owns_provider,
            maintenance=maintenance,
            scheduler=scheduler,
            proactive=proactive,
        )
    except BaseException:
        if credential_broker is not None:
            credential_broker.close()
        if wallet_ledger is not None:
            wallet_ledger.close()
        if scheduler is not None:
            scheduler.stop()
        if proactive is not None:
            proactive.close()
        if maintenance is not None:
            maintenance.stop()
        if store is not None:
            try:
                store.close()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
        lock.release()
        raise


def _install_stop_handlers(server: Any, stop: threading.Event) -> dict[int, Any]:
    previous: dict[int, Any] = {}

    def request_stop(_signum: int, _frame: object) -> None:
        if stop.is_set():
            return
        stop.set()
        threading.Thread(
            target=server.shutdown,
            name="kelp-shutdown",
            daemon=True,
        ).start()

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[signum] = signal.signal(signum, request_stop)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            continue
    return previous


def serve(
    config: ServerConfig,
    *,
    provider: Any | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Run until SIGTERM/SIGINT, then close everything cleanly."""

    app = build_app(config, provider=provider, environ=environ)
    try:
        server = make_server(app.api, config.host, config.port)
    except BaseException:
        app.close()
        raise
    server.daemon_threads = True
    stop = threading.Event()
    previous = _install_stop_handlers(server, stop)
    host, port = server.server_address[0], int(server.server_address[1])
    try:
        print(
            "kelp: listening on "
            f"http://{host}:{port} "
            f"(db={app.config.db_path}, "
            f"recovered={len(app.recovery.closed_turns)}, "
            f"expired_approvals={len(app.recovery.expired_approvals)})",
            flush=True,
        )
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        app.close()
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):  # pragma: no cover - non-main thread
                pass
    return 0


def _emit_error(error: ServerError) -> None:
    print(f"kelp: {error}", file=sys.stderr, flush=True)


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m chat.server",
        description="Run the KELP chat server (single process, owner-authenticated).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "validate configuration and skill manifests, then exit "
            "(creates no files or directories)"
        ),
    )
    args = parser.parse_args(argv)

    try:
        config = ServerConfig.from_env(env)
    except ServerError as error:
        _emit_error(error)
        return error.exit_code

    if args.check:
        try:
            # Read-only: no directory is created, no file is touched.
            config.check_layout()
            SkillRegistry.load(config.skill_root)
        except ServerError as error:
            _emit_error(error)
            return error.exit_code
        except SkillManifestError as error:
            print(f"kelp: startup_failed: {error}", file=sys.stderr, flush=True)
            return 2
        except OSError as error:
            print(
                f"kelp: startup_failed: {type(error).__name__}",
                file=sys.stderr,
                flush=True,
            )
            return 2
        print("kelp: configuration ok", flush=True)
        return 0

    try:
        return serve(config, environ=env)
    except DatabaseLocked as error:
        _emit_error(error)
        return error.exit_code
    except ServerError as error:
        _emit_error(error)
        return error.exit_code
    except SkillManifestError as error:
        print(f"kelp: startup_failed: {error}", file=sys.stderr, flush=True)
        return 2
    except OSError as error:
        print(
            f"kelp: startup_failed: {type(error).__name__}",
            file=sys.stderr,
            flush=True,
        )
        return 2
    except Exception as error:  # noqa: BLE001 - fail fast, never echo a secret
        print(
            f"kelp: startup_failed: {type(error).__name__}",
            file=sys.stderr,
            flush=True,
        )
        return 1


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())

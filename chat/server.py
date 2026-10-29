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

from kernel import JsonlUsageStore, KernelConfig, TokenFactoryClient
from openshell.approvals import ApprovalStore
from openshell.audit import AuditLog, SecretScrubber
from skills.errors import SkillManifestError
from skills.loader import SkillRegistry

from .api import ChatAPI, HealthProbe, make_server
from .artifacts import ArtifactRegistry
from .logging import configure_logging
from .metrics import LoggedUsageStore, Metrics
from .recovery import RecoveryReport, settle_interrupted
from .service import ChatService
from .store import ChatStore

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

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ServerConfig":
        source = os.environ if env is None else env

        api_key = _text(source, "NEBIUS_API_KEY")
        if not api_key:
            raise ConfigError(
                "missing_api_key",
                "NEBIUS_API_KEY is required; the server will not start without it.",
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
        kernel = KernelConfig.from_env(source)

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
            f"api_key_configured={self.kernel.api_key_configured})"
        )

    __str__ = __repr__


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
    provider: Any | None = None
    owns_provider: bool = False
    _closed: bool = field(default=False, init=False, repr=False)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Turns first: cancel and await them while the loop and the store are
        # still alive, so each one ends ``interrupted`` (§10.4) instead of
        # vanishing with the process.
        self._close_turns()
        if self.owns_provider and self.provider is not None:
            aclose = getattr(self.provider, "aclose", None)
            if aclose is not None:
                try:
                    self.api.runner.run(aclose(), timeout=5.0)
                except Exception:  # noqa: BLE001 - shutdown must not mask exit
                    pass
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
        values["NEBIUS_API_KEY"] = config.kernel.api_key
    values["KELP_AUTH_TOKEN"] = config.auth_token
    if config.csrf_token:
        values["KELP_CSRF_TOKEN"] = config.csrf_token
    return values


def build_app(
    config: ServerConfig,
    *,
    provider: Any | None = None,
    environ: Mapping[str, str] | None = None,
) -> Application:
    """Build the full runtime and run boot recovery exactly once."""

    source = os.environ if environ is None else environ
    config.ensure_directories()
    lock = DatabaseLock(config.db_path).acquire()
    store: ChatStore | None = None
    owns_provider = provider is None
    runtime_provider = provider
    try:
        store = ChatStore(config.db_path)
        registry = SkillRegistry.load(config.skill_root)
        secrets = _service_secrets(registry, config, source)
        secret_values = tuple(dict.fromkeys(secrets.values()))
        # Redaction and level are process-wide: one scrubber source for logs,
        # metrics labels, and the audit trail.
        configure_logging(
            level=_text(source, LOG_LEVEL_ENV) or "info",
            secret_values=secret_values,
        )
        metrics = Metrics(scrubber=SecretScrubber(secret_values))
        audit = AuditLog(config.audit_path, secret_values=secret_values)
        if runtime_provider is None:
            runtime_provider = TokenFactoryClient(config.kernel)
        service = ChatService(
            store=store,
            provider=runtime_provider,
            registry=registry,
            artifacts=ArtifactRegistry(store, config.artifact_dir),
            approvals=ApprovalStore(),
            usage_store=LoggedUsageStore(JsonlUsageStore(config.usage_path)),
            model=config.kernel.primary_model,
            config=config.kernel,
            secrets=secrets,
            workspace_root=str(config.workspace_root),
            tools=_tool_specs(registry),
            audit=audit,
            metrics=metrics,
        )
        # Exactly one recovery pass, before the API can accept a request.
        recovery = settle_interrupted(service, store, audit)
        recovered = len(recovery.closed_turns) + len(recovery.expired_approvals)
        if recovered:
            metrics.add_recoveries(recovered)
        api = ChatAPI(
            service,
            auth_token=config.auth_token,
            csrf_token=config.csrf_token,
            health=HealthProbe.from_service(service, audit=audit),
            metrics=metrics,
        )
        return Application(
            config=config,
            store=store,
            service=service,
            api=api,
            audit=audit,
            lock=lock,
            recovery=recovery,
            provider=runtime_provider,
            owns_provider=owns_provider,
        )
    except BaseException:
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

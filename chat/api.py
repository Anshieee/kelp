"""Framework-neutral HTTP API for the chat surface (KEL-8 §15).

``ChatAPI.handle`` takes a plain ``Request`` and returns a ``Response`` whose
body is either bytes or an SSE iterator. A thin ``http.server`` adapter is
included so the slice runs without adding a web framework; the contract is what
matters, so tests drive ``handle`` directly.

Auth is the single-owner minimum from §12: a bearer token or HttpOnly-style
session cookie on every ``/api/*`` call, a CSRF token or same-origin check on
mutations, and JSON ``401`` instead of a redirect.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import inspect
import math
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

from openshell.egress_ledger import recipient_for_host

from .artifacts import ArtifactError, MAX_UPLOAD_BYTES
from .errors import ChatError
from .logging import (
    is_valid_request_id,
    log,
    new_request_id,
    reset_request_id,
    set_request_id,
)
from .metrics import Metrics, get_metrics
from .routers import (
    approvals as approval_routes,
    artifacts as artifact_routes,
    auth as auth_routes,
    body as body_routes,
    health as health_routes,
    shared as share_routes,
    threads as thread_routes,
    turns as turn_routes,
)
from .routers._util import (
    DEVICE_ID_SHORT_LENGTH,
    DEVICE_ONLINE_SECONDS,
    _DEFAULT_DEVICE_LABEL,
    _DEVICE_ID_SHORT_RE,
    _RATE_LIMITED_FALLBACK_RETRY,
    _json_response,
    _method_not_allowed_response,
    _rate_limited_response,
    _route_label,
    _secret_hash,
    scrub_device_label,
)
from .routers.auth import _AuthLocked
from .routers.body import _BodyRejected, _body_rejection_response
from .routers.shared import (
    _SHARE_STREAM_TICK,
    _apply_share_headers,
    _is_share_path,
    _is_share_request,
)
from .service import ChatService
from .store import ChatStore
from .stream import IDLE_SECONDS, sse_frames

__all__ = [
    "AuthFailureTracker",
    "ChatAPI",
    "HealthProbe",
    "LoopRunner",
    "RateLimiter",
    "Request",
    "Response",
    "SessionStore",
    "TokenBucket",
    "make_server",
    "serve",
]

#: Methods the owner API actually routes. Anything else - HEAD, OPTIONS, PUT,
#: TRACE - is a typed 405 at the API boundary instead of the stdlib's 501
#: HTML page, so the wire answer is JSON and, on a share path, carries the
#: share header set. HEAD is not aliased to GET: a stream response would have
#: to open a real stream for a body the client will discard.
_SUPPORTED_METHODS = frozenset({"GET", "POST", "PATCH", "DELETE"})

# -- rate limiting and concurrency (KEL-17 M3-A) ---------------------------
#
# Every route belongs to one class. ``auth`` is charged only when a request
# cannot authenticate, so a burst of bad credentials cannot spend the owner's
# chat/read budget; the other four classes are charged per client identity.
_RATE_LIMIT_CLASSES = ("auth", "chat", "upload", "read", "admin", "share", "share_global")
_RATE_LIMIT_ENV_PREFIX = "KELP_RATE_LIMIT_"
_RATE_LIMIT_ENABLED_ENV = "KELP_RATE_LIMIT_ENABLED"
_RATE_LIMIT_IDLE_ENV = "KELP_RATE_LIMIT_IDLE_SECONDS"
_RATE_LIMIT_MAX_KEYS_ENV = "KELP_RATE_LIMIT_MAX_KEYS"
#: ``class -> (requests per minute, burst capacity)``. The defaults are
#: deliberately generous: they must not turn ordinary browsing into a 429,
#: while still bounding a runaway client.
_DEFAULT_RATE_LIMITS: dict[str, tuple[float, float]] = {
    "auth": (30.0, 10.0),
    "chat": (120.0, 30.0),
    "upload": (60.0, 20.0),
    "read": (600.0, 200.0),
    # Readiness probes and scrapes are deliberately far more generous: a
    # supervisor loop must not be the thing that makes the service look down.
    "admin": (1200.0, 300.0),
    # KEL-28: share reads are keyed on sha256(token), so one leaked link can
    # only spend its own bucket. ``share_global`` is the process-wide ceiling
    # that many distinct links cannot collectively exhaust.
    "share": (30.0, 10.0),
    "share_global": (600.0, 200.0),
}
_DEFAULT_RATE_LIMIT_IDLE_SECONDS = 300.0
_DEFAULT_RATE_LIMIT_MAX_KEYS = 4096
#: After this long without a failure, an auth bucket is discarded; the next
#: attempt starts with a full bucket. This also bounds the failure map.
_AUTH_IDLE_SECONDS = 300.0

# -- owner sessions and failure bounds (KEL-19 M3-E, KEL-24 M4-C) ---------
#
# The cookie carries an opaque 256-bit session id; the store keeps its SHA-256,
# never the raw value. Sessions are persisted in the chat database so multiple
# devices survive a restart, but neither the session id nor the CSRF half is
# ever written there: CSRF is derived with HMAC from the hashed id and the
# server-side ``KELP_SESSION_SECRET``. The CSRF cookie is the readable half of
# a per-session double-submit pair; the session cookie is HttpOnly and the pair
# is never logged.
SESSION_TTL_ENV = "KELP_SESSION_TTL_SECONDS"
SESSION_MAX_ENV = "KELP_SESSION_MAX"
SESSION_SECRET_ENV = "KELP_SESSION_SECRET"
SESSION_SECURE_ENV = "KELP_SESSION_COOKIE_SECURE"
AUTH_LOCKOUT_ATTEMPTS_ENV = "KELP_AUTH_LOCKOUT_ATTEMPTS"
AUTH_LOCKOUT_SECONDS_ENV = "KELP_AUTH_LOCKOUT_SECONDS"
AUTH_FAILURE_IDLE_ENV = "KELP_AUTH_FAILURE_IDLE_SECONDS"
AUTH_FAILURE_MAX_KEYS_ENV = "KELP_AUTH_FAILURE_MAX_KEYS"
_DEFAULT_SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
_DEFAULT_SESSION_MAX = 1024
_DEFAULT_AUTH_LOCKOUT_ATTEMPTS = 10
_DEFAULT_AUTH_LOCKOUT_SECONDS = 300.0
_DEFAULT_AUTH_FAILURE_IDLE_SECONDS = 300.0
_DEFAULT_AUTH_FAILURE_MAX_KEYS = 4096
_DEFAULT_MAX_CONCURRENT_TURNS = 4
_DEFAULT_MAX_STREAMS_PER_OWNER = 3
#: Share viewers are anonymous, so a share stream is capped process-wide rather
#: than per owner. Sixteen is deliberately above the owner's per-owner cap and
#: far below what one process can hold as idle heartbeats.
_DEFAULT_MAX_SHARE_STREAMS = 16
_MAX_CONCURRENT_TURNS_ENV = "KELP_MAX_CONCURRENT_TURNS"
_MAX_STREAMS_PER_OWNER_ENV = "KELP_MAX_STREAMS_PER_OWNER"
_MAX_SHARE_STREAMS_ENV = "KELP_MAX_SHARE_STREAMS"
#: A readiness probe is cached so a slow upstream cannot slow every scrape.
_PROVIDER_TTL_SECONDS = 30.0
_PROVIDER_PROBE_TIMEOUT = 3.0
_PROVIDER_HTTP_TIMEOUT = 2.0
_COMMIT_ENV = ("KELP_GIT_COMMIT", "GIT_COMMIT", "SOURCE_COMMIT")
_VERSION_ENV = "KELP_VERSION"

# -- read-only share links (KEL-28 M4-F) ----------------------------------
#
# A share is a 256-bit capability in a URL, bound to one frozen snapshot of one
# thread. The raw token exists in the creation response only; the store keeps
# its SHA-256. Every share read is GET-only, charges the ``share`` class before
# the lookup, and returns one indistinguishable 404 for unknown, expired,
# revoked, and malformed input.
SHARE_TTL_ENV = "KELP_SHARE_TTL_SECONDS"
SHARE_MAX_TTL_ENV = "KELP_SHARE_MAX_TTL_SECONDS"
SHARE_MAX_SNAPSHOT_BYTES_ENV = "KELP_SHARE_MAX_SNAPSHOT_BYTES"
_DEFAULT_SHARE_TTL_SECONDS = 7 * 24 * 60 * 60
_DEFAULT_SHARE_MAX_TTL_SECONDS = 30 * 24 * 60 * 60
_DEFAULT_SHARE_MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024

@dataclass
class Request:
    method: str
    path: str
    query: Mapping[str, list[str]] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""
    #: Peer address when an adapter knows it. Tests and embedded callers may
    #: leave it unset; the limiter then falls back to forwarded-address headers.
    client: str | None = None

    def __post_init__(self) -> None:
        self.headers = {key.lower(): value for key, value in self.headers.items()}

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    def query_one(self, name: str) -> str | None:
        values = self.query.get(name)
        return values[0] if values else None


@dataclass
class Response:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    stream: Iterator[str] | None = None
    #: ``Set-Cookie`` values, kept separate because a header mapping cannot hold
    #: two cookies and ``Set-Cookie`` must never be comma-folded.
    cookies: list[str] = field(default_factory=list)


class LoopRunner:
    """Runs chat coroutines on one background event loop for the sync adapter."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="chat-loop")
        self._thread.start()
        self._ready.wait(5)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def run(self, coro: Any, timeout: float = 30.0) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout)

    def spawn(self, coro: Any) -> Any:
        """Schedule a coroutine and return its future without waiting for it."""

        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def close(self) -> None:
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._loop.close()


class _NotConfigured(Exception):
    """The component is not part of this deployment; the check is skipped."""


class HealthProbe:
    """Cheap liveness/readiness probes with a cached provider round-trip.

    Every readiness check either touches only local state (SQLite lock, a
    zero-byte append, a throwaway file) or reads an already-loaded object. Only
    the provider check goes to the network, and its result is cached for
    :data:`_PROVIDER_TTL_SECONDS`, so a scrape loop cannot hammer the upstream.

    The payload deliberately carries stable codes only: no filesystem paths, no
    configuration values, and no exception text, so an unauthenticated probe or
    a degraded component never leaks where state lives.
    """

    def __init__(
        self,
        *,
        store: Any | None = None,
        artifacts: Any | None = None,
        audit: Any | None = None,
        usage_store: Any | None = None,
        provider: Any | None = None,
        registry: Any | None = None,
        provider_check: Callable[[], Any] | None = None,
        provider_ttl: float = _PROVIDER_TTL_SECONDS,
        version: str | None = None,
        commit: str | None = None,
        started_at: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.store = store
        self.artifacts = artifacts
        self.audit = audit
        self.usage_store = usage_store
        self.provider = provider
        self.registry = registry
        self.provider_check = provider_check
        self.provider_ttl = max(0.0, float(provider_ttl))
        self.version = version or _package_version()
        self.commit = commit or _commit_id()
        self._clock = clock
        self._started_at = self._clock() if started_at is None else started_at
        self._provider_lock = threading.Lock()
        self._provider_cached: tuple[float, dict[str, Any]] | None = None

    @classmethod
    def from_service(
        cls, service: ChatService, *, audit: Any | None = None, **kwargs: Any
    ) -> "HealthProbe":
        """Derive probes from the components ``ChatService`` already owns."""

        return cls(
            store=getattr(service, "store", None),
            artifacts=getattr(service, "artifacts", None),
            audit=audit,
            usage_store=getattr(service, "_usage_store", None),
            provider=getattr(service, "_provider", None),
            registry=getattr(service, "_registry", None),
            **kwargs,
        )

    # -- payloads ----------------------------------------------------------

    def liveness(self) -> dict[str, Any]:
        """Liveness never touches state: a 200 means the loop is serving."""

        return self._envelope("ok", {})

    def readiness(self, *, runner: "LoopRunner | None" = None) -> tuple[dict[str, Any], bool]:
        """Run every readiness check and return ``(payload, ready)``."""

        checks = {
            "store": self._guard(self._check_store),
            "artifacts": self._guard(self._check_artifacts),
            "audit": self._guard(self._check_audit),
            "usage": self._guard(self._check_usage),
            "unpriced_models": self._check_unpriced_models(),
            "provider": self._check_provider_cached(runner),
            "registry": self._guard(self._check_registry),
        }
        ready = all(result["ok"] for result in checks.values())
        return self._envelope("ok" if ready else "degraded", checks), ready

    def _envelope(self, status: str, checks: Mapping[str, Any]) -> dict[str, Any]:
        uptime = max(0.0, self._clock() - self._started_at)
        return {
            "ok": status == "ok",
            "status": status,
            "version": self.version,
            "commit": self.commit,
            "uptime_seconds": round(uptime, 3),
            "checks": dict(checks),
        }

    # -- individual checks -------------------------------------------------

    @staticmethod
    def _guard(probe: Callable[[], None]) -> dict[str, Any]:
        try:
            probe()
        except _NotConfigured:
            return {"ok": True, "detail": "not_configured"}
        except TimeoutError:
            return {"ok": False, "detail": "timeout"}
        except Exception:  # noqa: BLE001 - the body must never carry the message
            return {"ok": False, "detail": "unavailable"}
        return {"ok": True}

    def _check_store(self) -> None:
        """Acquire and release a write lock without changing any row."""

        if self.store is None:
            raise _NotConfigured
        path = getattr(self.store, "path", None)
        if path is None:
            connection = getattr(self.store, "_conn", None)
            if connection is None:
                raise _NotConfigured
            connection.execute("SELECT 1")
            return
        # A private connection with a short busy timeout keeps readyz under its
        # latency budget even while a turn is writing. The database must already
        # exist: a probe never creates state.
        if not Path(path).exists():
            raise FileNotFoundError("database file is missing")
        connection = sqlite3.connect(str(path), timeout=0.05)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("ROLLBACK")
        finally:
            connection.close()

    def _check_artifacts(self) -> None:
        if self.artifacts is None:
            raise _NotConfigured
        root = Path(self.artifacts.root)
        fd, name = tempfile.mkstemp(prefix=".readyz-", dir=str(root))
        try:
            os.write(fd, b"ok")
        finally:
            os.close(fd)
            try:
                os.unlink(name)
            except OSError:  # pragma: no cover - cleanup is best effort
                pass

    def _check_audit(self) -> None:
        if self.audit is None:
            raise _NotConfigured
        path = getattr(self.audit, "path", None)
        if path is None:
            raise _NotConfigured
        _append_probe(Path(path))

    def _check_usage(self) -> None:
        if self.usage_store is None:
            raise _NotConfigured
        path = getattr(self.usage_store, "path", None)
        if path is None:
            raise _NotConfigured
        _append_probe(Path(path))

    def _check_unpriced_models(self) -> dict[str, Any]:
        """Report how many usage records in the active window have no price.

        Additive to the ``usage`` check: that one proves the file is writable,
        this one surfaces the state the spend guardrail also reports on
        ``/api/metrics``. The payload is a bounded count only. It never carries
        a model id, a price, a path, or exception text, so a probe body can be
        logged or relayed without exposing usage detail.
        """

        try:
            count = self._unpriced_count()
        except _NotConfigured:
            return {"ok": True, "detail": "not_configured"}
        except TimeoutError:
            return {"ok": False, "detail": "timeout"}
        except Exception:  # noqa: BLE001 - the body must never carry the message
            return {"ok": False, "detail": "unavailable"}
        if count > 0:
            return {"ok": False, "detail": "unpriced", "count": count}
        return {"ok": True, "count": 0}

    def _unpriced_count(self) -> int:
        """Count ``cost_known`` false records finished in the current month.

        The month is the widest active spend window, so a record that still
        counts against the guardrail is also visible here. A torn or
        unparseable timestamp cannot be placed in a window and is skipped, the
        same way the spend guard treats it.
        """

        store = self.usage_store
        if store is None:
            raise _NotConfigured
        source = getattr(store, "records", None)
        if source is None:
            raise _NotConfigured
        rows = source() if callable(source) else source
        window_start = _usage_month_start(datetime.now(timezone.utc))
        count = 0
        for row in rows or ():
            data: Mapping[str, Any] | None = None
            if isinstance(row, Mapping):
                data = row
            else:
                to_dict = getattr(row, "to_dict", None)
                if callable(to_dict):
                    value = to_dict()
                    if isinstance(value, Mapping):
                        data = value
            if data is None or data.get("cost_known") is not False:
                continue
            finished_at = _usage_timestamp(data.get("finished_at"))
            if finished_at is None or finished_at < window_start:
                continue
            count += 1
        return count

    def _check_registry(self) -> None:
        if self.registry is None:
            raise _NotConfigured
        names = self.registry.names()
        if names is None:
            raise OSError("registry did not return a name list")

    # -- provider ----------------------------------------------------------

    def _check_provider_cached(
        self, runner: "LoopRunner | None"
    ) -> dict[str, Any]:
        # One scrape at a time: the clock is read under the lock, and the
        # answer is stamped when the probe *completes*, so ``age_seconds`` and
        # the TTL describe the cached answer rather than the moment a waiter
        # started queueing for it.
        with self._provider_lock:
            now = self._clock()
            cached = self._provider_cached
            if cached is not None and now - cached[0] <= self.provider_ttl:
                result = dict(cached[1])
                result["cached"] = True
                result["age_seconds"] = round(max(0.0, now - cached[0]), 3)
                return result
            result = self._guard(lambda: self._probe_provider(runner))
            self._provider_cached = (self._clock(), dict(result))
            result["cached"] = False
            # Fresh from the probe, by construction: same clock read.
            result["age_seconds"] = 0.0
            return result

    def _probe_provider(self, runner: "LoopRunner | None") -> None:
        if self.provider_check is not None:
            self._run_probe(self.provider_check, runner)
            return
        provider = self.provider
        if provider is None:
            raise _NotConfigured
        # Presence, not truthiness: a probe object may define ``__bool__``.
        custom = getattr(provider, "probe", None)
        if custom is None:
            custom = getattr(provider, "health", None)
        if callable(custom):
            self._run_probe(custom, runner)
            return
        config = getattr(provider, "config", None)
        base_url = getattr(config, "base_url", None) if config is not None else None
        if not base_url:
            # A test double or an embedded provider has no upstream to reach.
            raise _NotConfigured
        api_key = getattr(config, "api_key", None)
        ledger = getattr(provider, "ledger", None)
        self._run_probe(
            lambda: _http_provider_probe(base_url, api_key, ledger), runner
        )

    def _run_probe(self, probe: Callable[[], Any], runner: "LoopRunner | None") -> None:
        result = probe()
        if inspect.isawaitable(result):
            if runner is None:
                close = getattr(result, "close", None)
                if close is not None:
                    close()
                raise TimeoutError("no loop to run the provider probe")
            result = runner.run(result, timeout=_PROVIDER_PROBE_TIMEOUT)
        if result is False:
            raise OSError("provider reported not ready")


def _append_probe(path: Path) -> None:
    """Verify append permission without writing a byte.

    ``O_CREAT`` only creates an empty file; existing content, including the
    audit trail, is never touched. The parent is created exactly as
    ``AuditLog.record`` / ``JsonlUsageStore.record`` would create it, so a
    first-run layout is ready and a blocked one fails.
    """

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    os.close(fd)


def _usage_month_start(now: datetime) -> datetime:
    """Start of ``now``'s UTC month, the widest spend window in force."""

    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


def _usage_timestamp(value: Any) -> datetime | None:
    """Parse a usage record's ``finished_at``; ``None`` when unusable.

    Mirrors the spend guard's reading so a record that one considers in-window
    is not silently out-of-window for the other. A naive stamp is UTC.
    """

    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def _http_provider_probe(
    base_url: str, api_key: str | None, ledger: Any | None = None
) -> None:
    """One bounded ``GET /models``: only a 2xx means reachable and authenticated.

    A redirect, an auth rejection, or a gateway error is not a healthy upstream,
    so anything outside ``200..299`` fails the check. The row it writes says
    which: a rejected status is ``provider_error`` with the status code, never
    ``completed``. The call carries no user content — the key in a header and
    nothing else — but it is still an outbound request, so it writes one
    content-free ledger row (KEL-22).
    """

    import httpx

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    timeout = httpx.Timeout(
        _PROVIDER_HTTP_TIMEOUT, connect=min(1.0, _PROVIDER_HTTP_TIMEOUT)
    )
    url = f"{base_url.rstrip('/')}/models"
    response_bytes = 0
    status: int | None = None
    outcome = "failed"
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False
        ) as client:
            response = await client.get(url, headers=headers)
        status = int(response.status_code)
        response_bytes = len(getattr(response, "content", b"") or b"")
        outcome = "completed" if 200 <= status < 300 else "provider_error"
    except httpx.HTTPError:
        outcome = "unreachable"
        raise
    finally:
        _record_probe_egress(ledger, url, response_bytes, outcome, status)
    if status is None or not 200 <= status < 300:
        raise OSError(f"provider probe was rejected (HTTP {status})")


def _record_probe_egress(
    ledger: Any | None,
    url: str,
    response_bytes: int,
    outcome: str,
    status: int | None = None,
) -> None:
    if ledger is None:
        return
    try:
        host = urlsplit(url).hostname
        ledger.record(
            recipient=recipient_for_host(host),
            purpose="provider.health",
            request_bytes=0,
            response_bytes=response_bytes,
            outcome=outcome,
            method="GET",
            host=host,
            status=status,
        )
    except Exception:  # noqa: BLE001 - ledger is accounting, not traffic
        return


def _package_version() -> str:
    override = os.environ.get(_VERSION_ENV)
    if override and override.strip():
        return override.strip()
    try:
        from importlib.metadata import version

        return version("kelp-assistant")
    except Exception:  # noqa: BLE001 - an uninstalled tree still serves
        return "0.0.0"


def _commit_id() -> str:
    for name in _COMMIT_ENV:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()[:40]
    return "unknown"


def _request_id_for(request: Request) -> str:
    """Use a caller's id only when it is inert; otherwise mint a fresh one."""

    supplied = request.header("x-request-id")
    if is_valid_request_id(supplied):
        return supplied
    return new_request_id()


def _env_number(
    source: Mapping[str, str], name: str, default: float, *, minimum: float = 0.0
) -> float:
    """Read a finite, non-negative number; an unusable value keeps the default."""

    raw = source.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value) or value < minimum:
        return default
    return value


def _env_int(
    source: Mapping[str, str], name: str, default: int, *, minimum: int = 0
) -> int:
    return int(_env_number(source, name, float(default), minimum=minimum))


def _env_enabled(raw: str | None, default: bool = True) -> bool:
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"", "0", "false", "no", "off", "disabled"}


class TokenBucket:
    """A classic token bucket with a monotonic refill clock.

    ``capacity`` is the largest burst; ``rate_per_second`` is the steady-state
    allowance. ``take`` returns ``(allowed, retry_after_seconds)`` and never
    blocks, so the HTTP loop cannot be parked behind a limiter.
    """

    __slots__ = ("capacity", "rate_per_second", "tokens", "updated")

    def __init__(
        self,
        rate_per_second: float,
        capacity: float,
        *,
        tokens: float | None = None,
        now: float | None = None,
    ):
        self.rate_per_second = max(0.0, float(rate_per_second))
        self.capacity = max(0.0, float(capacity))
        self.updated = time.monotonic() if now is None else float(now)
        if tokens is None:
            self.tokens = self.capacity
        else:
            self.tokens = max(0.0, min(float(tokens), self.capacity))

    def take(
        self, cost: float = 1.0, *, now: float | None = None
    ) -> tuple[bool, float]:
        cost = max(0.0, float(cost))
        current = time.monotonic() if now is None else float(now)
        elapsed = max(0.0, current - self.updated)
        if elapsed:
            self.tokens = min(
                self.capacity, self.tokens + elapsed * self.rate_per_second
            )
            self.updated = current
        if cost == 0.0:
            return True, 0.0
        if self.tokens >= cost:
            self.tokens -= cost
            return True, 0.0
        if self.rate_per_second <= 0.0 or cost > self.capacity:
            return False, _RATE_LIMITED_FALLBACK_RETRY
        return False, (cost - self.tokens) / self.rate_per_second


class RateLimiter:
    """Per-class, per-identity token buckets with idle eviction.

    The limiter is intentionally fail-safe at the boundary in :class:`ChatAPI`:
    a bug here must never turn traffic into a 500. Buckets are keyed by
    ``(class, identity)`` so one client's burst cannot spend another's budget.
    """

    def __init__(
        self,
        limits: Mapping[str, tuple[float, float]] | None = None,
        *,
        enabled: bool = True,
        clock: Callable[[], float] = time.monotonic,
        idle_seconds: float = _DEFAULT_RATE_LIMIT_IDLE_SECONDS,
        max_keys: int = _DEFAULT_RATE_LIMIT_MAX_KEYS,
    ):
        self.enabled = bool(enabled)
        self._clock = clock
        self.idle_seconds = max(0.0, float(idle_seconds))
        self.max_keys = max(1, int(max_keys))
        source = limits or _DEFAULT_RATE_LIMITS
        self.limits: dict[str, tuple[float, float]] = {}
        for name in _RATE_LIMIT_CLASSES:
            rpm, burst = source.get(name, _DEFAULT_RATE_LIMITS[name])
            self.limits[name] = (max(0.0, float(rpm)), max(0.0, float(burst)))
        #: ``class -> identity -> (bucket, last_seen)``.
        self._buckets: dict[str, dict[str, tuple[TokenBucket, float]]] = {
            name: {} for name in self.limits
        }
        self._lock = threading.Lock()

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> "RateLimiter":
        source = os.environ if env is None else env
        limits: dict[str, tuple[float, float]] = {}
        for name, (rpm, burst) in _DEFAULT_RATE_LIMITS.items():
            limits[name] = (
                _env_number(source, f"{_RATE_LIMIT_ENV_PREFIX}{name.upper()}_RPM", rpm),
                _env_number(
                    source, f"{_RATE_LIMIT_ENV_PREFIX}{name.upper()}_BURST", burst
                ),
            )
        return cls(
            limits,
            enabled=_env_enabled(source.get(_RATE_LIMIT_ENABLED_ENV)),
            clock=clock,
            idle_seconds=_env_number(
                source,
                _RATE_LIMIT_IDLE_ENV,
                _DEFAULT_RATE_LIMIT_IDLE_SECONDS,
            ),
            max_keys=_env_int(
                source,
                _RATE_LIMIT_MAX_KEYS_ENV,
                _DEFAULT_RATE_LIMIT_MAX_KEYS,
                minimum=1,
            ),
        )

    def consume(
        self, class_name: str, key: str, cost: float = 1.0
    ) -> tuple[bool, float]:
        if not self.enabled:
            return True, 0.0
        name = class_name if class_name in self.limits else "read"
        identity = str(key or "unknown")
        now = float(self._clock())
        rpm, burst = self.limits[name]
        with self._lock:
            buckets = self._buckets[name]
            entry = buckets.get(identity)
            if entry is None or now - entry[1] > self.idle_seconds:
                # Idle-out: a key that went quiet starts from a full bucket.
                bucket = TokenBucket(rpm / 60.0, burst, now=now)
            else:
                bucket = entry[0]
            allowed, retry_after = bucket.take(cost, now=now)
            buckets[identity] = (bucket, now)
            if len(buckets) > self.max_keys:
                overflow = len(buckets) - self.max_keys
                stale = sorted(buckets, key=lambda item: buckets[item][1])[:overflow]
                for item in stale:
                    buckets.pop(item, None)
        return allowed, retry_after

    # A readable alias for direct callers and tests.
    take = consume

    def forget_class(self, class_name: str, key: str) -> None:
        """Drop one identity's bucket, e.g. after a successful authentication."""

        with self._lock:
            buckets = self._buckets.get(class_name)
            if buckets is not None:
                buckets.pop(str(key or "unknown"), None)

    def forget(self, key: str) -> None:
        identity = str(key or "unknown")
        with self._lock:
            for buckets in self._buckets.values():
                buckets.pop(identity, None)

    def reset(self) -> None:
        with self._lock:
            for buckets in self._buckets.values():
                buckets.clear()


class _Capacity:
    """A non-blocking counting semaphore with observable saturation."""

    def __init__(self, limit: int):
        self.limit = max(0, int(limit))
        self._used = 0
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        if self.limit <= 0:
            return True
        with self._lock:
            if self._used >= self.limit:
                return False
            self._used += 1
            return True

    def release(self) -> None:
        if self.limit <= 0:
            return
        with self._lock:
            self._used = max(0, self._used - 1)

    def stats(self) -> tuple[int, int]:
        with self._lock:
            return self._used, self.limit


class _PerOwnerCapacity:
    """A per-owner counting semaphore; a zero limit disables the cap."""

    def __init__(self, limit: int):
        self.limit = max(0, int(limit))
        self._used: dict[str, int] = {}
        self._lock = threading.Lock()

    def acquire(self, owner: str) -> bool:
        if self.limit <= 0:
            return True
        with self._lock:
            used = self._used.get(owner, 0)
            if used >= self.limit:
                return False
            self._used[owner] = used + 1
            return True

    def release(self, owner: str) -> None:
        if self.limit <= 0:
            return
        with self._lock:
            used = self._used.get(owner, 0)
            if used <= 1:
                self._used.pop(owner, None)
            else:
                self._used[owner] = used - 1

    def stats(self) -> tuple[int, int]:
        """Return ``(busiest_owner_usage, per_owner_limit)``."""

        with self._lock:
            return (max(self._used.values(), default=0), self.limit)


def _session_key(secret: str | bytes | None) -> bytes:
    """Normalise ``KELP_SESSION_SECRET`` into a fixed 32-byte HMAC key.

    A missing secret is generated per process; :meth:`SessionStore.from_env`
    passes the root owner token as a fallback so a restart keeps the derived
    CSRF halves stable even when operators have not set the dedicated value.
    """

    if secret is None:
        return secrets.token_bytes(32)
    raw = secret.encode("utf-8", "surrogatepass") if isinstance(secret, str) else bytes(secret)
    return hashlib.sha256(raw).digest()


@dataclass
class _Session:
    """A server-side owner session. Only hashes are used for lookup."""

    id_hash: str
    label: str = _DEFAULT_DEVICE_LABEL
    created_at: float = 0.0
    last_seen: float = 0.0
    expires_at: float = 0.0


class SessionStore:
    """Bounded owner sessions with 30-day rolling expiry.

    The raw session id exists only in the client's cookie and in the response
    that minted it. Lookup keys are SHA-256 digests, so a dump of this store
    cannot be replayed as a login. With a :class:`ChatStore` the rows persist
    in the chat database (so several devices survive a restart); without one
    the store keeps an in-process map with the same bounds, which is what
    tests and ephemeral embeddings use.

    The CSRF half is never stored: it is ``HMAC(secret, id_hash)``, so the
    database holds no credential that can be replayed as a cookie and a
    restart with the same secret keeps every live device's CSRF pair valid.
    """

    def __init__(
        self,
        *,
        ttl: float = _DEFAULT_SESSION_TTL_SECONDS,
        max_entries: int = _DEFAULT_SESSION_MAX,
        clock: Callable[[], float] | None = None,
        store: ChatStore | None = None,
        secret: str | bytes | None = None,
    ):
        self.ttl = max(60.0, float(ttl))
        self.max_entries = max(1, int(max_entries))
        # Resolve at construction, not at class definition, so a test can
        # monkeypatch ``time.time`` before building the store.
        self._clock = time.time if clock is None else clock
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()
        self._store = store
        self._secret = _session_key(secret)

    @classmethod
    def from_env(
        cls,
        source: Mapping[str, str] | None = None,
        *,
        store: ChatStore | None = None,
        fallback_secret: str | None = None,
    ) -> "SessionStore":
        env = os.environ if source is None else source
        secret = (env.get(SESSION_SECRET_ENV) or "").strip() or fallback_secret
        return cls(
            ttl=_env_number(env, SESSION_TTL_ENV, _DEFAULT_SESSION_TTL_SECONDS, minimum=60.0),
            max_entries=_env_int(env, SESSION_MAX_ENV, _DEFAULT_SESSION_MAX, minimum=1),
            store=store,
            secret=secret,
        )

    def now(self) -> float:
        return self._clock()

    def _csrf_for(self, id_hash: str) -> str:
        digest = hmac.new(
            self._secret, b"kelp-csrf:" + id_hash.encode("ascii"), hashlib.sha256
        ).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def _record(self, row: Mapping[str, Any]) -> _Session:
        return _Session(
            id_hash=str(row["id_hash"]),
            label=str(row["label"]),
            created_at=float(row["created_at"]),
            last_seen=float(row["last_seen"]),
            expires_at=float(row["expires_at"]),
        )

    def create(self, *, label: str | None = None) -> tuple[str, str, _Session]:
        """Mint a session and return ``(session_id, csrf_token, record)``."""

        now = self._clock()
        session_id = secrets.token_urlsafe(32)
        clean_label = scrub_device_label(label)
        record = _Session(
            id_hash=_secret_hash(session_id),
            label=clean_label,
            created_at=now,
            last_seen=now,
            expires_at=now + self.ttl,
        )
        if self._store is not None:
            self._store.session_prune(
                now=now, revoked_before=now - self.ttl
            )
            self._store.session_insert(
                record.id_hash,
                record.label,
                record.created_at,
                record.last_seen,
                record.expires_at,
            )
            self._store.session_enforce_max(self.max_entries, now=now)
        else:
            with self._lock:
                self._prune_locked(now)
                self._sessions[record.id_hash] = record
                self._enforce_max_locked()
        return session_id, self._csrf_for(record.id_hash), record

    def lookup(self, session_id: str, *, now: float | None = None) -> _Session | None:
        """Return a live session without changing its expiry."""

        if not session_id:
            return None
        moment = self._clock() if now is None else now
        key = _secret_hash(session_id)
        if self._store is not None:
            # Read-through keeps a revoke or expiry visible to the stream's
            # fresh auth check even when another device performed it.
            row = self._store.session_get(key, now=moment)
            return self._record(row) if row is not None else None
        with self._lock:
            record = self._sessions.get(key)
            if record is None:
                return None
            if record.expires_at <= moment:
                del self._sessions[key]
                return None
            return record

    def touch(self, record: _Session, *, now: float | None = None) -> _Session | None:
        """Roll one session's expiry forward, if it is still present and live."""

        moment = self._clock() if now is None else now
        if self._store is not None:
            ok = self._store.session_touch(
                record.id_hash, last_seen=moment, expires_at=moment + self.ttl
            )
            if not ok:
                return None
            return _Session(
                id_hash=record.id_hash,
                label=record.label,
                created_at=record.created_at,
                last_seen=moment,
                expires_at=moment + self.ttl,
            )
        with self._lock:
            current = self._sessions.get(record.id_hash)
            if current is None:
                return None
            if current.expires_at <= moment:
                del self._sessions[current.id_hash]
                return None
            current.last_seen = moment
            current.expires_at = moment + self.ttl
            return current

    def csrf_matches(self, record: _Session, token: str) -> bool:
        if not token:
            return False
        return hmac.compare_digest(self._csrf_for(record.id_hash), token)

    def csrf_token(self, record: _Session) -> str:
        """The derived readable CSRF half for one live session."""

        return self._csrf_for(record.id_hash)

    def revoke(self, session_id: str) -> bool:
        if not session_id:
            return False
        key = _secret_hash(session_id)
        if self._store is not None:
            return self._store.session_revoke(key, revoked_at=self._clock())
        with self._lock:
            return self._sessions.pop(key, None) is not None

    def revoke_all(self) -> int:
        if self._store is not None:
            return self._store.session_revoke_all(revoked_at=self._clock())
        with self._lock:
            count = len(self._sessions)
            self._sessions.clear()
            return count

    def devices(
        self, *, current_session_id: str | None = None, now: float | None = None
    ) -> list[dict[str, Any]]:
        """Describe live devices without exposing a session id or label in logs.

        ``current`` marks only the cookie that made this call; a bearer caller
        has no session and therefore no current device.
        """

        moment = self._clock() if now is None else now
        if self._store is not None:
            rows = self._store.session_list(now=moment)
        else:
            with self._lock:
                rows = [
                    {
                        "id_hash": record.id_hash,
                        "label": record.label,
                        "created_at": record.created_at,
                        "last_seen": record.last_seen,
                        "expires_at": record.expires_at,
                    }
                    for record in self._sessions.values()
                    if record.expires_at > moment
                ]
        current_hash = _secret_hash(current_session_id) if current_session_id else None
        devices = [
            {
                "id": str(row["id_hash"])[:DEVICE_ID_SHORT_LENGTH],
                "label": str(row["label"]),
                "created_at": int(float(row["created_at"])),
                "last_seen": int(float(row["last_seen"])),
                "expires_at": int(float(row["expires_at"])),
                "online": (moment - float(row["last_seen"])) <= DEVICE_ONLINE_SECONDS,
                "current": current_hash is not None
                and str(row["id_hash"]) == current_hash,
            }
            for row in rows
        ]
        devices.sort(key=lambda item: item["last_seen"], reverse=True)
        return devices

    def revoke_short(self, id_short: str, *, now: float | None = None) -> _Session | None:
        """Revoke one device by its public short id; ``None`` when not unique."""

        if not _DEVICE_ID_SHORT_RE.fullmatch(id_short):
            return None
        moment = self._clock() if now is None else now
        if self._store is not None:
            row = self._store.session_find_prefix(id_short, now=moment)
            if row is None:
                return None
            record = self._record(row)
            if not self._store.session_revoke(record.id_hash, revoked_at=moment):
                return None
            return record
        with self._lock:
            matches = [
                record
                for key, record in self._sessions.items()
                if key.startswith(id_short) and record.expires_at > moment
            ]
            if len(matches) != 1:
                return None
            record = matches[0]
            self._sessions.pop(record.id_hash, None)
            return record

    def prune(self, *, now: float | None = None) -> int:
        moment = self._clock() if now is None else now
        if self._store is not None:
            return self._store.session_prune(
                now=moment, revoked_before=moment - self.ttl
            )
        with self._lock:
            return self._prune_locked(moment)

    def _prune_locked(self, now: float) -> int:
        expired = [key for key, record in self._sessions.items() if record.expires_at <= now]
        for key in expired:
            del self._sessions[key]
        return len(expired)

    def _enforce_max_locked(self) -> None:
        while len(self._sessions) > self.max_entries:
            oldest = min(
                self._sessions.items(), key=lambda item: (item[1].last_seen, item[0])
            )[0]
            del self._sessions[oldest]

    def __len__(self) -> int:
        if self._store is not None:
            return self._store.session_count(now=self._clock())
        self.prune()
        return len(self._sessions)


@dataclass
class _FailureRecord:
    count: int
    first_at: float
    last_at: float
    locked_until: float = 0.0


class AuthFailureTracker:
    """Bounded per-identity failure counter and short lockout.

    Keys are already-hashed identities (a credential digest or a peer digest);
    no token, cookie value, or IP address is retained. Idle records and expired
    lockouts are dropped on access, and the oldest record is evicted when the
    cap is reached, so a flood of distinct bad credentials cannot grow the map.
    """

    def __init__(
        self,
        *,
        attempts: int = _DEFAULT_AUTH_LOCKOUT_ATTEMPTS,
        lockout_seconds: float = _DEFAULT_AUTH_LOCKOUT_SECONDS,
        idle_seconds: float = _DEFAULT_AUTH_FAILURE_IDLE_SECONDS,
        max_entries: int = _DEFAULT_AUTH_FAILURE_MAX_KEYS,
        clock: Callable[[], float] | None = None,
    ):
        self.attempts = max(0, int(attempts))
        self.lockout_seconds = max(1.0, float(lockout_seconds))
        self.idle_seconds = max(1.0, float(idle_seconds))
        self.max_entries = max(1, int(max_entries))
        self._clock = time.time if clock is None else clock
        self._records: dict[str, _FailureRecord] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, source: Mapping[str, str] | None = None) -> "AuthFailureTracker":
        env = os.environ if source is None else source
        return cls(
            attempts=_env_int(
                env,
                AUTH_LOCKOUT_ATTEMPTS_ENV,
                _DEFAULT_AUTH_LOCKOUT_ATTEMPTS,
                minimum=0,
            ),
            lockout_seconds=_env_number(
                env,
                AUTH_LOCKOUT_SECONDS_ENV,
                _DEFAULT_AUTH_LOCKOUT_SECONDS,
                minimum=1.0,
            ),
            idle_seconds=_env_number(
                env,
                AUTH_FAILURE_IDLE_ENV,
                _DEFAULT_AUTH_FAILURE_IDLE_SECONDS,
                minimum=1.0,
            ),
            max_entries=_env_int(
                env,
                AUTH_FAILURE_MAX_KEYS_ENV,
                _DEFAULT_AUTH_FAILURE_MAX_KEYS,
                minimum=1,
            ),
        )

    def status(self, identity: str) -> tuple[int, float | None]:
        """Return ``(failure_count, remaining_lockout_seconds)``."""

        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            record = self._records.get(identity)
            if record is None:
                return 0, None
            if record.locked_until > now:
                return record.count, record.locked_until - now
            if record.locked_until:
                # The lockout has run its course; the next attempt starts clean.
                del self._records[identity]
                return 0, None
            return record.count, None

    def record(self, identity: str) -> tuple[int, float | None]:
        """Increment one identity and return ``(count, remaining_lockout)``."""

        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            record = self._records.get(identity)
            if record is None:
                record = _FailureRecord(count=0, first_at=now, last_at=now)
                self._records[identity] = record
                self._enforce_max_locked()
            record.count += 1
            record.last_at = now
            if self.attempts and record.count >= self.attempts:
                record.locked_until = now + self.lockout_seconds
                return record.count, self.lockout_seconds
            return record.count, None

    def clear(self, identity: str) -> None:
        with self._lock:
            self._records.pop(identity, None)

    def _prune_locked(self, now: float) -> None:
        stale = []
        for key, record in self._records.items():
            lock_expired = bool(record.locked_until) and record.locked_until <= now
            idle = now - record.last_at > self.idle_seconds
            if lock_expired or (idle and not record.locked_until):
                stale.append(key)
        for key in stale:
            del self._records[key]

    def _enforce_max_locked(self) -> None:
        while len(self._records) > self.max_entries:
            oldest = min(
                self._records.items(), key=lambda item: (item[1].last_at, item[0])
            )[0]
            del self._records[oldest]

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

class ChatAPI:
    def __init__(
        self,
        service: ChatService,
        *,
        auth_token: str | None = None,
        csrf_token: str | None = None,
        runner: LoopRunner | None = None,
        stream_idle: float = IDLE_SECONDS,
        max_upload: int = MAX_UPLOAD_BYTES,
        health: HealthProbe | None = None,
        metrics: Metrics | None = None,
        limiter: RateLimiter | None = None,
        max_concurrent_turns: int | None = None,
        max_streams_per_owner: int | None = None,
        max_share_streams: int | None = None,
        sessions: SessionStore | None = None,
        auth_failures: AuthFailureTracker | None = None,
        cookie_secure: bool | None = None,
        share_ttl: float | None = None,
        share_max_ttl: float | None = None,
        share_max_snapshot_bytes: int | None = None,
    ):
        self.service = service
        self.auth_token = auth_token
        self.csrf_token = csrf_token
        self.stream_idle = stream_idle
        self.max_upload = max_upload
        self.health = health or HealthProbe.from_service(service)
        self.metrics = metrics or get_metrics()
        self.limiter = limiter or RateLimiter.from_env()
        source = os.environ
        self.sessions = (
            SessionStore.from_env(source) if sessions is None else sessions
        )
        self.auth_failures = (
            AuthFailureTracker.from_env(source)
            if auth_failures is None
            else auth_failures
        )
        self.cookie_secure = (
            _env_enabled(source.get(SESSION_SECURE_ENV), True)
            if cookie_secure is None
            else bool(cookie_secure)
        )
        # A requested TTL is clamped, never rejected: the hard ceiling is the
        # only bound that matters, and a caller asking for 90 days gets 30.
        self.share_max_ttl = (
            _env_number(
                source, SHARE_MAX_TTL_ENV, _DEFAULT_SHARE_MAX_TTL_SECONDS, minimum=60.0
            )
            if share_max_ttl is None
            else max(60.0, float(share_max_ttl))
        )
        self.share_ttl = min(
            _env_number(
                source, SHARE_TTL_ENV, _DEFAULT_SHARE_TTL_SECONDS, minimum=60.0
            )
            if share_ttl is None
            else max(60.0, float(share_ttl)),
            self.share_max_ttl,
        )
        self.share_max_snapshot_bytes = (
            _env_int(
                source,
                SHARE_MAX_SNAPSHOT_BYTES_ENV,
                _DEFAULT_SHARE_MAX_SNAPSHOT_BYTES,
                minimum=1024,
            )
            if share_max_snapshot_bytes is None
            else max(1024, int(share_max_snapshot_bytes))
        )
        # Tick for an open share stream's liveness check; tests shorten it.
        self.share_tick = _SHARE_STREAM_TICK
        self._turn_slots = _Capacity(
            _env_int(
                source,
                _MAX_CONCURRENT_TURNS_ENV,
                _DEFAULT_MAX_CONCURRENT_TURNS,
            )
            if max_concurrent_turns is None
            else max_concurrent_turns
        )
        self._stream_slots = _PerOwnerCapacity(
            _env_int(
                source,
                _MAX_STREAMS_PER_OWNER_ENV,
                _DEFAULT_MAX_STREAMS_PER_OWNER,
            )
            if max_streams_per_owner is None
            else max_streams_per_owner
        )
        # A share stream has no owner to key on, so its cap is process-wide.
        # Zero disables it, matching the other concurrency caps.
        self._share_stream_slots = _Capacity(
            _env_int(
                source,
                _MAX_SHARE_STREAMS_ENV,
                _DEFAULT_MAX_SHARE_STREAMS,
            )
            if max_share_streams is None
            else max_share_streams
        )
        self._owns_runner = runner is None
        self.runner = runner or LoopRunner()

    def close(self) -> None:
        if self._owns_runner:
            self.runner.close()

    # -- dispatch ----------------------------------------------------------

    def handle(self, request: Request) -> Response:
        """Dispatch one request, stamping the id and access metrics.

        Every response - success, 4xx, 5xx, stream or JSON - carries
        ``X-Request-Id``. The id is either a validated caller value or a fresh
        opaque one, and is bound to the context so a turn, its tool calls, its
        audit entries, and its usage record all report the same value.
        """

        request_id = _request_id_for(request)
        token = set_request_id(request_id)
        started = time.perf_counter()
        response: Response | None = None
        status = 500
        try:
            response = self._dispatch(request)
            status = response.status
            return response
        finally:
            duration = max(0.0, time.perf_counter() - started)
            route = _route_label(request.path)
            method = request.method.upper()
            try:
                self.metrics.observe_http(method, route, status, duration)
                log(
                    "http_request",
                    level="error" if status >= 500 else "info",
                    request_id=request_id,
                    method=method,
                    route=route,
                    status=status,
                    duration_ms=int(round(duration * 1000)),
                )
            except Exception:  # noqa: BLE001 - observability never breaks traffic
                pass
            finally:
                if response is not None:
                    response.headers.setdefault("X-Request-Id", request_id)
                reset_request_id(token)

    def _dispatch(self, request: Request) -> Response:
        response = self._dispatch_inner(request)
        # A successful cookie-authenticated response carries the rolled pair.
        # A failed response must not extend the session that just failed CSRF.
        # A share response never sets a cookie, even when the caller happens to
        # hold a live owner session: sharing is a capability, not an identity.
        if not _is_share_request(request):
            try:
                auth_routes._roll_session_cookie(self, request, response)
            except Exception:  # noqa: BLE001 - a cookie refresh never breaks a response
                log("session_roll_failed", level="warning", status="error")
        return response

    def _dispatch_inner(self, request: Request) -> Response:
        try:
            path = unquote(urlsplit(request.path).path)
            segments = [segment for segment in path.split("/") if segment]
            # A share is the only unauthenticated read surface besides
            # liveness. It is dispatched here, before auth and before the
            # owner's route classes, so every method (including a body-carrying
            # POST) is answered 405 without ever reaching ``_authorize``.
            if len(segments) >= 2 and segments[0] == "api" and segments[1] == "shared":
                return share_routes._shared(self, request, segments[2:])
            method = request.method.upper()
            if method not in _SUPPORTED_METHODS:
                return _method_not_allowed_response("GET, POST, PATCH, DELETE")
            # Liveness is the one unauthenticated route: a supervisor must be
            # able to tell "process alive" from "owner token missing".
            if not self._is_public(request, segments):
                limited = auth_routes._limit(self, request, segments)
                if limited is not None:
                    return limited
                auth_routes._authorize(self, request)
            if len(segments) < 1 or segments[0] != "api":
                raise ChatError("not_found", status=404, message="Unknown endpoint.")
            return self._route(request, segments[1:])
        except _AuthLocked as exc:
            return _rate_limited_response(
                exc.retry_after,
                class_name="auth",
                reason="locked",
                metrics=self.metrics,
            )
        except ChatError as exc:
            return _json_response(exc.status, exc.to_body())
        except ArtifactError as exc:
            return _json_response(
                exc.status,
                {
                    "ok": False,
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "retryable": False,
                        "user_fixable": True,
                    },
                },
            )
        except Exception:  # noqa: BLE001 - never leak a stack trace to the client
            return _json_response(
                500,
                {
                    "ok": False,
                    "error": {
                        "code": "internal_error",
                        "message": "Something went wrong.",
                        "retryable": True,
                        "user_fixable": False,
                    },
                },
            )

    def body_limit_for(self, path: str) -> tuple[int, int, str]:
        """Return ``(cap, status, code)`` for a request path."""

        return body_routes.body_limit_for(self, path)

    def _route(self, request: Request, segments: list[str]) -> Response:
        if not segments:
            raise ChatError("not_found", status=404, message="Unknown endpoint.")
        head = segments[0]
        method = request.method.upper()

        if head == "auth":
            return auth_routes._auth(self, request, method, segments[1:])
        if head == "threads":
            return thread_routes._threads(
                self, request, method, segments[1:], frames_fn=sse_frames
            )
        if head == "turns":
            return turn_routes._turns(self, request, method, segments[1:])
        if head == "approvals":
            return approval_routes._approvals(self, request, method, segments[1:])
        if head == "attachments":
            if len(segments) == 1 and method == "POST":
                return artifact_routes._upload(self, request)
            raise ChatError("not_found", status=404, message="Unknown endpoint.")
        if head == "artifacts":
            return artifact_routes._artifacts(self, request, method, segments[1:])
        if head == "health":
            return health_routes._health(self, request, method, segments[1:])
        if head == "healthz":
            return health_routes._healthz(self, request, method, segments[1:])
        if head == "readyz":
            return health_routes._readyz(self, request, method, segments[1:])
        if head == "metrics":
            return health_routes._metrics(self, request, method, segments[1:])
        raise ChatError("not_found", status=404, message="Unknown endpoint.")

    @staticmethod
    def _is_public(request: Request, segments: list[str]) -> bool:
        return request.method.upper() == "GET" and segments == ["api", "healthz"]


# -- stdlib adapter --------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    api: ChatAPI

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch()

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch()

    # Without these, ``BaseHTTPRequestHandler`` answers 501 with an HTML page
    # it builds from the request line — a page that would echo a share token
    # and carry none of the share headers. Routing them through the API makes
    # the answer typed JSON everywhere, and a 405 for the methods the API does
    # not serve (HEAD included: a stream response has no meaningful body).
    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch()

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._dispatch()

    def do_TRACE(self) -> None:  # noqa: N802
        self._dispatch()

    def _dispatch(self) -> None:
        parsed = urlsplit(self.path)
        share_path = _is_share_path(parsed.path)
        if share_path and self.command.upper() != "GET":
            # A share is GET-only before any body is read: answer the 405
            # without buffering a body, and close so unread bytes cannot
            # poison the next request on this connection.
            self._respond(
                self.api.handle(self._request(parsed, b"")),
                close=True,
            )
            return
        try:
            body = self._read_body(parsed.path)
        except _BodyRejected as rejected:
            response = _body_rejection_response(rejected)
            if share_path:
                # A body rejected on a share path still answers with the share
                # header set; no path of this surface may skip it.
                _apply_share_headers(response)
            self._respond(response, close=True)
            return
        except (ConnectionError, OSError):
            # The peer vanished mid-body; there is nobody to answer and the
            # partially consumed stream cannot be reused.
            self.close_connection = True
            return
        self._respond(self.api.handle(self._request(parsed, body)))

    def _request(self, parsed: Any, body: bytes) -> Request:
        headers = {key.lower(): value for key, value in self.headers.items()}
        return Request(
            method=self.command,
            path=parsed.path,
            query=parse_qs(parsed.query, keep_blank_values=True),
            headers=headers,
            body=body,
            client=self.client_address[0] if self.client_address else None,
        )

    def _read_body(self, path: str) -> bytes:
        """Read a bounded body, refusing an oversized declaration up front.

        ``Content-Length`` is parsed before a single byte is read: a
        non-numeric, negative, duplicated, or over-cap value gets a typed
        response and the connection is closed, so a large body is never
        buffered. A request that also carries ``Transfer-Encoding`` is refused
        outright (RFC 7230 §3.3.3). Chunked bodies are read incrementally and
        aborted as soon as the running total would exceed the same cap.
        """

        cap, status, code = self.api.body_limit_for(path)
        transfer = ",".join(self.headers.get_all("Transfer-Encoding") or [])
        if transfer.strip():
            if self.headers.get_all("Content-Length"):
                # RFC 7230 §3.3.3: a message carrying both framings is a
                # request-smuggling vector, so it is refused before any body is
                # read rather than resolved by precedence.
                raise _BodyRejected(
                    400,
                    "invalid_request",
                    "A request cannot carry both Content-Length and Transfer-Encoding.",
                )
            if transfer.strip().lower() != "chunked":
                raise _BodyRejected(
                    400, "invalid_request", "Unsupported Transfer-Encoding."
                )
            return self._read_chunked(cap, status, code)

        declared = self.headers.get_all("Content-Length") or []
        if not declared:
            return b""
        values = {value.strip() for value in declared}
        if len(values) != 1:
            raise _BodyRejected(
                400, "invalid_request", "The Content-Length header is ambiguous."
            )
        raw = values.pop()
        if not re.fullmatch(r"[0-9]+", raw):
            raise _BodyRejected(
                400, "invalid_request", "The Content-Length header is not valid."
            )
        length = int(raw, 10)
        if length > cap:
            raise _BodyRejected(status, code, "The request body is too large.")
        if length == 0:
            return b""
        body = self.rfile.read(length)
        if len(body) != length:
            raise _BodyRejected(
                400, "invalid_request", "The request body ended early."
            )
        return body

    def _read_chunked(self, cap: int, status: int, code: str) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            line = self.rfile.readline(1024)
            if not line:
                raise _BodyRejected(
                    400, "invalid_request", "The chunked body ended early."
                )
            if len(line) == 1024 and not line.endswith(b"\n"):
                raise _BodyRejected(
                    400, "invalid_request", "A chunk header is too long."
                )
            size_token = line.split(b";", 1)[0].strip()
            try:
                size = int(size_token, 16)
            except (TypeError, ValueError):
                raise _BodyRejected(
                    400, "invalid_request", "A chunk size is not valid."
                ) from None
            if size < 0:
                raise _BodyRejected(
                    400, "invalid_request", "A chunk size is not valid."
                )
            if size == 0:
                # Consume trailers without retaining them; a bounded loop keeps
                # a malicious trailer stream from parking the handler.
                for _ in range(100):
                    trailer = self.rfile.readline(8192)
                    if not trailer or trailer in (b"\r\n", b"\n"):
                        return b"".join(chunks)
                raise _BodyRejected(
                    400, "invalid_request", "The chunked trailer is too large."
                )
            if total + size > cap:
                # Abort mid-read: the unread remainder is never buffered and
                # the connection is closed after the response.
                raise _BodyRejected(status, code, "The request body is too large.")
            data = self.rfile.read(size)
            if len(data) != size:
                raise _BodyRejected(
                    400, "invalid_request", "The chunked body ended early."
                )
            chunks.append(data)
            total += size
            if self.rfile.read(2) != b"\r\n":
                raise _BodyRejected(
                    400, "invalid_request", "A chunk is not terminated."
                )

    def _respond(self, response: Response, *, close: bool = False) -> None:
        if close:
            self.close_connection = True
        self.send_response(response.status)
        names = {name.lower() for name in response.headers}
        for name, value in response.headers.items():
            if name.lower() == "content-length" and response.stream is not None:
                continue
            self.send_header(name, value)
        for cookie in response.cookies:
            self.send_header("Set-Cookie", cookie)
        if response.stream is None and "content-length" not in names:
            self.send_header("Content-Length", str(len(response.body)))
        if close and "connection" not in names:
            self.send_header("Connection", "close")
        self.end_headers()
        # HEAD keeps the headers (including the Content-Length of the body it
        # would have sent) and writes no bytes, as RFC 9110 requires. The API
        # answers HEAD with 405 before a stream can be opened, so a stream here
        # is only reachable from an embedded caller and is closed, never read.
        head = str(getattr(self, "command", "") or "").upper() == "HEAD"
        body = b"" if head else response.body
        if head and response.stream is not None:
            closer = getattr(response.stream, "close", None)
            if closer is not None:
                closer()
        try:
            if response.stream is not None and not head:
                for frame in response.stream:
                    self.wfile.write(frame.encode("utf-8"))
                    self.wfile.flush()
            elif body:
                self.wfile.write(body)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        """Answer a stdlib protocol error on a share path with the share set.

        The base implementation builds an HTML page from the request line and
        the caller's message, and neither is safe here: the path is the token.
        A share path therefore gets the ordinary typed envelope and the fixed
        header set, and the standard library's text is never rendered.
        """

        path = getattr(self, "path", "") or ""
        if not _is_share_path(path):
            super().send_error(code, message, explain)
            return
        # An unsupported method is the one stdlib error with a real route; the
        # contract says every non-GET on this prefix is a 405 with Allow: GET.
        status = 405 if code == 501 else code
        error = ChatError(
            "method_not_allowed" if status == 405 else "invalid_request",
            status=status,
            message=(
                "Share links are read-only."
                if status == 405
                else "The request could not be read."
            ),
        )
        response = _json_response(status, error.to_body())
        if status == 405:
            response.headers["Allow"] = "GET"
        _apply_share_headers(response)
        response.headers.setdefault("X-Request-Id", new_request_id())
        self.log_error("share_path_error")
        self._respond(response, close=True)

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the stdlib access log; the JSON sink already has this request.

        :meth:`ChatAPI.handle` emits exactly one structured ``http_request``
        line per request with a validated request id, a normalised route, and
        scrubbed values. ``BaseHTTPRequestHandler`` would print a second,
        unredacted line to stderr, which both doubles the record and escapes
        the JSON contract, so this is deliberately a no-op.
        """

        return None

    def log_error(self, format: str, *args: Any) -> None:
        """Route protocol errors to the JSON sink instead of raw stderr.

        The stdlib message is built from the request line, so it is not
        interpolated: only the fixed event and level are logged. Any detail
        would be unvalidated request text and the access line already carries
        the route and status.
        """

        try:
            log("http_error", level="warning", status="error")
        except Exception:  # noqa: BLE001 - logging must never break a connection
            return


def make_server(
    api: ChatAPI, host: str = "127.0.0.1", port: int = 0
) -> ThreadingHTTPServer:
    handler = type("ChatRequestHandler", (_Handler,), {"api": api})
    return ThreadingHTTPServer((host, port), handler)


def serve(api: ChatAPI, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = make_server(api, host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()

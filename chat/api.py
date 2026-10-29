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
import hashlib
import hmac
import inspect
import json
import math
import os
import re
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass, field
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
from .metrics import Metrics, PROMETHEUS_CONTENT_TYPE, get_metrics
from .service import ChatService
from .stream import IDLE_SECONDS, sse_frames

__all__ = [
    "ChatAPI",
    "HealthProbe",
    "LoopRunner",
    "RateLimiter",
    "Request",
    "Response",
    "TokenBucket",
    "make_server",
    "serve",
]

_THREAD_RE = re.compile(r"^[A-Za-z0-9:_-]{1,80}$")
_CLIENT_TURN_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_MUTATING = frozenset({"POST", "PATCH", "DELETE", "PUT"})
_JSON_LIMIT = 1024 * 1024
_DECISION_LIMIT = 1024
_UPLOAD_OVERHEAD = 64 * 1024

# -- rate limiting and concurrency (KEL-17 M3-A) ---------------------------
#
# Every route belongs to one class. ``auth`` is charged only when a request
# cannot authenticate, so a burst of bad credentials cannot spend the owner's
# chat/read budget; the other four classes are charged per client identity.
_RATE_LIMIT_CLASSES = ("auth", "chat", "upload", "read", "admin")
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
}
_DEFAULT_RATE_LIMIT_IDLE_SECONDS = 300.0
_DEFAULT_RATE_LIMIT_MAX_KEYS = 4096
#: After this long without a failure, an auth bucket is discarded; the next
#: attempt starts with a full bucket. This also bounds the failure map.
_AUTH_IDLE_SECONDS = 300.0
_DEFAULT_MAX_CONCURRENT_TURNS = 4
_DEFAULT_MAX_STREAMS_PER_OWNER = 3
_MAX_CONCURRENT_TURNS_ENV = "KELP_MAX_CONCURRENT_TURNS"
_MAX_STREAMS_PER_OWNER_ENV = "KELP_MAX_STREAMS_PER_OWNER"
_RATE_LIMITED_FALLBACK_RETRY = 60.0
#: A readiness probe is cached so a slow upstream cannot slow every scrape.
_PROVIDER_TTL_SECONDS = 30.0
_PROVIDER_PROBE_TIMEOUT = 3.0
_PROVIDER_HTTP_TIMEOUT = 2.0
_COMMIT_ENV = ("KELP_GIT_COMMIT", "GIT_COMMIT", "SOURCE_COMMIT")
_VERSION_ENV = "KELP_VERSION"


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


async def _http_provider_probe(
    base_url: str, api_key: str | None, ledger: Any | None = None
) -> None:
    """One bounded ``GET /models``: only a 2xx means reachable and authenticated.

    A redirect, an auth rejection, or a gateway error is not a healthy upstream,
    so anything outside ``200..299`` fails the check. The call carries no user
    content — the key in a header and nothing else — but it is still an outbound
    request, so it writes one content-free ledger row (KEL-22).
    """

    import httpx

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    timeout = httpx.Timeout(
        _PROVIDER_HTTP_TIMEOUT, connect=min(1.0, _PROVIDER_HTTP_TIMEOUT)
    )
    url = f"{base_url.rstrip('/')}/models"
    response_bytes = 0
    outcome = "failed"
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False
        ) as client:
            response = await client.get(url, headers=headers)
        response_bytes = len(getattr(response, "content", b"") or b"")
        outcome = "completed"
    except httpx.HTTPError:
        outcome = "unreachable"
        raise
    finally:
        _record_probe_egress(ledger, url, response_bytes, outcome)
    if not 200 <= response.status_code < 300:
        raise OSError("provider probe was rejected")


def _record_probe_egress(
    ledger: Any | None, url: str, response_bytes: int, outcome: str
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
        )
    except Exception:  # noqa: BLE001 - ledger is accounting, not traffic
        return


def _package_version() -> str:
    override = os.environ.get(_VERSION_ENV)
    if override and override.strip():
        return override.strip()
    try:
        from importlib.metadata import version

        return version("nebius-assistant")
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


_DYNAMIC_ROUTE_HEADS = frozenset({"threads", "turns", "approvals", "artifacts"})
_STATIC_ROUTE_HEADS = frozenset(
    {"health", "healthz", "readyz", "metrics", "attachments"}
)
_ROUTE_ACTIONS = frozenset(
    {
        "stream",
        "turns",
        "stop",
        "retry",
        "decision",
        "preview",
        "content",
    }
)


def _route_label(path: str) -> str:
    """A bounded route label for metrics: ids are replaced, not recorded."""

    try:
        clean = unquote(urlsplit(path).path)
    except ValueError:  # pragma: no cover - urlsplit rarely raises here
        return "/api/other"
    segments = [segment for segment in clean.split("/") if segment]
    if not segments:
        return "/"
    if segments[0] != "api":
        return "/other"
    if len(segments) == 1:
        return "/api"
    head = segments[1]
    if head in _STATIC_ROUTE_HEADS:
        return f"/api/{head}" if len(segments) == 2 else "/api/other"
    if head in _DYNAMIC_ROUTE_HEADS:
        if len(segments) == 2:
            return f"/api/{head}"
        label = f"/api/{head}/{{id}}"
        if len(segments) == 4 and segments[3] in _ROUTE_ACTIONS:
            label = f"{label}/{segments[3]}"
        elif len(segments) > 4:
            return "/api/other"
        return label
    return "/api/other"


def _gauge(value: float) -> str:
    """Prometheus number formatting without importing the metrics internals."""

    number = float(value)
    if not math.isfinite(number):
        number = 0.0
    if number.is_integer():
        return str(int(number))
    return repr(round(number, 9))


def _rate_limited_response(
    retry_after: float,
    *,
    class_name: str,
    reason: str,
    limit: int | None = None,
) -> Response:
    """Build the one 429 shape: ``rate_limited`` plus a usable Retry-After.

    The status is always 429 and the body is always the ordinary error
    envelope, so the client's existing rate-limited state renders it without a
    special case.
    """

    try:
        seconds = float(retry_after)
    except (TypeError, ValueError):
        seconds = _RATE_LIMITED_FALLBACK_RETRY
    if not math.isfinite(seconds) or seconds <= 0:
        seconds = _RATE_LIMITED_FALLBACK_RETRY
    header = str(max(1, int(math.ceil(seconds))))
    details: dict[str, Any] = {"class": class_name, "reason": reason}
    if limit is not None:
        details["limit"] = max(0, int(limit))
    error = ChatError(
        "rate_limited",
        status=429,
        retryable=True,
        details=details,
    )
    response = _json_response(429, error.to_body())
    response.headers["Retry-After"] = header
    return response


def _body_rejection_response(rejected: "_BodyRejected") -> Response:
    error = ChatError(
        rejected.code,
        status=rejected.status,
        message=rejected.message,
        retryable=False,
        user_fixable=True,
    )
    return _json_response(rejected.status, error.to_body())


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


def _client_address(request: Request) -> str:
    """Best-effort peer identity for an unauthenticated caller.

    The adapter records the socket peer in :attr:`Request.client`. Forwarded
    headers are only a fallback for tests and embedded servers that do not, and
    the first hop is taken: a proxy appends, it does not replace.
    """

    if request.client:
        return str(request.client)[:64]
    forwarded = request.header("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first[:64]
    for name in ("x-real-ip", "cf-connecting-ip"):
        value = request.header(name)
        if value and value.strip():
            return value.strip()[:64]
    return "unknown"


def _presented_token(request: Request) -> str | None:
    header = request.header("authorization") or ""
    prefix = "Bearer "
    if header.startswith(prefix) and header[len(prefix) :]:
        return header[len(prefix) :]
    session = _parse_cookies(request.header("cookie")).get("session")
    return session or None


def _client_identity(request: Request) -> str:
    """A stable, non-secret limiter key.

    A presented token is hashed, never retained: the map must not become a
    secondary store of the owner token. With auth disabled the peer address is
    the identity, which also makes per-client tests possible.
    """

    token = _presented_token(request)
    if token:
        digest = hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()
        return f"owner:{digest[:24]}"
    return f"client:{_client_address(request)}"


def _classify_route(method: str, segments: list[str]) -> str:
    """Map a route to its rate-limit class; unknown routes are cheap reads."""

    parts = segments[1:] if segments and segments[0] == "api" else segments
    if not parts:
        return "read"
    head = parts[0]
    verb = method.upper()
    if verb == "GET":
        return "admin" if head in {"metrics", "readyz"} else "read"
    if head == "attachments":
        return "upload"
    if head in {"threads", "turns", "approvals"}:
        return "chat"
    return "read"


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


class _Lease:
    """Idempotent release for a stream slot, including a never-started stream."""

    def __init__(self, release: Callable[[], None]):
        self._release = release
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        try:
            self._release()
        except Exception:  # noqa: BLE001 - a lease must never raise
            pass

    def __del__(self) -> None:  # pragma: no cover - timing is interpreter-owned
        try:
            self.release()
        except Exception:  # noqa: BLE001
            pass


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
        self._owns_runner = runner is None
        self.runner = runner or LoopRunner()

    def close(self) -> None:
        if self._owns_runner:
            self.runner.close()

    # -- dispatch ----------------------------------------------------------

    def handle(self, request: Request) -> Response:
        """Dispatch one request, stamping the id and access metrics.

        Every response — success, 4xx, 5xx, stream or JSON — carries
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
        try:
            path = unquote(urlsplit(request.path).path)
            segments = [segment for segment in path.split("/") if segment]
            # Liveness is the one unauthenticated route: a supervisor must be
            # able to tell "process alive" from "owner token missing".
            if not self._is_public(request, segments):
                limited = self._limit(request, segments)
                if limited is not None:
                    return limited
                self._authorize(request)
            if len(segments) < 1 or segments[0] != "api":
                raise ChatError("not_found", status=404, message="Unknown endpoint.")
            return self._route(request, segments[1:])
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

    def _limit(self, request: Request, segments: list[str]) -> Response | None:
        """Charge the request's limiter class, or return a typed 429.

        Unauthenticated calls are checked by the independent ``auth`` bucket,
        keyed by peer address, before :meth:`_authorize` produces its 401. A
        valid credential is never charged against that bucket. Any limiter
        fault fails open: a broken bucket must not turn a request into a 500.
        """

        try:
            if not self.limiter.enabled:
                return None
            if self._authorized_for(request):
                route = _classify_route(request.method, segments)
                allowed, retry_after = self.limiter.consume(
                    route, _client_identity(request)
                )
                # A successful login clears accumulated failure debt for this
                # peer without touching the owner's route budget.
                self.limiter.forget_class("auth", _client_address(request))
                if not allowed:
                    return _rate_limited_response(
                        retry_after, class_name=route, reason="rate"
                    )
                return None
            allowed, retry_after = self.limiter.consume(
                "auth", _client_address(request)
            )
            if not allowed:
                return _rate_limited_response(
                    retry_after, class_name="auth", reason="failed_auth"
                )
            return None
        except Exception:  # noqa: BLE001 - observability/limits never break traffic
            log("rate_limit_error", level="warning", status="error")
            return None

    def _limiter_exposition(self) -> str:
        """Prometheus gauges for the two concurrency caps, appended to /metrics."""

        try:
            return self._limiter_gauges()
        except Exception:  # noqa: BLE001 - a scrape must never fail on a gauge
            return ""

    def _limiter_gauges(self) -> str:
        turn_used, turn_limit = self._turn_slots.stats()
        stream_used, stream_limit = self._stream_slots.stats()
        turn_ratio = turn_used / turn_limit if turn_limit > 0 else 0.0
        stream_ratio = stream_used / stream_limit if stream_limit > 0 else 0.0
        lines = [
            "# HELP kelp_limiter_in_use Slots currently held by a limiter.",
            "# TYPE kelp_limiter_in_use gauge",
            f'kelp_limiter_in_use{{class="turns"}} {_gauge(turn_used)}',
            f'kelp_limiter_in_use{{class="streams"}} {_gauge(stream_used)}',
            "# HELP kelp_limiter_capacity Slot limit per class; streams are per owner.",
            "# TYPE kelp_limiter_capacity gauge",
            f'kelp_limiter_capacity{{class="turns"}} {_gauge(turn_limit)}',
            f'kelp_limiter_capacity{{class="streams"}} {_gauge(stream_limit)}',
            "# HELP kelp_limiter_saturation Fraction of a limiter's capacity in use.",
            "# TYPE kelp_limiter_saturation gauge",
            f'kelp_limiter_saturation{{class="turns"}} {_gauge(turn_ratio)}',
            f'kelp_limiter_saturation{{class="streams"}} {_gauge(stream_ratio)}',
        ]
        return "\n".join(lines) + "\n"

    def _busy_response(
        self, *, class_name: str, reason: str, limit: int, retry_after: float = 1.0
    ) -> Response:
        return _rate_limited_response(
            retry_after,
            class_name=class_name,
            reason=reason,
            limit=limit,
        )

    def _route(self, request: Request, segments: list[str]) -> Response:
        if not segments:
            raise ChatError("not_found", status=404, message="Unknown endpoint.")
        head = segments[0]
        method = request.method.upper()

        if head == "threads":
            return self._threads(request, method, segments[1:])
        if head == "turns":
            return self._turns(request, method, segments[1:])
        if head == "approvals":
            return self._approvals(request, method, segments[1:])
        if head == "attachments":
            if len(segments) == 1 and method == "POST":
                return self._upload(request)
            raise ChatError("not_found", status=404, message="Unknown endpoint.")
        if head == "artifacts":
            return self._artifacts(request, method, segments[1:])
        if head == "health":
            if len(segments) != 1 or method != "GET":
                raise ChatError("not_found", status=404, message="Unknown endpoint.")
            # Legacy alias kept byte-compatible for existing supervisors.
            return _json_response(200, {"ok": True})
        if head == "healthz":
            if len(segments) != 1 or method != "GET":
                raise ChatError("not_found", status=404, message="Unknown endpoint.")
            return _json_response(200, self.health.liveness())
        if head == "readyz":
            if len(segments) != 1 or method != "GET":
                raise ChatError("not_found", status=404, message="Unknown endpoint.")
            payload, ready = self.health.readiness(runner=self.runner)
            return _json_response(200 if ready else 503, payload)
        if head == "metrics":
            if len(segments) != 1 or method != "GET":
                raise ChatError("not_found", status=404, message="Unknown endpoint.")
            # Refresh the derived spend gauges from the persisted records before
            # rendering, so a scrape is current even when no turn settled in this
            # process (a restart, or a turn that ended in another worker). The
            # refresh is best effort: a scrape must not fail because accounting
            # is momentarily unreadable (KEL-18 review M1).
            self.service.publish_spend_metrics()
            # Reached only through ``handle``, which has already authorized it.
            text = self.metrics.render() + self._limiter_exposition()
            body = text.encode("utf-8")
            return Response(
                status=200,
                headers={
                    "Content-Type": PROMETHEUS_CONTENT_TYPE,
                    "Content-Length": str(len(body)),
                    "Cache-Control": "no-store",
                },
                body=body,
            )
        raise ChatError("not_found", status=404, message="Unknown endpoint.")

    @staticmethod
    def _is_public(request: Request, segments: list[str]) -> bool:
        return request.method.upper() == "GET" and segments == ["api", "healthz"]

    # -- threads -----------------------------------------------------------

    def _threads(self, request: Request, method: str, rest: list[str]) -> Response:
        if not rest:
            if method == "GET":
                limit = _int_param(request, "limit", 50, 1, 100)
                threads, cursor = self.service.list_threads(
                    limit=limit, cursor=request.query_one("cursor")
                )
                return _json_response(
                    200, {"ok": True, "threads": threads, "next_cursor": cursor}
                )
            if method == "POST":
                body = self._json_body(request, optional=True)
                title = body.get("title")
                if title is not None and not isinstance(title, str):
                    raise ChatError(
                        "invalid_request",
                        status=400,
                        message="Title must be a string.",
                        user_fixable=True,
                        field="/title",
                    )
                thread = self.service.create_thread(title)
                return _json_response(201, {"ok": True, "thread": thread})
            raise ChatError("not_found", status=404, message="Unknown endpoint.")

        thread_id = _segment(rest[0])
        if len(rest) == 1:
            if method == "GET":
                limit = _int_param(request, "limit", 50, 1, 100)
                snapshot = self.service.get_thread(
                    thread_id,
                    limit=limit,
                    before=request.query_one("before"),
                )
                return _json_response(200, {"ok": True, **snapshot})
            if method == "PATCH":
                body = self._json_body(request)
                title = body.get("title")
                if not isinstance(title, str):
                    raise ChatError(
                        "invalid_request",
                        status=400,
                        message="Title must be a string.",
                        user_fixable=True,
                        field="/title",
                    )
                thread = self.service.rename_thread(thread_id, title)
                return _json_response(200, {"ok": True, "thread": thread})
            if method == "DELETE":
                result = self.runner.run(self.service.delete_thread(thread_id))
                return _json_response(200, result)
            raise ChatError("not_found", status=404, message="Unknown endpoint.")

        if rest[1] == "stream" and len(rest) == 2 and method == "GET":
            return self._stream(request, thread_id)
        if rest[1] == "turns" and len(rest) == 2 and method == "POST":
            return self._send(request, thread_id)
        raise ChatError("not_found", status=404, message="Unknown endpoint.")

    def _stream(self, request: Request, thread_id: str) -> Response:
        if self.service.store.get_thread(thread_id) is None:
            raise ChatError("unknown_thread", status=404)
        owner = _client_identity(request)
        if not self._stream_slots.acquire(owner):
            return self._busy_response(
                class_name="streams",
                reason="stream_capacity",
                limit=self._stream_slots.limit,
            )
        try:
            after = _after_param(request)
            if after > 0:
                # A resume asks for a position it already had: the browser is
                # reconnecting, not opening a fresh stream.
                self.metrics.inc_sse_reconnect()
            frames = sse_frames(
                self.service.store,
                self.service.bus,
                thread_id,
                after=after,
                idle=self.stream_idle,
                auth_check=lambda: self._authorized_for(request),
                thread_check=lambda: (
                    self.service.store.get_thread(thread_id) is not None
                ),
            )
        except BaseException:
            self._stream_slots.release(owner)
            raise
        return Response(
            status=200,
            headers={
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
            stream=self._lease_stream(frames, owner),
        )

    def _lease_stream(
        self, frames: Iterator[str], owner: str
    ) -> Iterator[str]:
        """Release a stream slot on exhaustion, ``close()``, or GC.

        A generator that was never started would not run its ``finally``, so
        the lease object is also released when the generator is collected.
        """

        lease = _Lease(lambda: self._stream_slots.release(owner))

        def guarded() -> Iterator[str]:
            try:
                yield from frames
            finally:
                lease.release()

        return guarded()

    def _send(self, request: Request, thread_id: str) -> Response:
        body = self._json_body(request)
        client_turn_id = body.get("client_turn_id")
        if not isinstance(client_turn_id, str) or not _CLIENT_TURN_RE.match(client_turn_id):
            raise ChatError(
                "invalid_request",
                status=400,
                message="client_turn_id is required for idempotent sends.",
                user_fixable=True,
                field="/client_turn_id",
            )
        text = body.get("text")
        if not isinstance(text, str):
            raise ChatError(
                "invalid_message",
                status=400,
                message="A message needs text.",
                user_fixable=True,
                field="/text",
            )
        handles = body.get("attachment_handles") or []
        if not isinstance(handles, list):
            raise ChatError(
                "invalid_message",
                status=400,
                message="attachment_handles must be a list.",
                user_fixable=True,
                field="/attachment_handles",
            )
        if not self._turn_slots.acquire():
            return self._busy_response(
                class_name="turns",
                reason="turn_capacity",
                limit=self._turn_slots.limit,
            )
        try:
            payload, created = self.runner.run(
                self.service.send(
                    thread_id,
                    text=text,
                    client_turn_id=client_turn_id,
                    attachment_handles=handles,
                )
            )
        except BaseException:
            # The turn never started, so its slot is returned immediately.
            self._turn_slots.release()
            raise
        if created:
            self._release_turn_slot_when_done(str(payload.get("turn_id", "")))
        else:
            # An idempotent replay owns no new work.
            self._turn_slots.release()
        return _json_response(202 if created else 200, {"ok": True, **payload})

    def _release_turn_slot_when_done(self, turn_id: str) -> None:
        """Return a turn slot when the background turn finalizes."""

        async def waiter() -> None:
            try:
                await self.service.wait_for_turn(turn_id, timeout=None)
            except BaseException:  # noqa: BLE001 - release is the contract
                pass
            finally:
                self._turn_slots.release()

        coro = waiter()
        try:
            self.runner.spawn(coro)
        except Exception:  # noqa: BLE001 - a closed runner must not leak the slot
            close = getattr(coro, "close", None)
            if close is not None:
                close()
            self._turn_slots.release()

    def _retry_turn(self, turn_id: str) -> Response:
        """Accept a retry as new work, taking a turn slot exactly like a send.

        A retry queues a fresh turn (§5.4), so it shares the global turn cap and
        returns its slot the same way ``_send`` does: when the retried turn
        finalizes, or immediately when the call fails.
        """

        if not self._turn_slots.acquire():
            return self._busy_response(
                class_name="turns",
                reason="turn_capacity",
                limit=self._turn_slots.limit,
            )
        try:
            payload = self.runner.run(self.service.retry(turn_id))
        except BaseException:
            # The retried turn never started, so its slot is returned now.
            self._turn_slots.release()
            raise
        self._release_turn_slot_when_done(str(payload.get("turn_id", "")))
        return _json_response(202, {"ok": True, **payload})

    # -- turns -------------------------------------------------------------

    def _turns(self, request: Request, method: str, rest: list[str]) -> Response:
        if len(rest) != 2 or method != "POST":
            raise ChatError("not_found", status=404, message="Unknown endpoint.")
        turn_id = _segment(rest[0])
        if rest[1] == "stop":
            payload = self.runner.run(self.service.stop(turn_id))
            return _json_response(202, {"ok": True, **payload})
        if rest[1] == "retry":
            return self._retry_turn(turn_id)
        raise ChatError("not_found", status=404, message="Unknown endpoint.")

    # -- approvals ---------------------------------------------------------

    def _approvals(self, request: Request, method: str, rest: list[str]) -> Response:
        if not rest:
            if method != "GET":
                raise ChatError("not_found", status=404, message="Unknown endpoint.")
            limit = _int_param(request, "limit", 50, 1, 200)
            approvals = self.service.list_approvals(
                status=request.query_one("status"),
                thread_id=request.query_one("thread_id"),
                limit=limit,
            )
            return _json_response(200, {"ok": True, "approvals": approvals})
        approval_id = _segment(rest[0])
        if len(rest) == 1:
            if method != "GET":
                raise ChatError("not_found", status=404, message="Unknown endpoint.")
            return _json_response(
                200, {"ok": True, "approval": self.service.get_approval(approval_id)}
            )
        if rest[1] != "decision" or len(rest) != 2 or method != "POST":
            raise ChatError("not_found", status=404, message="Unknown endpoint.")
        body = self._json_body(request, limit=_DECISION_LIMIT)
        decision = body.get("decision")
        note = body.get("note")
        if note is not None and not isinstance(note, str):
            raise ChatError(
                "invalid_request",
                status=400,
                message="note must be a string.",
                user_fixable=True,
                field="/note",
            )
        result = self.service.decide(approval_id, decision, note)
        return _json_response(200, result)

    # -- artifacts ---------------------------------------------------------

    def _upload(self, request: Request) -> Response:
        if len(request.body) > self.max_upload + 64 * 1024:
            raise ChatError("upload_too_large", status=413)
        filename, data = _multipart_file(request)
        meta = self.service.artifacts.save_upload(filename, data)
        card = self.service.artifacts.card(meta["handle"])
        return _json_response(201, {"ok": True, "artifact": card})

    def _artifacts(self, request: Request, method: str, rest: list[str]) -> Response:
        if method != "GET" or not rest:
            raise ChatError("not_found", status=404, message="Unknown endpoint.")
        handle = _segment(rest[0])
        thread_id = request.query_one("thread_id")
        if len(rest) == 1:
            card = self.service.artifacts.card(handle, thread_id=thread_id)
            return _json_response(200, {"ok": True, "artifact": card})
        action = rest[1]
        if len(rest) != 2:
            raise ChatError("not_found", status=404, message="Unknown endpoint.")
        if action == "preview":
            rows = _int_param(request, "rows", 20, 1, 20)
            preview = self.service.artifacts.preview(
                handle, rows=rows, thread_id=thread_id
            )
            return _json_response(200, preview)
        if action == "content":
            result = self.service.artifacts.content(
                handle,
                thread_id=thread_id,
                download=request.query_one("download") == "1",
                range_header=request.header("range"),
            )
            return Response(status=result.status, headers=dict(result.headers), body=result.body)
        raise ChatError("not_found", status=404, message="Unknown endpoint.")

    # -- auth --------------------------------------------------------------

    def _authorize(self, request: Request) -> None:
        if not self._authorized_for(request):
            raise ChatError("unauthorized", status=401)
        if request.method.upper() in _MUTATING and not self._csrf_ok(request):
            raise ChatError("csrf_failed", status=403)

    def _authorized_for(self, request: Request) -> bool:
        if not self.auth_token:
            return True
        header = request.header("authorization") or ""
        prefix = "Bearer "
        if header.startswith(prefix) and hmac.compare_digest(
            header[len(prefix) :], self.auth_token
        ):
            return True
        cookies = _parse_cookies(request.header("cookie"))
        session = cookies.get("session")
        return bool(session) and hmac.compare_digest(session, self.auth_token)

    def _csrf_ok(self, request: Request) -> bool:
        if self.csrf_token:
            supplied = request.header("x-csrf-token") or ""
            return hmac.compare_digest(supplied, self.csrf_token)
        origin = request.header("origin")
        if not origin:
            # Non-browser local calls without a configured token (§12 minimum).
            return True
        host = request.header("host") or ""
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return False
        return bool(host) and parsed.netloc == host

    # -- body parsing ------------------------------------------------------

    def body_limit_for(self, path: str) -> tuple[int, int, str]:
        """Return ``(cap, status, code)`` for a request path.

        The transport adapter uses this to reject a declared body before any
        bytes are read. Uploads keep their richer ``413 upload_too_large``;
        every other route reports ``400 invalid_request``, matching
        :meth:`_json_body`.
        """

        try:
            clean = unquote(urlsplit(path).path)
            segments = [segment for segment in clean.split("/") if segment]
        except ValueError:  # pragma: no cover - urlsplit rarely raises here
            segments = []
        if len(segments) >= 2 and segments[0] == "api" and segments[1] == "attachments":
            return self.max_upload + _UPLOAD_OVERHEAD, 413, "upload_too_large"
        return _JSON_LIMIT, 400, "invalid_request"

    def _json_body(self, request: Request, *, optional: bool = False, limit: int = _JSON_LIMIT) -> dict[str, Any]:
        if not request.body:
            if optional:
                return {}
            raise ChatError(
                "invalid_request",
                status=400,
                message="A JSON object is required.",
                user_fixable=True,
            )
        if len(request.body) > limit:
            raise ChatError(
                "invalid_request",
                status=400,
                message="The request body is too large.",
                user_fixable=True,
            )
        try:
            body = json.loads(request.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ChatError(
                "invalid_request",
                status=400,
                message="The request body is not valid JSON.",
                user_fixable=True,
            ) from None
        if not isinstance(body, dict):
            raise ChatError(
                "invalid_request",
                status=400,
                message="The request body must be a JSON object.",
                user_fixable=True,
            )
        return body


def _json_response(status: int, payload: Mapping[str, Any]) -> Response:
    body = json.dumps(payload, default=str, separators=(",", ":")).encode("utf-8")
    return Response(
        status=status,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(body)),
            "Cache-Control": "no-store",
        },
        body=body,
    )


def _segment(value: str) -> str:
    value = unquote(value)
    if not _THREAD_RE.match(value) or value in {".", ".."}:
        raise ChatError("not_found", status=404, message="Unknown endpoint.")
    return value


def _int_param(request: Request, name: str, default: int, low: int, high: int) -> int:
    raw = request.query_one(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ChatError(
            "invalid_request",
            status=400,
            message=f"{name} must be an integer.",
            user_fixable=True,
        ) from None
    return max(low, min(value, high))


def _after_param(request: Request) -> int:
    raw = request.header("last-event-id") or request.query_one("after") or "0"
    try:
        value = int(raw)
    except ValueError:
        raise ChatError(
            "invalid_request",
            status=400,
            message="after must be a sequence number.",
            user_fixable=True,
        ) from None
    return max(0, value)


def _parse_cookies(value: str | None) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in (value or "").split(";"):
        if "=" in part:
            name, _, raw = part.partition("=")
            cookies[name.strip()] = raw.strip()
    return cookies


def _multipart_file(request: Request) -> tuple[str, bytes]:
    content_type = request.header("content-type") or ""
    if not content_type.lower().startswith("multipart/form-data"):
        raise ChatError(
            "invalid_request",
            status=400,
            message="Uploads must be multipart/form-data.",
            user_fixable=True,
        )
    match = re.search(r'boundary="?([^";]+)"?', content_type)
    if match is None:
        raise ChatError(
            "invalid_request",
            status=400,
            message="The multipart boundary is missing.",
            user_fixable=True,
        )
    boundary = b"--" + match.group(1).encode("ascii", "ignore")
    for chunk in request.body.split(boundary):
        if not chunk or chunk.startswith(b"--"):
            continue
        chunk = chunk.lstrip(b"\r\n")
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        head, separator, data = chunk.partition(b"\r\n\r\n")
        if not separator:
            continue
        disposition = ""
        for line in head.split(b"\r\n"):
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-disposition":
                disposition = value.decode("utf-8", "replace")
        if 'name="file"' not in disposition:
            continue
        file_match = re.search(r'filename="([^"]*)"', disposition)
        filename = file_match.group(1) if file_match else "upload"
        return filename, data
    raise ChatError(
        "invalid_request",
        status=400,
        message="No file field in the upload.",
        user_fixable=True,
    )


# -- stdlib adapter --------------------------------------------------------


class _BodyRejected(Exception):
    """A body that must be refused before it is read into memory."""

    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


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

    def _dispatch(self) -> None:
        parsed = urlsplit(self.path)
        try:
            body = self._read_body(parsed.path)
        except _BodyRejected as rejected:
            self._respond(_body_rejection_response(rejected), close=True)
            return
        except (ConnectionError, OSError):
            # The peer vanished mid-body; there is nobody to answer and the
            # partially consumed stream cannot be reused.
            self.close_connection = True
            return
        headers = {key.lower(): value for key, value in self.headers.items()}
        request = Request(
            method=self.command,
            path=parsed.path,
            query=parse_qs(parsed.query, keep_blank_values=True),
            headers=headers,
            body=body,
            client=self.client_address[0] if self.client_address else None,
        )
        self._respond(self.api.handle(request))

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
        if response.stream is None and "content-length" not in names:
            self.send_header("Content-Length", str(len(response.body)))
        if close and "connection" not in names:
            self.send_header("Connection", "close")
        self.end_headers()
        try:
            if response.stream is not None:
                for frame in response.stream:
                    self.wfile.write(frame.encode("utf-8"))
                    self.wfile.flush()
            else:
                self.wfile.write(response.body)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

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

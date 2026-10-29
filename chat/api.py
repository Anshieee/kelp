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
import hmac
import inspect
import json
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
    "Request",
    "Response",
    "make_server",
    "serve",
]

_THREAD_RE = re.compile(r"^[A-Za-z0-9:_-]{1,80}$")
_CLIENT_TURN_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_MUTATING = frozenset({"POST", "PATCH", "DELETE", "PUT"})
_JSON_LIMIT = 1024 * 1024
_DECISION_LIMIT = 1024
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
        self._run_probe(lambda: _http_provider_probe(base_url, api_key), runner)

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


async def _http_provider_probe(base_url: str, api_key: str | None) -> None:
    """One bounded ``GET /models``: only a 2xx means reachable and authenticated.

    A redirect, an auth rejection, or a gateway error is not a healthy upstream,
    so anything outside ``200..299`` fails the check.
    """

    import httpx

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    timeout = httpx.Timeout(
        _PROVIDER_HTTP_TIMEOUT, connect=min(1.0, _PROVIDER_HTTP_TIMEOUT)
    )
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        response = await client.get(
            f"{base_url.rstrip('/')}/models", headers=headers
        )
    if not 200 <= response.status_code < 300:
        raise OSError("provider probe was rejected")


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
    ):
        self.service = service
        self.auth_token = auth_token
        self.csrf_token = csrf_token
        self.stream_idle = stream_idle
        self.max_upload = max_upload
        self.health = health or HealthProbe.from_service(service)
        self.metrics = metrics or get_metrics()
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
            body = self.metrics.render().encode("utf-8")
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
            thread_check=lambda: self.service.store.get_thread(thread_id) is not None,
        )
        return Response(
            status=200,
            headers={
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
            stream=frames,
        )

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
        payload, created = self.runner.run(
            self.service.send(
                thread_id,
                text=text,
                client_turn_id=client_turn_id,
                attachment_handles=handles,
            )
        )
        return _json_response(202 if created else 200, {"ok": True, **payload})

    # -- turns -------------------------------------------------------------

    def _turns(self, request: Request, method: str, rest: list[str]) -> Response:
        if len(rest) != 2 or method != "POST":
            raise ChatError("not_found", status=404, message="Unknown endpoint.")
        turn_id = _segment(rest[0])
        if rest[1] == "stop":
            payload = self.runner.run(self.service.stop(turn_id))
            return _json_response(202, {"ok": True, **payload})
        if rest[1] == "retry":
            payload = self.runner.run(self.service.retry(turn_id))
            return _json_response(202, {"ok": True, **payload})
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
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        headers = {key.lower(): value for key, value in self.headers.items()}
        request = Request(
            method=self.command,
            path=parsed.path,
            query=parse_qs(parsed.query, keep_blank_values=True),
            headers=headers,
            body=body,
        )
        response = self.api.handle(request)
        self.send_response(response.status)
        for name, value in response.headers.items():
            if name.lower() == "content-length" and response.stream is not None:
                continue
            self.send_header(name, value)
        if response.stream is None and "content-length" not in {
            name.lower() for name in response.headers
        }:
            self.send_header("Content-Length", str(len(response.body)))
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

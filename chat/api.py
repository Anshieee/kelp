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
import json
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

from .artifacts import ArtifactError, MAX_UPLOAD_BYTES
from .errors import ChatError
from .service import ChatService
from .stream import IDLE_SECONDS, sse_frames

__all__ = [
    "ChatAPI",
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
    ):
        self.service = service
        self.auth_token = auth_token
        self.csrf_token = csrf_token
        self.stream_idle = stream_idle
        self.max_upload = max_upload
        self._owns_runner = runner is None
        self.runner = runner or LoopRunner()

    def close(self) -> None:
        if self._owns_runner:
            self.runner.close()

    # -- dispatch ----------------------------------------------------------

    def handle(self, request: Request) -> Response:
        try:
            self._authorize(request)
            path = unquote(urlsplit(request.path).path)
            segments = [segment for segment in path.split("/") if segment]
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
            return _json_response(200, {"ok": True})
        raise ChatError("not_found", status=404, message="Unknown endpoint.")

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
        # Request line only: ids, status, duration. No bodies, no arguments.
        super().log_message(format, *args)


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

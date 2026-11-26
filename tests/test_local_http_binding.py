"""Opt-in loopback HTTP binding for the local sidecar.

These tests use a real disposable loopback socket. They prove that the hosted
default is untouched, that the ``local_only`` server refuses to start unless the
literal loopback address and a configured owner bearer are both present, and
that every request path is rejected before the body is read, before the route
or query is parsed, before ``ChatAPI.handle`` runs, and before the stdlib can
emit ``100 Continue``.

No fixture starts a real service, provider, or external connection: the store,
fake provider, and API are the same doubles the chat tests use.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

from chat import ChatAPI, ChatService, ChatStore
from chat.api import make_server

from .chatkit import MODEL, StubProvider, text_script

AUTH = "local-owner-bearer-0123456789"
CSRF = "local-csrf-token"


def _make_api(
    tmp_path: Path,
    *,
    auth_token: str | None = AUTH,
    name: str = "chat.db",
) -> tuple[ChatAPI, ChatStore]:
    store = ChatStore(tmp_path / name)
    service = ChatService(
        store=store,
        provider=StubProvider([text_script("Hello")]),
        model=MODEL,
    )
    api = ChatAPI(service, auth_token=auth_token, csrf_token=CSRF)
    api.stream_idle = 0.02
    return api, store


@contextmanager
def _running(api: ChatAPI, *, local_only: bool) -> Iterator[int]:
    server = make_server(api, "127.0.0.1", 0, local_only=local_only)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def _origin(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _request(
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 5.0,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def _raw(
    port: int,
    headers: list[str],
    *,
    body: bytes = b"",
    timeout: float = 5.0,
) -> bytes:
    """Send exactly ``headers`` and ``body`` and read the complete response."""

    payload = ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(payload)
        received = bytearray()
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                return bytes(received)
            received.extend(chunk)


def _sse(port: int, path: str, headers: dict[str, str], *, timeout: float = 5.0) -> bytes:
    """Read one SSE stream up to its first keep-alive frame, then close.

    The stream route deliberately replies ``Connection: keep-alive``, which
    (via ``send_header``) keeps the stdlib connection open even when the
    request asked to close. The test must not wait for EOF; it stops after the
    known finite frame and lets the daemon handler observe the client close.
    """

    payload = (
        f"GET {path} HTTP/1.1\r\n"
        + "\r\n".join(f"{name}: {value}" for name, value in headers.items())
        + "\r\n\r\n"
    ).encode("ascii")
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(payload)
        received = bytearray()
        while b": keep-alive\n\n" not in received:
            chunk = sock.recv(65536)
            if not chunk:
                break
            received.extend(chunk)
        return bytes(received)


def _error(raw: bytes) -> dict[str, Any]:
    head, _, body = raw.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 ")
    return json.loads(body)


def _error_body(body: bytes) -> dict[str, Any]:
    return json.loads(body)


def _local_headers(port: int, *, bearer: str = AUTH) -> dict[str, str]:
    return {
        "Origin": _origin(port),
        "Authorization": f"Bearer {bearer}",
    }


def _track_handle(api: ChatAPI) -> list[Any]:
    """Record every application dispatch; a rejected request must add none."""

    calls: list[Any] = []
    original = api.handle

    def recording(request: Any) -> Any:
        calls.append(request)
        return original(request)

    api.handle = recording  # type: ignore[method-assign]
    return calls


def test_local_only_refuses_nonliteral_host_and_empty_bearer(
    tmp_path: Path,
) -> None:
    api, store = _make_api(tmp_path)
    try:
        with pytest.raises(ValueError):
            make_server(api, "0.0.0.0", 0, local_only=True)
        with pytest.raises(ValueError):
            make_server(api, "localhost", 0, local_only=True)
    finally:
        api.close()
        store.close()

    # Both preconditions are independent: a valid literal loopback address is
    # still refused when the API has no owner bearer to enforce.
    empty_api, empty_store = _make_api(tmp_path, auth_token="", name="empty.db")
    try:
        with pytest.raises(ValueError):
            make_server(empty_api, "127.0.0.1", 0, local_only=True)
        # The hosted default remains constructible with an empty bearer.
        hosted = make_server(empty_api)
        hosted.server_close()
    finally:
        empty_api.close()
        empty_store.close()


def test_hosted_default_keeps_public_health_without_local_headers(
    tmp_path: Path,
) -> None:
    api, store = _make_api(tmp_path)
    try:
        with _running(api, local_only=False) as port:
            status, headers, body = _request(port, "GET", "/api/healthz")
        assert status == 200
        assert json.loads(body)["ok"] is True
        assert "Access-Control-Allow-Origin" not in headers
    finally:
        api.close()
        store.close()


def test_local_guard_protects_public_health_sse_and_static(
    tmp_path: Path,
) -> None:
    api, store = _make_api(tmp_path)
    try:
        with _running(api, local_only=True) as port:
            # Even routes that are public in the hosted deployment, a share
            # link, and static mounts must pass the Local guard first.
            for path in (
                "/api/healthz",
                "/api/shared/share-token",
                "/api/threads/t/stream",
                "/phone/",
            ):
                status, headers, body = _request(
                    port, "GET", path, headers={"Origin": _origin(port)}
                )
                assert status == 401, (path, body)
                assert _error_body(body)["error"]["code"] == "unauthorized"
                assert AUTH.encode() not in body
                assert "Access-Control-Allow-Origin" not in headers
                assert "Set-Cookie" not in headers

            # A cookie is ambient state, never the Local owner credential.
            status, _headers, body = _request(
                port,
                "GET",
                "/api/healthz",
                headers={
                    "Origin": _origin(port),
                    "Cookie": f"KELP_AUTH_TOKEN={AUTH}",
                },
            )
            assert status == 401
            assert _error_body(body)["error"]["code"] == "unauthorized"
            assert AUTH.encode() not in body
    finally:
        api.close()
        store.close()


def test_local_guard_rejects_duplicate_raw_singletons(tmp_path: Path) -> None:
    api, store = _make_api(tmp_path)
    try:
        with _running(api, local_only=True) as port:
            host = f"127.0.0.1:{port}"
            origin = f"http://{host}"
            cases = [
                (
                    [
                        "GET /api/healthz HTTP/1.1",
                        f"Host: {host}",
                        f"Host: {origin}",
                        f"Origin: {origin}",
                        f"Authorization: Bearer {AUTH}",
                        "Connection: close",
                    ],
                    400,
                    "invalid_host",
                ),
                (
                    [
                        "GET /api/healthz HTTP/1.1",
                        f"Host: {host}",
                        f"Origin: {origin}",
                        "Origin: http://evil.example",
                        f"Authorization: Bearer {AUTH}",
                        "Connection: close",
                    ],
                    403,
                    "invalid_origin",
                ),
                (
                    [
                        "GET /api/healthz HTTP/1.1",
                        f"Host: {host}",
                        f"Origin: {origin}",
                        f"Authorization: Bearer {AUTH}",
                        f"Authorization: Bearer {AUTH}",
                        "Connection: close",
                    ],
                    401,
                    "unauthorized",
                ),
            ]
            for headers, status, code in cases:
                raw = _raw(port, headers, timeout=3)
                assert raw.startswith(f"HTTP/1.1 {status} ".encode()), raw[:80]
                assert b"\r\n\r\n" in raw
                assert _error(raw)["error"]["code"] == code
                assert AUTH.encode() not in raw
                assert b"Access-Control-Allow-Origin" not in raw
                assert b"Set-Cookie" not in raw
    finally:
        api.close()
        store.close()


def test_local_guard_is_reached_by_expect_before_100_continue(
    tmp_path: Path,
) -> None:
    api, store = _make_api(tmp_path)
    try:
        with _running(api, local_only=True) as port:
            host = f"127.0.0.1:{port}"
            started = time.monotonic()
            raw = _raw(
                port,
                [
                    "POST /api/threads HTTP/1.1",
                    f"Host: {host}",
                    f"Origin: http://{host}",
                    "Authorization: Bearer wrong-owner-bearer",
                    "Content-Type: application/json",
                    "Content-Length: 64",
                    "Expect: 100-continue",
                    "Connection: close",
                ],
                # The body is deliberately never sent. A correct guard answers
                # from the headers alone; a 100 Continue would wait for bytes
                # that never arrive.
                timeout=3,
            )
            elapsed = time.monotonic() - started
        assert raw.startswith(b"HTTP/1.1 401 ")
        assert b"100 Continue" not in raw
        assert _error(raw)["error"]["code"] == "unauthorized"
        assert elapsed < 2.0
    finally:
        api.close()
        store.close()


def test_local_authorized_rest_health_and_sse_stream(tmp_path: Path) -> None:
    api, store = _make_api(tmp_path)
    try:
        with _running(api, local_only=True) as port:
            status, headers, body = _request(
                port, "GET", "/api/healthz", headers=_local_headers(port)
            )
            assert status == 200
            assert json.loads(body)["ok"] is True
            assert "Access-Control-Allow-Origin" not in headers

            status, _headers, body = _request(
                port,
                "POST",
                "/api/threads",
                headers={
                    **_local_headers(port),
                    "Content-Type": "application/json",
                },
                body=b"{}",
            )
            assert status == 403
            assert json.loads(body)["error"]["code"] == "csrf_failed"

            status, _headers, body = _request(
                port,
                "POST",
                "/api/threads",
                headers={
                    **_local_headers(port),
                    "Content-Type": "application/json",
                    "X-CSRF-Token": CSRF,
                },
                body=b'{"title":"local"}',
            )
            assert status == 201, body
            thread_id = json.loads(body)["thread"]["id"]

            # A real SSE route, finite because the idle clock is short. The
            # stream route answers ``Connection: keep-alive``, so read exactly
            # one keep-alive frame and close; no stale socket timeout and no
            # leased slot survives.
            raw = _sse(
                port,
                f"/api/threads/{thread_id}/stream",
                {
                    "Host": f"127.0.0.1:{port}",
                    **_local_headers(port),
                    "Connection": "close",
                },
                timeout=5,
            )
            head, _, stream = raw.partition(b"\r\n\r\n")
            assert head.startswith(b"HTTP/1.1 200 ")
            assert b"Content-Type: text/event-stream" in head
            assert stream.startswith(b": keep-alive")
            deadline = time.monotonic() + 2
            while api._stream_slots.stats()[0] and time.monotonic() < deadline:
                time.sleep(0.01)
            assert api._stream_slots.stats()[0] == 0
    finally:
        api.close()
        store.close()


def test_local_guard_precedes_method_lookup_and_error_text(
    tmp_path: Path,
) -> None:
    api, store = _make_api(tmp_path)
    try:
        with _running(api, local_only=True) as port:
            host = f"127.0.0.1:{port}"
            # The stdlib would answer 501 for BREW after parsing; the guard
            # must answer the missing owner credential first.
            raw = _raw(
                port,
                [
                    "BREW /api/healthz HTTP/1.1",
                    f"Host: {host}",
                    f"Origin: http://{host}",
                    "Connection: close",
                ],
                timeout=3,
            )
            assert raw.startswith(b"HTTP/1.1 401 ")
            assert _error(raw)["error"]["code"] == "unauthorized"
            assert b"Unsupported method" not in raw

            # Once L1 accepts the request, the stdlib's method lookup is the
            # only remaining ``send_error`` caller, so its 501 survives as the
            # same fixed JSON envelope rather than the stdlib HTML page.
            raw = _raw(
                port,
                [
                    "BREW /api/healthz HTTP/1.1",
                    f"Host: {host}",
                    f"Origin: http://{host}",
                    f"Authorization: Bearer {AUTH}",
                    "Connection: close",
                ],
                timeout=3,
            )
            assert raw.startswith(b"HTTP/1.1 501 "), raw[:120]
            assert _error(raw)["error"]["code"] == "invalid_request"
            assert b"Unsupported method" not in raw

            # A malformed request version is a stdlib error before headers; the
            # Local renderer is fixed JSON and never echoes the parser text.
            raw = _raw(
                port,
                [
                    "GET /api/healthz HTTP/9.9",
                    f"Host: {host}",
                    f"Origin: http://{host}",
                    f"Authorization: Bearer {AUTH}",
                    "Connection: close",
                ],
                timeout=3,
            )
            assert raw.startswith(b"HTTP/1.1 400 ")
            assert _error(raw)["error"]["code"] == "invalid_request"
            assert b"9.9" not in raw
            assert b"Bad request" not in raw
    finally:
        api.close()
        store.close()


def test_local_guard_rejects_malformed_parsed_header_block(
    tmp_path: Path,
) -> None:
    api, store = _make_api(tmp_path)
    try:
        calls = _track_handle(api)
        with _running(api, local_only=True) as port:
            host = f"127.0.0.1:{port}"
            # ``http.client.parse_headers`` keeps the fields before a malformed
            # line and records ``Message.defects`` instead of raising. The
            # three valid-looking headers below must not be enough to pass.
            raw = _raw(
                port,
                [
                    "GET /api/healthz HTTP/1.1",
                    f"Host: {host}",
                    f"Origin: http://{host}",
                    f"Authorization: Bearer {AUTH}",
                    "Connection: close",
                    "MalformedHeaderWithoutColon",
                ],
                timeout=3,
            )
            assert raw.startswith(b"HTTP/1.1 400 "), raw[:120]
            assert _error(raw)["error"]["code"] == "invalid_request"
            assert AUTH.encode() not in raw

            # A malformed line can swallow the following ``Connection``
            # header. The guard's own close must still terminate the response
            # instead of leaving the client waiting on a keep-alive socket.
            raw = _raw(
                port,
                [
                    "GET /api/healthz HTTP/1.1",
                    f"Host: {host}",
                    f"Origin: http://{host}",
                    f"Authorization: Bearer {AUTH}",
                    "MalformedHeaderWithoutColon",
                    "Connection: close",
                ],
                timeout=3,
            )
            assert raw.startswith(b"HTTP/1.1 400 "), raw[:120]
            assert _error(raw)["error"]["code"] == "invalid_request"
        # The defect is refused before the route, body, or handler is touched.
        assert calls == []
    finally:
        api.close()
        store.close()


def test_local_guard_rejects_malformed_header_before_expect_100(
    tmp_path: Path,
) -> None:
    api, store = _make_api(tmp_path)
    try:
        calls = _track_handle(api)
        with _running(api, local_only=True) as port:
            host = f"127.0.0.1:{port}"
            # A body is supplied so a regressed guard fails deterministically
            # on the 100 Continue prefix instead of timing out on a missing
            # body. The defect must still be rejected before ``do_POST``.
            raw = _raw(
                port,
                [
                    "POST /api/threads HTTP/1.1",
                    f"Host: {host}",
                    f"Origin: http://{host}",
                    f"Authorization: Bearer {AUTH}",
                    "Content-Type: application/json",
                    "Content-Length: 2",
                    "Expect: 100-continue",
                    "Connection: close",
                    "MalformedHeaderWithoutColon",
                ],
                body=b"{}",
                timeout=3,
            )
            assert raw.startswith(b"HTTP/1.1 400 "), raw[:120]
            assert b"100 Continue" not in raw
            assert _error(raw)["error"]["code"] == "invalid_request"
            assert AUTH.encode() not in raw
        assert calls == []
    finally:
        api.close()
        store.close()


def test_local_guard_rejects_http_0_9_with_parsed_headers(
    tmp_path: Path,
) -> None:
    api, store = _make_api(tmp_path)
    try:
        calls = _track_handle(api)
        with _running(api, local_only=True) as port:
            host = f"127.0.0.1:{port}"
            # Python 3.12 parses a header block even when the two-word request
            # line leaves ``request_version`` at its HTTP/0.9 default. The
            # headers alone must never be treated as proof of HTTP/1.x.
            raw = _raw(
                port,
                [
                    "GET /api/healthz",
                    f"Host: {host}",
                    f"Origin: http://{host}",
                    f"Authorization: Bearer {AUTH}",
                    "Expect: 100-continue",
                    "Connection: close",
                ],
                timeout=3,
            )
            assert raw.startswith(b"HTTP/1.1 400 "), raw[:120]
            assert b"100 Continue" not in raw
            assert _error(raw)["error"]["code"] == "invalid_request"
            assert AUTH.encode() not in raw
        assert calls == []
    finally:
        api.close()
        store.close()


def test_local_profile_rejects_off_profile_targets_before_routing(
    tmp_path: Path,
) -> None:
    api, store = _make_api(tmp_path)
    try:
        calls = _track_handle(api)
        with _running(api, local_only=True) as port:
            host = f"127.0.0.1:{port}"
            origin = f"http://{host}"
            cases = [
                ("GET", "/api/auth/session", None),
                ("GET", "/api/readyz", None),
                ("GET", "/api/shared/share-token", None),
                ("GET", "/phone/", None),
                ("GET", "/api/threads?limit=1&extra=2", None),
                ("GET", "/api/threads?limit=", None),
                ("GET", "/api/threads/%41", None),
                ("GET", "/api/threads/abc/stream?after=1&extra=2", None),
                ("POST", "/api/approvals/abc/decision", b"{}"),
                ("POST", "/api/threads/abc/turns?limit=1", b"{}"),
            ]
            for method, path, body in cases:
                headers = {
                    "Host": host,
                    "Origin": origin,
                    "Authorization": f"Bearer {AUTH}",
                    "Connection": "close",
                }
                if body is not None:
                    headers["Content-Type"] = "application/json"
                status, response_headers, response_body = _request(
                    port, method, path, headers=headers, body=body
                )
                assert status == 404, (method, path, response_body)
                assert _error_body(response_body)["error"]["code"] == "not_found"
                assert response_headers.get("Connection") == "close"
                assert "Access-Control-Allow-Origin" not in response_headers
                assert "Set-Cookie" not in response_headers
                assert AUTH.encode() not in response_body

            # gh-87389: the stdlib collapses a leading ``//`` in ``self.path``
            # before this guard runs, so an exact-target matcher reading that
            # attribute would accept the network-path alias. The request-line
            # token is the target the caller actually sent, so the alias gets
            # the same fixed 404 and the application is never called.
            raw = _raw(
                port,
                [
                    "GET //api/health HTTP/1.1",
                    f"Host: {host}",
                    f"Origin: {origin}",
                    f"Authorization: Bearer {AUTH}",
                    "Connection: close",
                ],
                timeout=3,
            )
            assert raw.startswith(b"HTTP/1.1 404 "), raw[:120]
            assert _error(raw)["error"]["code"] == "not_found"
            assert AUTH.encode() not in raw
            assert b"Access-Control-Allow-Origin" not in raw
            assert b"Set-Cookie" not in raw
        assert calls == []
    finally:
        api.close()
        store.close()


def test_local_profile_rejects_off_profile_expect_before_100(
    tmp_path: Path,
) -> None:
    api, store = _make_api(tmp_path)
    try:
        calls = _track_handle(api)
        with _running(api, local_only=True) as port:
            host = f"127.0.0.1:{port}"
            started = time.monotonic()
            raw = _raw(
                port,
                [
                    "POST /api/auth/logout HTTP/1.1",
                    f"Host: {host}",
                    f"Origin: http://{host}",
                    f"Authorization: Bearer {AUTH}",
                    "Content-Type: application/json",
                    "Content-Length: 64",
                    "Expect: 100-continue",
                    "Connection: close",
                ],
                # The body is never sent: the off-profile answer must come
                # from the target alone, never after a 100 Continue waits for
                # bytes that will not arrive.
                timeout=3,
            )
            elapsed = time.monotonic() - started
        assert raw.startswith(b"HTTP/1.1 404 "), raw[:120]
        assert b"100 Continue" not in raw
        assert _error(raw)["error"]["code"] == "not_found"
        assert elapsed < 2.0
        assert calls == []
    finally:
        api.close()
        store.close()


def test_chat_api_explicit_environ_suppresses_ambient_defaults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("KELP_MAX_CONCURRENT_TURNS", "9")
    monkeypatch.setenv("KELP_RATE_LIMIT_ENABLED", "0")

    store = ChatStore(tmp_path / "environ.db")
    service = ChatService(
        store=store,
        provider=StubProvider([text_script("Hello")]),
        model=MODEL,
    )
    ambient = ChatAPI(service, auth_token=AUTH, csrf_token=CSRF)
    try:
        assert ambient._turn_slots.limit == 9
        assert ambient.limiter.enabled is False

        explicit = ChatAPI(
            service, auth_token=AUTH, csrf_token=CSRF, environ={}
        )
        try:
            assert explicit._turn_slots.limit == 4
            assert explicit.limiter.enabled is True
        finally:
            explicit.close()

        mapped = ChatAPI(
            service,
            auth_token=AUTH,
            csrf_token=CSRF,
            environ={
                "KELP_MAX_CONCURRENT_TURNS": "2",
                "KELP_RATE_LIMIT_ENABLED": "0",
            },
        )
        try:
            assert mapped._turn_slots.limit == 2
            assert mapped.limiter.enabled is False
        finally:
            mapped.close()
    finally:
        ambient.close()
        store.close()


def test_local_host_must_name_the_actual_bound_port(tmp_path: Path) -> None:
    api, store = _make_api(tmp_path)
    try:
        with _running(api, local_only=True) as port:
            wrong = port + 1 if port < 65535 else port - 1
            raw = _raw(
                port,
                [
                    "GET /api/healthz HTTP/1.1",
                    f"Host: 127.0.0.1:{wrong}",
                    f"Origin: http://127.0.0.1:{wrong}",
                    f"Authorization: Bearer {AUTH}",
                    "Connection: close",
                ],
                timeout=3,
            )
        assert raw.startswith(b"HTTP/1.1 400 ")
        assert _error(raw)["error"]["code"] == "invalid_host"
    finally:
        api.close()
        store.close()

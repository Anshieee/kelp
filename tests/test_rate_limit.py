"""KEL-17 M3-A: token buckets, failed-auth throttling, body caps, concurrency.

The limiter sits in front of ``ChatAPI._dispatch``. These tests drive the HTTP
contract directly (``ChatAPI.handle``) and the stdlib adapter over a socket for
the pre-read body rules, which only exist at the transport boundary.
"""

from __future__ import annotations

import gc
import json
import socket
import threading
from pathlib import Path
from typing import Any, Mapping

from chat import ChatService, ChatStore
from chat.api import (
    ChatAPI,
    RateLimiter,
    Request,
    Response,
    TokenBucket,
    make_server,
)
from kernel import TurnBudget
from kernel.provider import ProviderFailure, TextDelta

from .chatkit import MODEL, StubProvider, text_script

AUTH = {"authorization": "Bearer tok"}
CSRF = {"x-csrf-token": "csrf"}

_GENEROUS = {name: (1_000_000.0, 1_000_000.0) for name in
             ("auth", "chat", "upload", "read", "admin")}


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_api(
    tmp_path: Path,
    *,
    limiter: RateLimiter | None = None,
    auth_token: str | None = "tok",
    csrf_token: str | None = "csrf",
    max_upload: int = 20 * 1024 * 1024,
    max_concurrent_turns: int = 4,
    max_streams_per_owner: int = 3,
) -> tuple[ChatAPI, ChatService, ChatStore]:
    store = ChatStore(tmp_path / "chat.db")
    service = ChatService(
        store=store,
        provider=StubProvider([text_script("Hello")]),
        model=MODEL,
    )
    api = ChatAPI(
        service,
        auth_token=auth_token,
        csrf_token=csrf_token,
        max_upload=max_upload,
        max_concurrent_turns=max_concurrent_turns,
        max_streams_per_owner=max_streams_per_owner,
        limiter=limiter,
    )
    api.stream_idle = 0.05
    return api, service, store


def call(
    api: ChatAPI,
    method: str,
    path: str,
    body: Mapping[str, Any] | None = None,
    *,
    headers: Mapping[str, str] | None = None,
    auth: bool = True,
    csrf: bool = True,
    client: str | None = None,
) -> Response:
    request_headers: dict[str, str] = {}
    if auth:
        request_headers.update(AUTH)
    if csrf and method.upper() in {"POST", "PATCH", "DELETE"}:
        request_headers.update(CSRF)
    if body is not None:
        request_headers["content-type"] = "application/json"
    request_headers.update(headers or {})
    raw = json.dumps(body).encode() if body is not None else b""
    return api.handle(
        Request(method, path, {}, request_headers, raw, client)
    )


def payload(response: Response) -> dict[str, Any]:
    return json.loads(response.body)


def thread(api: ChatAPI) -> dict[str, Any]:
    response = call(api, "POST", "/api/threads", {})
    assert response.status == 201, response.body
    return payload(response)["thread"]


# -- token bucket -----------------------------------------------------------


def test_token_bucket_bursts_then_recovers() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate_per_second=1.0, capacity=2.0, now=clock())

    assert bucket.take(now=clock())[0] is True
    assert bucket.take(now=clock())[0] is True
    allowed, retry_after = bucket.take(now=clock())
    assert allowed is False
    assert 0.0 <= retry_after <= 1.0

    clock.advance(1.0)
    assert bucket.take(now=clock())[0] is True


def test_route_burst_returns_429_with_rate_limited_and_retry_after(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    limiter = RateLimiter(
        {**_GENEROUS, "chat": (60.0, 2.0)}, clock=clock
    )
    api, _service, store = make_api(tmp_path, limiter=limiter)
    try:
        assert call(api, "POST", "/api/threads", {}).status == 201
        assert call(api, "POST", "/api/threads", {}).status == 201

        limited = call(api, "POST", "/api/threads", {})
        assert limited.status == 429
        assert limited.headers["Retry-After"].isdigit()
        assert int(limited.headers["Retry-After"]) >= 1
        error = payload(limited)["error"]
        assert error["code"] == "rate_limited"
        assert error["retryable"] is True
        assert error["details"]["class"] == "chat"

        # One second of refill buys exactly one more request.
        clock.advance(1.01)
        assert call(api, "POST", "/api/threads", {}).status == 201
    finally:
        api.close()
        store.close()


def test_per_key_independence(tmp_path: Path) -> None:
    clock = FakeClock()
    limiter = RateLimiter({**_GENEROUS, "chat": (60.0, 1.0)}, clock=clock)
    # No token means the peer address is the identity, so distinct callers
    # have distinct buckets.
    api, _service, store = make_api(
        tmp_path, limiter=limiter, auth_token=None, csrf_token=None
    )
    try:
        first = call(
            api, "POST", "/api/threads", {}, auth=False, csrf=False,
            headers={"x-forwarded-for": "198.51.100.10"},
        )
        assert first.status == 201
        exhausted = call(
            api, "POST", "/api/threads", {}, auth=False, csrf=False,
            headers={"x-forwarded-for": "198.51.100.10"},
        )
        assert exhausted.status == 429

        other = call(
            api, "POST", "/api/threads", {}, auth=False, csrf=False,
            headers={"x-forwarded-for": "198.51.100.11"},
        )
        assert other.status == 201, other.body
    finally:
        api.close()
        store.close()


# -- failed auth ------------------------------------------------------------


def test_failed_auth_is_throttled_independently_with_idle_out(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    limiter = RateLimiter(
        {**_GENEROUS, "auth": (6.0, 1.0), "read": (600.0, 300.0)},
        clock=clock,
        idle_seconds=10.0,
    )
    api, _service, store = make_api(tmp_path, limiter=limiter)
    try:
        first = call(api, "GET", "/api/threads", auth=False)
        assert first.status == 401
        assert payload(first)["error"]["code"] == "unauthorized"

        # The auth bucket, not the read/chat budget, is now exhausted.
        throttled = call(api, "GET", "/api/threads", auth=False)
        assert throttled.status == 429
        assert throttled.headers["Retry-After"].isdigit()
        assert payload(throttled)["error"]["details"]["class"] == "auth"

        # A valid request is untouched by the failure bucket.
        assert call(api, "GET", "/api/threads").status == 200
        assert call(api, "POST", "/api/threads", {}).status == 201

        # Idle-out discards the failure bucket; the next bad attempt is a 401.
        clock.advance(10.1)
        assert call(api, "GET", "/api/threads", auth=False).status == 401
    finally:
        api.close()
        store.close()


def test_limiter_faults_fail_open(monkeypatch: Any, tmp_path: Path) -> None:
    class BrokenLimiter(RateLimiter):
        def consume(self, class_name: str, key: str, cost: float = 1.0):
            raise RuntimeError("bucket exploded")

    api, _service, store = make_api(tmp_path, limiter=BrokenLimiter())
    try:
        response = call(api, "GET", "/api/threads")
        assert response.status == 200, response.body
    finally:
        api.close()
        store.close()


# -- route/environment configuration ---------------------------------------


def test_limiter_reads_rate_limits_from_env() -> None:
    limiter = RateLimiter.from_env(
        {
            "KELP_RATE_LIMIT_CHAT_RPM": "1",
            "KELP_RATE_LIMIT_CHAT_BURST": "0",
            "KELP_RATE_LIMIT_AUTH_RPM": "12",
            "KELP_RATE_LIMIT_AUTH_BURST": "3",
            "KELP_RATE_LIMIT_ENABLED": "1",
        }
    )
    assert limiter.limits["chat"] == (1.0, 0.0)
    assert limiter.limits["auth"] == (12.0, 3.0)
    allowed, _retry = limiter.consume("chat", "any")
    assert allowed is False

    disabled = RateLimiter.from_env({"KELP_RATE_LIMIT_ENABLED": "0"})
    assert disabled.consume("chat", "any")[0] is True


# -- transport body caps ----------------------------------------------------


def _serve(api: ChatAPI) -> tuple[Any, threading.Thread]:
    server = make_server(api, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _raw_exchange(
    server: Any, request: bytes, *, timeout: float = 3.0
) -> bytes:
    host, port = server.server_address[0], int(server.server_address[1])
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(request)
        # The header-only requests carry no body. Half-closing tells the server
        # there is nothing more to read without waiting for a timeout.
        sock.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while True:
            block = sock.recv(65536)
            if not block:
                break
            chunks.append(block)
    return b"".join(chunks)


def _split(data: bytes) -> tuple[bytes, bytes]:
    head, _, body = data.partition(b"\r\n\r\n")
    return head, body


def test_oversized_content_length_is_rejected_before_read(
    tmp_path: Path,
) -> None:
    api, _service, store = make_api(tmp_path, max_upload=32)
    server, _thread = _serve(api)
    try:
        request = (
            b"POST /api/attachments HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Authorization: Bearer tok\r\n"
            b"X-CSRF-Token: csrf\r\n"
            b"Content-Type: multipart/form-data; boundary=x\r\n"
            b"Content-Length: 104857600\r\n"
            b"\r\n"
        )
        head, body = _split(_raw_exchange(server, request))
        assert b" 413 " in head
        assert b"Connection: close" in head
        error = json.loads(body)["error"]
        assert error["code"] == "upload_too_large"
    finally:
        server.shutdown()
        server.server_close()
        api.close()
        store.close()


def test_non_numeric_content_length_is_a_typed_400(tmp_path: Path) -> None:
    api, _service, store = make_api(tmp_path)
    server, _thread = _serve(api)
    try:
        request = (
            b"POST /api/threads HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Authorization: Bearer tok\r\n"
            b"X-CSRF-Token: csrf\r\n"
            b"Content-Length: not-a-number\r\n"
            b"\r\n"
        )
        head, body = _split(_raw_exchange(server, request))
        assert b" 400 " in head
        assert b" 500 " not in head
        error = json.loads(body)["error"]
        assert error["code"] == "invalid_request"
    finally:
        server.shutdown()
        server.server_close()
        api.close()
        store.close()


def test_chunked_over_cap_is_aborted_mid_read(tmp_path: Path) -> None:
    api, _service, store = make_api(tmp_path, max_upload=32)
    server, _thread = _serve(api)
    try:
        # Declare one chunk larger than the cap and send no payload: the
        # handler must reject on the size line rather than wait for bytes.
        request = (
            b"POST /api/attachments HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Authorization: Bearer tok\r\n"
            b"X-CSRF-Token: csrf\r\n"
            b"Content-Type: multipart/form-data; boundary=x\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
            b"20000\r\n"
        )
        head, body = _split(_raw_exchange(server, request))
        assert b" 413 " in head
        error = json.loads(body)["error"]
        assert error["code"] == "upload_too_large"
    finally:
        server.shutdown()
        server.server_close()
        api.close()
        store.close()


def test_content_length_with_transfer_encoding_is_a_typed_400(
    tmp_path: Path,
) -> None:
    api, _service, store = make_api(tmp_path)
    server, _thread = _serve(api)
    try:
        # Both framings on one message are a smuggling vector (RFC 7230
        # §3.3.3). The body here is a valid chunked JSON object, so a 201
        # would mean the ambiguity had been resolved instead of refused.
        request = (
            b"POST /api/threads HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Authorization: Bearer tok\r\n"
            b"X-CSRF-Token: csrf\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 2\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
            b"2\r\n{}\r\n0\r\n\r\n"
        )
        head, body = _split(_raw_exchange(server, request))
        assert b" 400 " in head
        assert b" 500 " not in head
        assert b"Connection: close" in head
        error = json.loads(body)["error"]
        assert error["code"] == "invalid_request"
    finally:
        server.shutdown()
        server.server_close()
        api.close()
        store.close()


# -- concurrency ------------------------------------------------------------


def test_stream_cap_per_owner_returns_rate_limited(tmp_path: Path) -> None:
    api, _service, store = make_api(tmp_path, max_streams_per_owner=1)
    try:
        thread_row = thread(api)
        path = f"/api/threads/{thread_row['id']}/stream"

        first = call(api, "GET", path)
        assert first.status == 200
        assert first.stream is not None

        second = call(api, "GET", path)
        assert second.status == 429
        error = payload(second)["error"]
        assert error["code"] == "rate_limited"
        assert error["details"]["reason"] == "stream_capacity"
        assert second.headers["Retry-After"].isdigit()

        # Closing the stream releases its slot, even if it never yielded.
        first.stream.close()  # type: ignore[union-attr]
        first = None  # type: ignore[assignment]
        gc.collect()
        third = call(api, "GET", path)
        assert third.status == 200, third.body
        third.stream.close()  # type: ignore[union-attr]
    finally:
        api.close()
        store.close()


def test_turn_cap_returns_typed_busy_and_releases_after_turn(
    tmp_path: Path,
) -> None:
    api, service, store = make_api(tmp_path, max_concurrent_turns=1)
    try:
        thread_row = thread(api)
        # Occupy the only slot the same way a long-running turn would.
        assert api._turn_slots.acquire() is True

        blocked = call(
            api,
            "POST",
            f"/api/threads/{thread_row['id']}/turns",
            {"client_turn_id": "ct-rate-1", "text": "hello"},
        )
        assert blocked.status == 429
        error = payload(blocked)["error"]
        assert error["code"] == "rate_limited"
        assert error["details"]["reason"] == "turn_capacity"
        assert blocked.headers["Retry-After"].isdigit()

        api._turn_slots.release()
        accepted = call(
            api,
            "POST",
            f"/api/threads/{thread_row['id']}/turns",
            {"client_turn_id": "ct-rate-2", "text": "hello"},
        )
        assert accepted.status == 202, accepted.body
        turn_id = payload(accepted)["turn_id"]
        api.runner.run(service.wait_for_turn(turn_id, timeout=5.0))
        # The release monitor returned the slot when the turn finalized.
        used, limit = api._turn_slots.stats()
        assert (used, limit) == (0, 1)
    finally:
        api.close()
        store.close()


def test_retry_takes_and_returns_a_turn_slot(tmp_path: Path) -> None:
    class TimingOut:
        async def stream(self, request: object) -> Any:
            yield TextDelta("partial")
            yield ProviderFailure("provider_timeout", "timed out", retryable=True)

    store = ChatStore(tmp_path / "chat.db")
    service = ChatService(store=store, provider=TimingOut(), model=MODEL)
    api = ChatAPI(
        service, auth_token="tok", csrf_token="csrf", max_concurrent_turns=1
    )
    try:
        thread_row = thread(api)
        sent = call(
            api,
            "POST",
            f"/api/threads/{thread_row['id']}/turns",
            {"client_turn_id": "ct-retry-1", "text": "question"},
        )
        assert sent.status == 202, sent.body
        turn_id = payload(sent)["turn_id"]
        api.runner.run(service.wait_for_turn(turn_id, timeout=5.0))
        assert api._turn_slots.stats() == (0, 1)

        # A retry that fails before queueing work returns its slot at once.
        missing = call(
            api, "POST", "/api/turns/turn_00000000000000000000000000000000/retry", {}
        )
        assert missing.status == 404, missing.body
        assert api._turn_slots.stats() == (0, 1)

        # With the cap occupied, a retry is refused exactly like a send.
        assert api._turn_slots.acquire() is True
        blocked = call(api, "POST", f"/api/turns/{turn_id}/retry", {})
        assert blocked.status == 429, blocked.body
        error = payload(blocked)["error"]
        assert error["code"] == "rate_limited"
        assert error["details"]["reason"] == "turn_capacity"
        assert blocked.headers["Retry-After"].isdigit()
        assert api._turn_slots.stats() == (1, 1)

        # The freed slot admits the retry, which holds it until that turn ends.
        api._turn_slots.release()
        accepted = call(api, "POST", f"/api/turns/{turn_id}/retry", {})
        assert accepted.status == 202, accepted.body
        retried = payload(accepted)
        assert retried["retry_of"] == turn_id
        assert api._turn_slots.stats() == (1, 1)

        api.runner.run(service.wait_for_turn(retried["turn_id"], timeout=5.0))
        assert api._turn_slots.stats() == (0, 1)
    finally:
        api.close()
        store.close()


def test_metrics_exposes_limiter_saturation_gauges(tmp_path: Path) -> None:
    api, _service, store = make_api(
        tmp_path, max_concurrent_turns=4, max_streams_per_owner=2
    )
    try:
        assert api._turn_slots.acquire() is True
        assert api._stream_slots.acquire("owner:test") is True

        response = call(api, "GET", "/api/metrics")
        assert response.status == 200
        text = response.body.decode()
        assert "# TYPE kelp_limiter_saturation gauge" in text
        assert 'kelp_limiter_in_use{class="turns"} 1' in text
        assert 'kelp_limiter_capacity{class="turns"} 4' in text
        assert 'kelp_limiter_in_use{class="streams"} 1' in text
        assert 'kelp_limiter_saturation{class="turns"} 0.25' in text
        assert 'kelp_limiter_saturation{class="streams"} 0.5' in text
    finally:
        api._turn_slots.release()
        api._stream_slots.release("owner:test")
        api.close()
        store.close()


# -- budgets stay untouched -------------------------------------------------


def test_turn_budget_defaults_are_unchanged(tmp_path: Path) -> None:
    budget = TurnBudget()
    assert (
        budget.max_steps,
        budget.max_output_tokens,
        budget.max_tokens_per_step,
        budget.max_tool_calls,
        budget.max_wall_seconds,
    ) == (6, 4096, 1024, 8, 120.0)

    api, service, store = make_api(tmp_path)
    try:
        # The API limiter is not a TurnBudget and never mutates the kernel's.
        assert service._budget.max_steps == 6
        assert service._budget.max_tool_calls == 8
        assert service._budget.max_wall_seconds == 120.0
    finally:
        api.close()
        store.close()

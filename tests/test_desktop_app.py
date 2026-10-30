"""Desktop-app contract tests (Tauri thin client + desk pet).

Pins the server surface the desktop client depends on, so the client and
server cannot drift apart:

- auth flow: bearer exchange (CHAT_SPEC §12.2) mints an HttpOnly session,
  cookie auth works, sign-out revokes;
- approval round-trip: pending card -> approve/deny decision over HTTP,
  idempotent replay, unknown id is 404;
- SSE reconnect: resume with ``after=`` replays only missed frames;
- pet state mapping: ``desktop/pet_state.py`` (mirrored in
  ``desktop/src/orb.js``) maps live SSE events to working/waiting/done/idle.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest

from chat import ChatAPI, ChatService, ChatStore, Request
from chat.api import AuthFailureTracker, RateLimiter, SessionStore
from chat.cards import approval_card
from chat.logging import configure_logging
from chat.stream import EventBus, sse_frames
from desktop.pet_state import PET_STATES, pet_state_from_event

from .chatkit import MODEL, StubProvider, text_script

ROOT_TOKEN = "root-owner-token-for-desktop-tests"


def _make_api(tmp_path: Path) -> tuple[ChatAPI, ChatStore, SessionStore]:
    store = ChatStore(tmp_path / "chat.db")
    service = ChatService(
        store=store,
        provider=StubProvider([text_script("Hello")]),
        model=MODEL,
    )
    sessions = SessionStore(ttl=30 * 24 * 60 * 60)
    api = ChatAPI(
        service,
        auth_token=ROOT_TOKEN,
        sessions=sessions,
        auth_failures=AuthFailureTracker(),
        cookie_secure=True,
        limiter=RateLimiter(),
    )
    return api, store, sessions


def _request(
    method: str,
    path: str,
    *,
    bearer: str | None = None,
    cookie: str | None = None,
    csrf: str | None = None,
    body: bytes = b"",
) -> Request:
    headers: dict[str, str] = {}
    if bearer is not None:
        headers["authorization"] = f"Bearer {bearer}"
    if cookie is not None:
        headers["cookie"] = cookie
    if csrf is not None:
        headers["x-csrf-token"] = csrf
        headers["origin"] = "http://localhost"
        headers["host"] = "localhost"
    elif method in {"POST", "PATCH", "PUT", "DELETE"}:
        headers["origin"] = "http://localhost"
        headers["host"] = "localhost"
    return Request(method, path, {}, headers, body)


def _body(response: Any) -> dict[str, Any]:
    return json.loads(response.body)


def _cookie_value(cookies: list[str], name: str) -> str:
    prefix = f"{name}="
    for cookie in cookies:
        if cookie.startswith(prefix):
            return cookie[len(prefix):].split(";", 1)[0]
    raise AssertionError(f"missing {name} cookie in {cookies!r}")


@pytest.fixture(autouse=True)
def _quiet_logging() -> Iterator[None]:
    configure_logging(level="info", secret_values=(), stream=None, enabled=False)
    yield
    configure_logging(level="info", secret_values=(), stream=None, enabled=False)


def _sign_in(api: ChatAPI) -> tuple[str, str]:
    response = api.handle(_request("POST", "/api/auth/session", bearer=ROOT_TOKEN))
    assert response.status == 201, response.body
    return (
        _cookie_value(response.cookies, "session"),
        _cookie_value(response.cookies, "csrf"),
    )


def _cookie_header(session_id: str, csrf: str) -> str:
    return f"session={session_id}; csrf={csrf}"


# -- auth flow: the desktop sign-in -------------------------------------------


def test_desktop_sign_in_exchanges_bearer_for_session_cookie(tmp_path: Path) -> None:
    api, store, sessions = _make_api(tmp_path)
    try:
        response = api.handle(
            _request(
                "POST",
                "/api/auth/session",
                bearer=ROOT_TOKEN,
                body=json.dumps({"device_label": "Kelp desktop"}).encode(),
            )
        )
        assert response.status == 201, response.body
        session = _body(response)["session"]
        assert session["device"]["label"] == "Kelp desktop"
        assert "csrf_token" in session

        session_cookie = next(c for c in response.cookies if c.startswith("session="))
        assert "HttpOnly" in session_cookie
        assert "Secure" in session_cookie
        assert "SameSite=Lax" in session_cookie
        # The raw bearer is never echoed back.
        assert ROOT_TOKEN.encode() not in response.body

        # Cookie auth works for reads without ever re-sending the bearer.
        session_id = _cookie_value(response.cookies, "session")
        csrf = _cookie_value(response.cookies, "csrf")
        listed = api.handle(
            _request("GET", "/api/threads", cookie=_cookie_header(session_id, csrf))
        )
        assert listed.status == 200, listed.body
    finally:
        api.close()
        store.close()


def test_desktop_sign_in_rejects_wrong_bearer(tmp_path: Path) -> None:
    api, store, _sessions = _make_api(tmp_path)
    try:
        response = api.handle(_request("POST", "/api/auth/session", bearer="nope"))
        assert response.status == 401, response.body
    finally:
        api.close()
        store.close()


def test_desktop_sign_out_revokes_session(tmp_path: Path) -> None:
    api, store, _sessions = _make_api(tmp_path)
    try:
        session_id, csrf = _sign_in(api)
        cookie = _cookie_header(session_id, csrf)
        revoked = api.handle(
            _request("DELETE", "/api/auth/session", cookie=cookie, csrf=csrf)
        )
        assert revoked.status == 200, revoked.body
        after = api.handle(_request("GET", "/api/threads", cookie=cookie))
        assert after.status == 401
    finally:
        api.close()
        store.close()


def _save_skill_approval(store: ChatStore, thread_id: str, approval_id: str) -> None:
    now = datetime.now(timezone.utc)
    card = approval_card(
        approval_id=approval_id,
        source="skill",
        thread_id=thread_id,
        turn_id="turn_1",
        call_id="call_1",
        tool="demo.tool",
        arguments={"target": "inbox"},
        title="File an item",
        requested_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=10)).isoformat(),
    )
    store.save_approval(card)


# -- approval round-trip: cards over HTTP -------------------------------------


def test_desktop_approval_round_trip_over_http(tmp_path: Path) -> None:
    api, store, _sessions = _make_api(tmp_path)
    try:
        session_id, csrf = _sign_in(api)
        cookie = _cookie_header(session_id, csrf)
        thread = api.service.create_thread("desktop approvals")
        _save_skill_approval(store, thread["id"], "appr_1")

        pending = api.handle(
            _request(
                "GET", "/api/approvals?status=pending", cookie=cookie,
            )
        )
        assert pending.status == 200, pending.body
        assert [a["id"] for a in _body(pending)["approvals"]] == ["appr_1"]

        decided = api.handle(
            _request(
                "POST",
                "/api/approvals/appr_1/decision",
                cookie=cookie,
                csrf=csrf,
                body=json.dumps({"decision": "approve"}).encode(),
            )
        )
        assert decided.status == 200, decided.body
        assert _body(decided)["approval"]["status"] == "approved"

        # Idempotent replay of the same decision.
        replay = api.handle(
            _request(
                "POST",
                "/api/approvals/appr_1/decision",
                cookie=cookie,
                csrf=csrf,
                body=json.dumps({"decision": "approve"}).encode(),
            )
        )
        assert replay.status == 200, replay.body
        assert _body(replay).get("idempotent") is True

        missing = api.handle(
            _request(
                "POST",
                "/api/approvals/nope/decision",
                cookie=cookie,
                csrf=csrf,
                body=json.dumps({"decision": "approve"}).encode(),
            )
        )
        assert missing.status == 404
    finally:
        api.close()
        store.close()


def test_desktop_approval_deny_over_http(tmp_path: Path) -> None:
    api, store, _sessions = _make_api(tmp_path)
    try:
        session_id, csrf = _sign_in(api)
        cookie = _cookie_header(session_id, csrf)
        thread = api.service.create_thread("desktop deny")
        _save_skill_approval(store, thread["id"], "appr_2")
        denied = api.handle(
            _request(
                "POST",
                "/api/approvals/appr_2/decision",
                cookie=cookie,
                csrf=csrf,
                body=json.dumps({"decision": "deny", "note": "not now"}).encode(),
            )
        )
        assert denied.status == 200, denied.body
        assert _body(denied)["approval"]["status"] == "denied"
    finally:
        api.close()
        store.close()


# -- SSE reconnect: resume from the last seen id -------------------------------


def _frame_payload(frame: str) -> dict[str, Any]:
    data = next(
        line[len("data: "):]
        for line in frame.splitlines()
        if line.startswith("data: ")
    )
    return json.loads(data)


def test_desktop_sse_reconnect_resumes_without_loss_or_duplicates(
    tmp_path: Path,
) -> None:
    store = ChatStore(tmp_path / "chat.db")
    try:
        thread = store.create_thread()
        thread_id = thread["id"]
        for index in range(5):
            store.append_event(thread_id, "token_delta", {"text": f"t{index}"})

        bus = EventBus()

        # First connection reads frames 1-3, then drops.
        first = list(
            sse_frames(store, bus, thread_id, after=0, heartbeat=600, idle=600,
                       max_events=3)
        )
        assert [_frame_payload(f)["seq"] for f in first] == [1, 2, 3]

        # Reconnect resumes after the last seen id: only 4-5, no duplicates.
        seen = _frame_payload(first[-1])["seq"]
        bus2 = EventBus()
        resumed = list(
            sse_frames(store, bus2, thread_id, after=seen, heartbeat=600,
                       idle=600, max_events=2)
        )
        assert [_frame_payload(f)["seq"] for f in resumed] == [4, 5]

        # A resume at the tip yields nothing new (heartbeat/idle close only).
        bus3 = EventBus()
        closing: list[str] = []

        def _collect() -> None:
            frames = sse_frames(
                store, bus3, thread_id, after=5, heartbeat=0.01, idle=0.05,
                clock=time.monotonic,
            )
            for frame in frames:
                if not frame.startswith(":"):
                    closing.append(frame)

        worker = threading.Thread(target=_collect)
        worker.start()
        worker.join(timeout=5)
        assert closing == []
    finally:
        store.close()


# -- pet state mapping ----------------------------------------------------------


def test_desktop_pet_states_cover_working_waiting_done_idle() -> None:
    assert set(PET_STATES) == {"working", "waiting", "done", "idle"}


def test_desktop_pet_state_follows_live_sse_stream() -> None:
    assert pet_state_from_event({"type": "turn_started"}) == "working"
    assert pet_state_from_event({"type": "token_delta"}) == "working"
    assert pet_state_from_event({"type": "tool_call"}) == "working"
    assert pet_state_from_event({"type": "approval_created"}) == "waiting"
    assert pet_state_from_event({"type": "approval_pending"}) == "waiting"
    assert pet_state_from_event({"type": "approval_decided"}) == "working"
    assert pet_state_from_event({"type": "turn_finished"}) == "done"
    assert pet_state_from_event({"type": "turn_failed"}) == "idle"
    assert pet_state_from_event({"type": "stream_closed"}) == "idle"


def test_desktop_pet_state_ignores_unknown_and_malformed_events() -> None:
    assert pet_state_from_event({"type": "heartbeat"}) is None
    assert pet_state_from_event({}) is None
    assert pet_state_from_event(None) is None
    assert pet_state_from_event({"type": 42}) is None

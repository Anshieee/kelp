"""KEL-24 M4-C: persisted owner device sessions.

These tests pin the multi-device contract: two devices survive a process
restart and keep a valid derived CSRF pair, the device list marks exactly the
calling cookie as current, revoking one device closes its live stream while the
other stays usable, and no session id or CSRF token is ever written to the
database, logs, or audit trail.
"""

from __future__ import annotations

import io
import json
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

from chat import ChatAPI, ChatService, ChatStore, Request
from chat import api as api_module
from chat.api import SessionStore
from chat.logging import configure_logging

from .chatkit import MODEL, StubProvider, text_script

ROOT_TOKEN = "root-owner-token-for-devices"
SESSION_SECRET = "session-secret-for-device-tests"
ORIGIN = "http://localhost"


def _make_api(
    db_path: Path,
    *,
    secret: str = SESSION_SECRET,
    clock: Any = time.time,
    max_entries: int = 1024,
) -> tuple[ChatAPI, ChatStore, SessionStore]:
    store = ChatStore(db_path)
    service = ChatService(
        store=store,
        provider=StubProvider([text_script("Hello")]),
        model=MODEL,
    )
    sessions = SessionStore(
        ttl=30 * 24 * 60 * 60,
        max_entries=max_entries,
        clock=clock,
        store=store,
        secret=secret,
    )
    api = ChatAPI(
        service,
        auth_token=ROOT_TOKEN,
        sessions=sessions,
        cookie_secure=True,
    )
    return api, store, sessions


def _request(
    method: str,
    path: str,
    *,
    bearer: str | None = None,
    cookie: str | None = None,
    csrf: str | None = None,
    origin: str | None = None,
    host: str | None = None,
    body: bytes = b"",
) -> Request:
    headers: dict[str, str] = {}
    if bearer is not None:
        headers["authorization"] = f"Bearer {bearer}"
    if cookie is not None:
        headers["cookie"] = cookie
    if csrf is not None:
        headers["x-csrf-token"] = csrf
    if origin is not None:
        headers["origin"] = origin
    if host is not None:
        headers["host"] = host
    return Request(method, path, {}, headers, body)


def _cookie_value(cookies: list[str], name: str) -> str:
    prefix = f"{name}="
    for cookie in cookies:
        if cookie.startswith(prefix):
            return cookie[len(prefix) :].split(";", 1)[0]
    raise AssertionError(f"missing {name} cookie in {cookies!r}")


def _cookie_header(session_id: str, csrf: str | None = None) -> str:
    value = f"session={session_id}"
    if csrf is not None:
        value += f"; csrf={csrf}"
    return value


def _body(response: Any) -> dict[str, Any]:
    return json.loads(response.body)


def _sign_in(
    api: ChatAPI, *, label: str | None = None
) -> tuple[str, str, dict[str, Any]]:
    payload = {} if label is None else {"device_label": label}
    response = api.handle(
        _request(
            "POST",
            "/api/auth/session",
            bearer=ROOT_TOKEN,
            body=json.dumps(payload).encode("utf-8"),
        )
    )
    assert response.status == 201, response.body
    session_id = _cookie_value(response.cookies, "session")
    csrf = _cookie_value(response.cookies, "csrf")
    return session_id, csrf, _body(response)["session"]


def _mutate(
    api: ChatAPI, session_id: str, csrf: str, *, path: str = "/api/threads"
) -> Any:
    return api.handle(
        _request(
            "POST",
            path,
            cookie=_cookie_header(session_id, csrf),
            csrf=csrf,
            origin=ORIGIN,
            host="localhost",
            body=b"{}",
        )
    )


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    configure_logging(level="info", secret_values=(), stream=None, enabled=False)
    yield
    configure_logging(level="info", secret_values=(), stream=None, enabled=False)


def test_two_devices_survive_restart_and_keep_a_valid_csrf_pair(
    tmp_path: Path,
) -> None:
    now = [1_000_000.0]
    db_path = tmp_path / "chat.db"
    api, store, _sessions = _make_api(db_path, clock=lambda: now[0])
    try:
        a_id, a_csrf, a_device = _sign_in(api, label="Laptop")
        b_id, b_csrf, b_device = _sign_in(api, label="Phone")
        assert a_device["device"]["label"] == "Laptop"
        assert b_device["device"]["label"] == "Phone"
    finally:
        api.close()
        store.close()

    # A fresh process, same database and same server secret.
    api, store, sessions = _make_api(db_path, clock=lambda: now[0])
    try:
        assert sessions.lookup(a_id) is not None
        assert sessions.lookup(b_id) is not None

        read = api.handle(_request("GET", "/api/threads", cookie=_cookie_header(a_id)))
        assert read.status == 200, read.body

        # The CSRF half is derived, not stored, so the pre-restart pair still
        # authorizes a mutation on both devices.
        a_write = _mutate(api, a_id, a_csrf)
        assert a_write.status == 201, a_write.body
        b_write = _mutate(api, b_id, b_csrf)
        assert b_write.status == 201, b_write.body

        listed = api.handle(
            _request("GET", "/api/auth/devices", cookie=_cookie_header(a_id))
        )
        assert listed.status == 200, listed.body
        labels = sorted(device["label"] for device in _body(listed)["devices"])
        assert labels == ["Laptop", "Phone"]
    finally:
        api.close()
        store.close()


def test_device_list_marks_only_the_calling_cookie_as_current(
    tmp_path: Path,
) -> None:
    api, store, _sessions = _make_api(tmp_path / "chat.db")
    try:
        a_id, _a_csrf, a_device = _sign_in(api, label="Laptop")
        b_id, _b_csrf, b_device = _sign_in(api, label="Phone")
        a_short = a_device["device"]["id"]
        b_short = b_device["device"]["id"]
        assert a_short != b_short

        listed = api.handle(
            _request("GET", "/api/auth/devices", cookie=_cookie_header(a_id))
        )
        assert listed.status == 200, listed.body
        devices = {device["id"]: device for device in _body(listed)["devices"]}
        assert set(devices) == {a_short, b_short}
        assert devices[a_short]["current"] is True
        assert devices[b_short]["current"] is False
        assert all(device["online"] for device in devices.values())

        # A bearer caller has no session cookie, so no device is "current".
        bearer_list = api.handle(
            _request("GET", "/api/auth/devices", bearer=ROOT_TOKEN)
        )
        assert bearer_list.status == 200
        assert not any(
            device["current"] for device in _body(bearer_list)["devices"]
        )
    finally:
        api.close()
        store.close()


def test_revoke_one_device_closes_its_stream_and_leaves_the_other_working(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Keep the stream loop snappy: the auth check still runs between comments.
    original_frames = api_module.sse_frames

    def fast_frames(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("heartbeat", 0.05)
        kwargs.setdefault("idle", 2.0)
        return original_frames(*args, **kwargs)

    monkeypatch.setattr(api_module, "sse_frames", fast_frames)

    api, store, _sessions = _make_api(tmp_path / "chat.db")
    try:
        a_id, a_csrf, _a_device = _sign_in(api, label="Laptop")
        b_id, _b_csrf, b_device = _sign_in(api, label="Phone")
        b_short = b_device["device"]["id"]

        created = api.handle(
            _request("POST", "/api/threads", bearer=ROOT_TOKEN, body=b"{}")
        )
        assert created.status == 201, created.body
        thread_id = _body(created)["thread"]["id"]

        opened = api.handle(
            _request(
                "GET",
                f"/api/threads/{thread_id}/stream",
                cookie=_cookie_header(b_id),
            )
        )
        assert opened.status == 200, opened.body
        assert opened.stream is not None
        stream = opened.stream
        try:
            # The first frame proves the stream is live before revocation.
            assert next(stream).startswith(":")

            revoked = api.handle(
                _request(
                    "DELETE",
                    f"/api/auth/devices/{b_short}",
                    cookie=_cookie_header(a_id, a_csrf),
                    csrf=a_csrf,
                    origin=ORIGIN,
                    host="localhost",
                )
            )
            assert revoked.status == 200, revoked.body
            assert _body(revoked)["revoked"] == 1

            # The next auth check sees the fresh DB row and ends the iterator.
            with pytest.raises(StopIteration):
                next(stream)
        finally:
            stream.close()

        b_after = api.handle(
            _request("GET", "/api/threads", cookie=_cookie_header(b_id))
        )
        assert b_after.status == 401
        assert _body(b_after)["error"]["code"] == "unauthorized"

        a_after = _mutate(api, a_id, a_csrf)
        assert a_after.status == 201, a_after.body
    finally:
        api.close()
        store.close()


def test_database_never_contains_the_session_id_or_csrf_token(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "chat.db"
    api, store, _sessions = _make_api(db_path)
    try:
        session_id, csrf, device = _sign_in(api, label="Laptop")
        id_short = device["device"]["id"]
        # Force the WAL back into the main file before inspecting bytes.
        store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        raw = db_path.read_bytes()
        assert session_id.encode() not in raw
        assert csrf.encode() not in raw
        # Only the digest and its public short prefix are at rest.
        row = store._conn.execute("SELECT id_hash, label FROM sessions").fetchone()
        assert row is not None
        assert row["id_hash"].startswith(id_short)
        assert row["label"] == "Laptop"
    finally:
        api.close()
        store.close()


def test_session_cap_evicts_the_oldest_live_device_across_restarts(
    tmp_path: Path,
) -> None:
    now = [1_000_000.0]
    db_path = tmp_path / "chat.db"
    api, store, sessions = _make_api(
        db_path, clock=lambda: now[0], max_entries=2
    )
    try:
        minted: list[str] = []
        for index in range(5):
            session_id, _csrf, _device = _sign_in(api, label=f"Device {index}")
            minted.append(session_id)
            now[0] += 1
        assert len(sessions) <= 2
        count = store._conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()[
            "n"
        ]
        assert count <= 2
        assert sessions.lookup(minted[0]) is None
        assert sessions.lookup(minted[-1]) is not None
    finally:
        api.close()
        store.close()

    api, store, sessions = _make_api(
        db_path, clock=lambda: now[0], max_entries=2
    )
    try:
        assert len(sessions) <= 2
    finally:
        api.close()
        store.close()


def test_device_label_is_scrubbed_defaulted_and_never_logged(
    tmp_path: Path,
) -> None:
    stream = io.StringIO()
    configure_logging(level="info", secret_values=(), stream=stream, enabled=True)
    api, store, _sessions = _make_api(tmp_path / "chat.db")
    try:
        _id, _csrf, defaulted = _sign_in(api)
        assert defaulted["device"]["label"] == "Unknown device"

        _id, _csrf, scrubbed = _sign_in(
            api, label="  Alice\u0000's\n\n laptop\u202e  "
        )
        assert scrubbed["device"]["label"] == "Alice's laptop"

        too_long = api.handle(
            _request(
                "POST",
                "/api/auth/session",
                bearer=ROOT_TOKEN,
                body=json.dumps({"device_label": "x" * 41}).encode("utf-8"),
            )
        )
        assert too_long.status == 400
        assert _body(too_long)["error"]["code"] == "invalid_request"

        listed = api.handle(
            _request("GET", "/api/auth/devices", bearer=ROOT_TOKEN)
        )
        assert listed.status == 200

        # Labels live in the owner's device list, never in a log line.
        rendered = stream.getvalue()
        assert "Alice" not in rendered
        assert "laptop" not in rendered
        assert "Device" not in rendered
        assert "Unknown device" not in rendered
    finally:
        api.close()
        store.close()

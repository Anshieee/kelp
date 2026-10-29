"""KEL-19 M3-E: server-side owner sessions, CSRF, and auth-failure bounds.

These tests pin the owner-auth contract: a bearer token still works, a sign-in
mints an HttpOnly/Secure/SameSite=Lax cookie with a 30-day rolling expiry,
cookie mutations need a same-origin request plus a per-session double-submit
pair, revoke and revoke-all invalidate immediately, and every rejected
credential produces exactly one ``auth_failed`` record whose identity is a
digest rather than a secret.
"""

from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

from chat import ChatAPI, ChatService, ChatStore, Request, make_server
from chat.api import AuthFailureTracker, RateLimiter, SessionStore
from chat.logging import configure_logging

from .chatkit import MODEL, StubProvider, text_script

ROOT_TOKEN = "root-owner-token-for-tests"


class _NoopProvider:
    async def stream(self, request: Any) -> Any:  # pragma: no cover - never called
        raise AssertionError("no turn should run")
        yield  # pragma: no cover - makes this an async generator


def _make_api(
    tmp_path: Path,
    *,
    clock: Any = time.time,
    ttl: float = 30 * 24 * 60 * 60,
    attempts: int = 10,
    lockout_seconds: float = 300.0,
    idle_seconds: float = 300.0,
    csrf_token: str | None = None,
    limiter: RateLimiter | None = None,
    max_streams_per_owner: int = 3,
) -> tuple[ChatAPI, ChatStore, SessionStore, AuthFailureTracker]:
    store = ChatStore(tmp_path / "chat.db")
    service = ChatService(
        store=store,
        provider=StubProvider([text_script("Hello")]),
        model=MODEL,
    )
    sessions = SessionStore(ttl=ttl, clock=clock)
    failures = AuthFailureTracker(
        attempts=attempts,
        lockout_seconds=lockout_seconds,
        idle_seconds=idle_seconds,
        clock=clock,
    )
    api = ChatAPI(
        service,
        auth_token=ROOT_TOKEN,
        csrf_token=csrf_token,
        sessions=sessions,
        auth_failures=failures,
        cookie_secure=True,
        limiter=limiter,
        max_streams_per_owner=max_streams_per_owner,
    )
    return api, store, sessions, failures


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


def _sign_in(api: ChatAPI) -> tuple[Any, str, str]:
    response = api.handle(
        _request("POST", "/api/auth/session", bearer=ROOT_TOKEN)
    )
    assert response.status == 201, response.body
    session_id = _cookie_value(response.cookies, "session")
    csrf = _cookie_value(response.cookies, "csrf")
    return response, session_id, csrf


def _cookie_value(cookies: list[str], name: str) -> str:
    prefix = f"{name}="
    for cookie in cookies:
        if cookie.startswith(prefix):
            return cookie[len(prefix):].split(";", 1)[0]
    raise AssertionError(f"missing {name} cookie in {cookies!r}")


def _cookie_header(session_id: str, csrf: str | None = None) -> str:
    value = f"session={session_id}"
    if csrf is not None:
        value += f"; csrf={csrf}"
    return value


def _body(response: Any) -> dict[str, Any]:
    return json.loads(response.body)


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    configure_logging(level="info", secret_values=(), stream=None, enabled=False)
    yield
    configure_logging(level="info", secret_values=(), stream=None, enabled=False)


def test_sign_in_sets_httponly_secure_samesite_cookie_with_30_day_expiry(
    tmp_path: Path,
) -> None:
    api, store, sessions, _failures = _make_api(tmp_path)
    try:
        response, session_id, csrf = _sign_in(api)
        session_cookie = next(
            cookie for cookie in response.cookies if cookie.startswith("session=")
        )
        assert "HttpOnly" in session_cookie
        assert "Secure" in session_cookie
        assert "SameSite=Lax" in session_cookie
        assert "Path=/" in session_cookie
        assert "Max-Age=2592000" in session_cookie

        csrf_cookie = next(
            cookie for cookie in response.cookies if cookie.startswith("csrf=")
        )
        assert "HttpOnly" not in csrf_cookie
        assert "Secure" in csrf_cookie
        assert "SameSite=Lax" in csrf_cookie

        # The raw session id is only in the HttpOnly cookie; the response body
        # carries the readable CSRF half and no session credential.
        assert session_id.encode() not in response.body
        assert _body(response)["session"]["csrf_token"] == csrf
        assert sessions.lookup(session_id) is not None
    finally:
        api.close()
        store.close()


def test_cookie_auth_and_rolling_expiry(tmp_path: Path) -> None:
    now = [1_000_000.0]
    api, store, sessions, _failures = _make_api(
        tmp_path, clock=lambda: now[0], ttl=100.0
    )
    try:
        _response, session_id, _csrf = _sign_in(api)
        initial = sessions.lookup(session_id)
        assert initial is not None
        initial_expiry = initial.expires_at

        now[0] += 50
        response = api.handle(
            _request("GET", "/api/threads", cookie=_cookie_header(session_id))
        )
        assert response.status == 200
        rolled = next(
            cookie for cookie in response.cookies if cookie.startswith("session=")
        )
        assert "Max-Age=100" in rolled

        current = sessions.lookup(session_id)
        assert current is not None
        assert current.expires_at == now[0] + 100
        assert current.expires_at > initial_expiry
    finally:
        api.close()
        store.close()


def test_session_idle_out_after_ttl(tmp_path: Path) -> None:
    now = [1_000_000.0]
    api, store, sessions, _failures = _make_api(
        tmp_path, clock=lambda: now[0], ttl=100.0
    )
    try:
        _response, session_id, _csrf = _sign_in(api)
        now[0] += 101
        response = api.handle(
            _request("GET", "/api/threads", cookie=_cookie_header(session_id))
        )
        assert response.status == 401
        assert _body(response)["error"]["code"] == "unauthorized"
        assert sessions.lookup(session_id) is None
    finally:
        api.close()
        store.close()


def test_revoke_current_session(tmp_path: Path) -> None:
    api, store, sessions, _failures = _make_api(tmp_path)
    try:
        _response, session_id, csrf = _sign_in(api)
        revoked = api.handle(
            _request(
                "DELETE",
                "/api/auth/session",
                cookie=_cookie_header(session_id, csrf),
                csrf=csrf,
                origin="http://localhost",
                host="localhost",
            )
        )
        assert revoked.status == 200, revoked.body
        assert _body(revoked)["revoked"] == 1
        assert any(
            cookie.startswith("session=") and "Max-Age=0" in cookie
            for cookie in revoked.cookies
        )
        assert sessions.lookup(session_id) is None

        after = api.handle(
            _request("GET", "/api/threads", cookie=_cookie_header(session_id))
        )
        assert after.status == 401
    finally:
        api.close()
        store.close()


def test_revoke_all_sessions(tmp_path: Path) -> None:
    api, store, _sessions, _failures = _make_api(tmp_path)
    try:
        _first, first_id, _first_csrf = _sign_in(api)
        _second, second_id, _second_csrf = _sign_in(api)
        revoked = api.handle(
            _request("DELETE", "/api/auth/sessions", bearer=ROOT_TOKEN)
        )
        assert revoked.status == 200, revoked.body
        assert _body(revoked)["revoked"] == 2

        for session_id in (first_id, second_id):
            response = api.handle(
                _request("GET", "/api/threads", cookie=_cookie_header(session_id))
            )
            assert response.status == 401
    finally:
        api.close()
        store.close()


def test_bearer_token_still_works(tmp_path: Path) -> None:
    api, store, _sessions, _failures = _make_api(tmp_path)
    try:
        response = api.handle(
            _request("GET", "/api/threads", bearer=ROOT_TOKEN)
        )
        assert response.status == 200
        assert _body(response)["ok"] is True
    finally:
        api.close()
        store.close()


def test_a_session_cannot_mint_another_session(tmp_path: Path) -> None:
    api, store, _sessions, _failures = _make_api(tmp_path)
    try:
        _response, session_id, csrf = _sign_in(api)
        refused = api.handle(
            _request(
                "POST",
                "/api/auth/session",
                cookie=_cookie_header(session_id, csrf),
                csrf=csrf,
                origin="http://localhost",
                host="localhost",
            )
        )
        assert refused.status == 401
        assert _body(refused)["error"]["code"] == "unauthorized"
    finally:
        api.close()
        store.close()


def test_cookie_mutation_requires_session_csrf_pair(tmp_path: Path) -> None:
    api, store, _sessions, _failures = _make_api(tmp_path)
    try:
        _response, session_id, csrf = _sign_in(api)
        path = "/api/threads"
        headers = {
            "cookie": _cookie_header(session_id, csrf),
            "origin": "http://localhost",
            "host": "localhost",
        }

        missing = api.handle(Request("POST", path, {}, headers, b"{}"))
        assert missing.status == 403
        assert _body(missing)["error"]["code"] == "csrf_failed"

        wrong = api.handle(
            Request(
                "POST",
                path,
                {},
                {**headers, "x-csrf-token": "not-the-session-token"},
                b"{}",
            )
        )
        assert wrong.status == 403
        assert _body(wrong)["error"]["code"] == "csrf_failed"

        allowed = api.handle(
            Request("POST", path, {}, {**headers, "x-csrf-token": csrf}, b"{}")
        )
        assert allowed.status == 201, allowed.body
    finally:
        api.close()
        store.close()


def test_cookie_mutation_without_origin_is_rejected(tmp_path: Path) -> None:
    api, store, _sessions, _failures = _make_api(tmp_path)
    try:
        _response, session_id, csrf = _sign_in(api)
        headers = {
            "cookie": _cookie_header(session_id, csrf),
            "x-csrf-token": csrf,
        }
        no_origin = api.handle(Request("POST", "/api/threads", {}, headers, b"{}"))
        assert no_origin.status == 403
        assert _body(no_origin)["error"]["code"] == "csrf_failed"

        cross_site = api.handle(
            Request(
                "POST",
                "/api/threads",
                {},
                {**headers, "origin": "https://evil.example", "host": "localhost"},
                b"{}",
            )
        )
        assert cross_site.status == 403
        assert _body(cross_site)["error"]["code"] == "csrf_failed"
    finally:
        api.close()
        store.close()


def test_auth_failed_logs_once_per_rejection_without_the_secret(
    tmp_path: Path,
) -> None:
    stream = io.StringIO()
    configure_logging(level="warning", secret_values=(), stream=stream, enabled=True)
    api, store, _sessions, _failures = _make_api(tmp_path)
    secret = "wrong-bearer-secret-42"
    try:
        first = api.handle(_request("GET", "/api/threads", bearer=secret))
        assert first.status == 401
        first_records = [
            json.loads(line)
            for line in stream.getvalue().splitlines()
            if line.strip()
        ]
        auth_records = [
            record for record in first_records if record["event"] == "auth_failed"
        ]
        assert len(auth_records) == 1
        assert auth_records[0]["outcome"] == "rejected"
        assert auth_records[0]["count"] == 1
        assert auth_records[0]["status"] == 401
        assert len(auth_records[0]["identity_hash"]) == 32
        assert secret not in stream.getvalue()

        second = api.handle(_request("GET", "/api/threads", bearer=secret))
        assert second.status == 401
        second_records = [
            json.loads(line)
            for line in stream.getvalue().splitlines()
            if line.strip()
        ]
        assert len([r for r in second_records if r["event"] == "auth_failed"]) == 2
        assert [r for r in second_records if r["event"] == "auth_failed"][-1][
            "count"
        ] == 2
        assert secret not in stream.getvalue()

        cookie_secret = "super-secret-stale-cookie"
        third = api.handle(
            _request("GET", "/api/threads", cookie=f"session={cookie_secret}")
        )
        assert third.status == 401
        assert cookie_secret not in stream.getvalue()
        third_records = [
            json.loads(line)
            for line in stream.getvalue().splitlines()
            if line.strip()
        ]
        assert len([r for r in third_records if r["event"] == "auth_failed"]) == 3
    finally:
        api.close()
        store.close()


def test_lockout_after_repeated_failures_and_expiry(tmp_path: Path) -> None:
    now = [1_000_000.0]
    api, store, _sessions, failures = _make_api(
        tmp_path,
        clock=lambda: now[0],
        attempts=3,
        lockout_seconds=60.0,
        idle_seconds=10.0,
    )
    secret = "wrong-secret-for-lockout"
    try:
        assert api.handle(_request("GET", "/api/threads", bearer=secret)).status == 401
        assert api.handle(_request("GET", "/api/threads", bearer=secret)).status == 401
        locked = api.handle(_request("GET", "/api/threads", bearer=secret))
        assert locked.status == 429
        assert _body(locked)["error"]["code"] == "rate_limited"
        assert "Retry-After" in locked.headers

        still_locked = api.handle(_request("GET", "/api/threads", bearer=secret))
        assert still_locked.status == 429

        # The lockout expires and the next attempt is a normal 401 again.
        now[0] += 61
        after = api.handle(_request("GET", "/api/threads", bearer=secret))
        assert after.status == 401

        # An idle failure record is dropped rather than retained forever.
        tracker = AuthFailureTracker(attempts=5, idle_seconds=10.0, clock=lambda: now[0])
        tracker.record("hashed-identity")
        now[0] += 11
        assert tracker.status("hashed-identity") == (0, None)
        assert len(failures) >= 1
    finally:
        api.close()
        store.close()


def test_sessions_share_one_owner_rate_limit_budget(tmp_path: Path) -> None:
    """M1: the budget is keyed on the owner, not on the credential.

    Two sessions minted from the same root token are the same owner, so the
    second tab cannot spend a second bucket.
    """

    limiter = RateLimiter(
        {
            "auth": (600.0, 100.0),
            "chat": (600.0, 100.0),
            "upload": (600.0, 100.0),
            "read": (60.0, 1.0),
            "admin": (600.0, 100.0),
        }
    )
    api, store, _sessions, _failures = _make_api(tmp_path, limiter=limiter)
    try:
        _first, first_id, _first_csrf = _sign_in(api)
        # Sign-in is itself a read-class request, so clear the budget after
        # minting both sessions: the assertions are about the sessions only.
        limiter.reset()
        _second, second_id, _second_csrf = _sign_in(api)
        limiter.reset()

        opened = api.handle(
            _request("GET", "/api/threads", cookie=_cookie_header(first_id))
        )
        assert opened.status == 200, opened.body

        shared = api.handle(
            _request("GET", "/api/threads", cookie=_cookie_header(second_id))
        )
        assert shared.status == 429
        assert _body(shared)["error"]["details"]["class"] == "read"

        # The root bearer is the same owner and the same bucket.
        bearer = api.handle(_request("GET", "/api/threads", bearer=ROOT_TOKEN))
        assert bearer.status == 429
    finally:
        api.close()
        store.close()


def test_stream_cap_is_shared_by_every_session(tmp_path: Path) -> None:
    """M1: the per-owner stream cap counts slots, not credentials."""

    api, store, _sessions, _failures = _make_api(
        tmp_path, max_streams_per_owner=1
    )
    try:
        created = api.handle(
            _request("POST", "/api/threads", bearer=ROOT_TOKEN, body=b"{}")
        )
        assert created.status == 201, created.body
        thread_id = _body(created)["thread"]["id"]
        _first, first_id, _first_csrf = _sign_in(api)
        _second, second_id, _second_csrf = _sign_in(api)

        path = f"/api/threads/{thread_id}/stream"
        opened = api.handle(_request("GET", path, cookie=_cookie_header(first_id)))
        assert opened.status == 200, opened.body
        assert opened.stream is not None
        try:
            refused = api.handle(
                _request("GET", path, cookie=_cookie_header(second_id))
            )
            assert refused.status == 429
            assert (
                _body(refused)["error"]["details"]["reason"] == "stream_capacity"
            )
        finally:
            opened.stream.close()
    finally:
        api.close()
        store.close()


def test_sign_in_failure_is_logged_counted_and_can_lock_out(
    tmp_path: Path,
) -> None:
    """M2: a wrong bearer on sign-in goes through ``_reject_auth``.

    A valid session cookie authorizes the route, so without this the wrong
    bearer would be a bare 401 with no ``auth_failed`` record, no count, and
    no lockout.
    """

    stream = io.StringIO()
    configure_logging(level="warning", secret_values=(), stream=stream, enabled=True)
    now = [1_000_000.0]
    api, store, _sessions, _failures = _make_api(
        tmp_path, clock=lambda: now[0], attempts=3, lockout_seconds=60.0
    )
    secret = "wrong-bearer-for-sign-in"
    try:
        _response, session_id, csrf = _sign_in(api)
        authorized = {
            "cookie": _cookie_header(session_id, csrf),
            "csrf": csrf,
            "origin": "http://localhost",
            "host": "localhost",
        }

        def attempt() -> Any:
            return api.handle(
                _request("POST", "/api/auth/session", bearer=secret, **authorized)
            )

        first = attempt()
        assert first.status == 401
        records = [
            json.loads(line)
            for line in stream.getvalue().splitlines()
            if line.strip()
        ]
        auth_records = [r for r in records if r["event"] == "auth_failed"]
        assert len(auth_records) == 1
        assert auth_records[0]["outcome"] == "rejected"
        assert auth_records[0]["count"] == 1
        assert auth_records[0]["status"] == 401
        assert len(auth_records[0]["identity_hash"]) == 32
        assert secret not in stream.getvalue()

        # The cookie that authorized the wrapper does not erase the presented
        # bearer's debt, so the failures accumulate into a lockout.
        assert attempt().status == 401
        locked = attempt()
        assert locked.status == 429
        assert _body(locked)["error"]["code"] == "rate_limited"
        assert locked.headers["Retry-After"].isdigit()
        assert attempt().status == 429

        # The lockout expires; the next wrong bearer is a plain 401 again.
        now[0] += 61
        assert attempt().status == 401

        # A correct bearer still mints a session through the same cookie.
        minted = api.handle(
            _request("POST", "/api/auth/session", bearer=ROOT_TOKEN, **authorized)
        )
        assert minted.status == 201, minted.body
    finally:
        api.close()
        store.close()


def test_failure_tracker_and_session_store_are_bounded(tmp_path: Path) -> None:
    now = [1_000_000.0]
    tracker = AuthFailureTracker(max_entries=2, clock=lambda: now[0])
    for index in range(10):
        tracker.record(f"identity-{index}")
        now[0] += 1
    assert len(tracker) <= 2

    sessions = SessionStore(ttl=100.0, max_entries=2, clock=lambda: now[0])
    raw_ids: list[str] = []
    records: list[Any] = []
    for _ in range(10):
        session_id, _csrf, record = sessions.create()
        raw_ids.append(session_id)
        records.append(record)
        now[0] += 0.01
    assert len(sessions) <= 2
    assert all(len(key) == 64 for key in sessions._sessions)
    for session_id in raw_ids:
        assert session_id not in sessions._sessions
    for record in records[-2:]:
        assert record.id_hash in sessions._sessions


def test_stdlib_adapter_emits_both_set_cookie_headers(tmp_path: Path) -> None:
    import http.client

    api, store, _sessions, _failures = _make_api(tmp_path)
    server = make_server(api, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = http.client.HTTPConnection(host, port, timeout=2)
        connection.request(
            "POST",
            "/api/auth/session",
            headers={"Authorization": f"Bearer {ROOT_TOKEN}"},
        )
        response = connection.getresponse()
        assert response.status == 201
        set_cookies = [
            value
            for name, value in response.getheaders()
            if name.lower() == "set-cookie"
        ]
        assert len(set_cookies) == 2
        session_cookie = next(
            value for value in set_cookies if value.startswith("session=")
        )
        csrf_cookie = next(value for value in set_cookies if value.startswith("csrf="))
        assert "HttpOnly" in session_cookie
        assert "HttpOnly" not in csrf_cookie
        response.read()

        connection.request(
            "GET",
            "/api/threads",
            headers={"Cookie": session_cookie.split(";", 1)[0]},
        )
        authenticated = connection.getresponse()
        assert authenticated.status == 200
        authenticated.read()
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        api.close()
        store.close()


def test_server_config_wires_session_bounds(tmp_path: Path) -> None:
    from chat.server import ServerConfig, build_app

    env = {
        "NEBIUS_API_KEY": "test-api-key",
        "NEBIUS_TOKEN_FACTORY_BASE_URL": "http://127.0.0.1:9/v1",
        "KELP_AUTH_TOKEN": ROOT_TOKEN,
        "KELP_SESSION_TTL_SECONDS": "120",
        "KELP_SESSION_MAX": "7",
        "KELP_AUTH_LOCKOUT_ATTEMPTS": "4",
        "KELP_SESSION_COOKIE_SECURE": "0",
        "KELP_DB_PATH": str(tmp_path / "db" / "chat.db"),
        "KELP_ARTIFACT_DIR": str(tmp_path / "artifacts"),
        "KELP_AUDIT_DIR": str(tmp_path / "audit"),
        "KELP_USAGE_DIR": str(tmp_path / "usage"),
        "KELP_WORKSPACE_ROOT": str(tmp_path / "workspace"),
        "KELP_HOST": "127.0.0.1",
        "KELP_PORT": "0",
    }
    app = build_app(
        ServerConfig.from_env(env), provider=_NoopProvider(), environ=env
    )
    try:
        assert app.api.sessions.ttl == 120.0
        assert app.api.sessions.max_entries == 7
        assert app.api.auth_failures.attempts == 4
        assert app.api.cookie_secure is False
    finally:
        app.close()

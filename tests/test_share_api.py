"""KEL-28 M4-F: read-only thread share links (ADR 0002).

These tests drive the public contract through ``ChatAPI.handle``:

* create, use, list and revoke a link;
* expiry, unknown, malformed, revoked and purged inputs are one identical 404;
* every non-GET under ``/api/shared/*`` is ``405`` before a lookup and mutates
  nothing, and no artifact bytes are reachable through the share surface;
* the snapshot drops owner-only material (artifact handles, storage paths,
  hashes, approval argv);
* the stream replays the frozen snapshot and closes on revoke/purge;
* the limiter is keyed on ``sha256(token)`` plus a global ceiling, charged
  before the lookup.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from chat import ChatAPI, ChatService, ChatStore, Request
from chat.api import RateLimiter, _route_label
from openshell.audit import AuditLog

from .chatkit import MODEL, StubProvider, text_script

AUTH = {"authorization": "Bearer tok"}
CSRF = {"x-csrf-token": "csrf"}

_GENEROUS_LIMITS = {
    name: (1_000_000.0, 1_000_000.0)
    for name in (
        "auth",
        "chat",
        "upload",
        "read",
        "admin",
        "share",
        "share_global",
    )
}

#: Thread-owned tables. ``share_views`` is deliberately separate: a share GET
#: is allowed to increment its own daily aggregate and nothing else.
_THREAD_TABLES = (
    "threads",
    "messages",
    "blocks",
    "events",
    "turns",
    "approvals",
    "artifacts",
    "shares",
)


class Clock:
    """A mutable UTC wall clock shared by the store and the snapshot."""

    def __init__(self) -> None:
        self.current = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


def make_api(
    tmp_path: Path,
    *,
    clock: Clock | None = None,
    limiter: RateLimiter | None = None,
    audit: AuditLog | None = None,
    **kwargs: Any,
) -> tuple[ChatAPI, ChatService, ChatStore, Clock]:
    clock = clock or Clock()
    store = ChatStore(tmp_path / "chat.db", now=clock)
    service = ChatService(
        store=store,
        provider=StubProvider([text_script("Hello")]),
        model=MODEL,
        audit=audit,
    )
    api = ChatAPI(
        service,
        auth_token="tok",
        csrf_token="csrf",
        limiter=limiter
        or RateLimiter(_GENEROUS_LIMITS, enabled=True),
        **kwargs,
    )
    api.stream_idle = 0.05
    api.share_tick = 0.01
    return api, service, store, clock


def call(
    api: ChatAPI,
    method: str,
    path: str,
    body: Mapping[str, Any] | None = None,
    *,
    headers: Mapping[str, str] | None = None,
    auth: bool = True,
) -> Any:
    request_headers: dict[str, str] = {}
    if auth:
        request_headers.update(AUTH)
    if method.upper() in {"POST", "PATCH", "DELETE"}:
        request_headers.update(CSRF)
    if body is not None:
        request_headers["content-type"] = "application/json"
    request_headers.update(headers or {})
    raw = json.dumps(body).encode() if body is not None else b""
    return api.handle(Request(method, path, {}, request_headers, raw))


def payload(response: Any) -> dict[str, Any]:
    return json.loads(response.body)


def make_thread(api: ChatAPI, title: str = "Shared thread") -> str:
    response = call(api, "POST", "/api/threads", {"title": title})
    assert response.status == 201, response.body
    return payload(response)["thread"]["id"]


def create_share(api: ChatAPI, thread_id: str, **body: Any) -> dict[str, Any]:
    response = call(api, "POST", f"/api/threads/{thread_id}/share", body or {})
    assert response.status == 201, response.body
    return payload(response)["share"]


def seed_messages(store: ChatStore, thread_id: str) -> None:
    """One user turn, then an assistant turn with every block shape."""

    store.create_message(
        thread_id=thread_id,
        turn_id="turn_seed_1",
        role="user",
        status="complete",
        blocks=[{"type": "text", "text": "What is in the report?"}],
        message_id="msg_seed_user",
    )
    store.create_message(
        thread_id=thread_id,
        turn_id="turn_seed_1",
        role="assistant",
        status="complete",
        blocks=[
            {"type": "text", "text": "The report says hello."},
            {
                "type": "attachment",
                "attachment": {
                    "handle": "artifact:secretHandle99",
                    "name": "report.pdf",
                    "kind": "pdf",
                    "mime": "application/pdf",
                    "size_bytes": 1234,
                    "status": "attached",
                    "open_url": "/api/artifacts/artifact:secretHandle99/content",
                },
            },
            {
                "type": "tool_call",
                "call_id": "call_secret",
                "tool": "docs.answer",
                "label": "Document answer",
                "summary": "report.pdf",
                "status": "done",
                "duration_ms": 12,
                "effects": ["read"],
                "approval_id": "appr_secret",
                "detail": {
                    "arguments": {"question": "argv-leak"},
                    "result": {"output": {"handle": "artifact:secretHandle99"}},
                    "error": None,
                },
            },
        ],
        message_id="msg_seed_assistant",
    )
    store.update_message(
        "msg_seed_assistant",
        sources=[
            {
                "id": "src_0",
                "kind": "doc",
                "title": "report.pdf",
                "tool": "docs.answer",
                "handle": "artifact:secretHandle99",
                "locator": "page 2",
                "storage_path": "/home/owner/secret/report.pdf",
                "sha256": "deadbeef",
            }
        ],
        sources_total=1,
    )


def seed_pending_approval(store: ChatStore, thread_id: str) -> None:
    store.save_approval(
        {
            "id": "appr_secret",
            "thread_id": thread_id,
            "turn_id": "turn_seed_1",
            "call_id": "call_secret",
            "source": "skill",
            "status": "pending",
            "requested_at": store.now_iso(),
            "expires_at": store.now_iso(),
            "tool": "shell.exec",
            "arguments": {"command": ["rm", "-rf", "/tmp/argv-leak"]},
        }
    )


def dump_tables(store: ChatStore, tables: tuple[str, ...] = _THREAD_TABLES) -> dict[str, Any]:
    return {
        table: [
            dict(row)
            for row in store._conn.execute(  # noqa: SLF001 - the test owns the file
                f"SELECT * FROM {table} ORDER BY 1"
            ).fetchall()
        ]
        for table in tables
    }


def header_set(response: Any) -> set[str]:
    return {name.lower() for name in response.headers}


# -- create, use, revoke ---------------------------------------------------


def test_create_use_list_and_revoke(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    api, service, store, clock = make_api(tmp_path, audit=audit)
    thread_id = make_thread(api)
    seed_messages(store, thread_id)

    share = create_share(api, thread_id)
    token = share["token"]
    assert len(token) >= 32
    assert share["url"] == f"/api/shared/{token}"
    assert share["stream_url"] == f"/api/shared/{token}/stream"
    assert share["message_count"] == 2
    # 7 days by default.
    created = datetime.fromisoformat(share["created_at"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(share["expires_at"].replace("Z", "+00:00"))
    assert expires - created == timedelta(days=7)

    response = call(api, "GET", f"/api/shared/{token}", auth=False)
    assert response.status == 200
    snapshot = payload(response)["share"]
    assert snapshot["title"] == "Shared thread"
    assert snapshot["read_only"] is True
    texts = [
        block.get("text")
        for message in snapshot["messages"]
        for block in message["blocks"]
        if block.get("type") == "text"
    ]
    assert any(str(text).startswith("What is in") for text in texts)
    assert "Set-Cookie" not in response.headers
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]

    listing = call(api, "GET", f"/api/threads/{thread_id}/shares")
    assert listing.status == 200
    rows = payload(listing)["shares"]
    assert len(rows) == 1
    assert rows[0]["status"] == "active"
    assert rows[0]["id"] == hash_token(token)[:16]
    assert token not in listing.body.decode()
    assert rows[0]["views"] == 1

    revoked = call(
        api, "DELETE", f"/api/threads/{thread_id}/shares/{rows[0]['id']}"
    )
    assert revoked.status == 200
    assert payload(revoked)["revoked"] == 1

    after = call(api, "GET", f"/api/shared/{token}", auth=False)
    assert after.status == 404

    entries = audit.entries()
    created_entry = next(item for item in entries if item["event"] == "share_created")
    revoked_entry = next(item for item in entries if item["event"] == "share_revoked")
    assert created_entry["actor"] == "owner"
    assert revoked_entry["actor"] == "owner"
    assert created_entry["extra"]["token_hash"] == hash_token(token)
    assert revoked_entry["extra"]["token_hash"] == hash_token(token)
    assert token not in (tmp_path / "audit.jsonl").read_text()


def test_listing_revokes_all_links(tmp_path: Path) -> None:
    api, _service, store, _clock = make_api(tmp_path)
    thread_id = make_thread(api)
    first = create_share(api, thread_id)["token"]
    second = create_share(api, thread_id)["token"]

    response = call(api, "DELETE", f"/api/threads/{thread_id}/shares")
    assert response.status == 200
    assert payload(response)["revoked"] == 2
    for token in (first, second):
        assert call(api, "GET", f"/api/shared/{token}", auth=False).status == 404


def test_ttl_is_clamped_and_validated(tmp_path: Path) -> None:
    api, _service, store, _clock = make_api(tmp_path)
    thread_id = make_thread(api)

    share = create_share(api, thread_id, ttl_seconds=10**9)
    created = datetime.fromisoformat(share["created_at"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(share["expires_at"].replace("Z", "+00:00"))
    assert expires - created == timedelta(days=30)

    bad = call(api, "POST", f"/api/threads/{thread_id}/share", {"ttl_seconds": "soon"})
    assert bad.status == 400
    assert payload(bad)["error"]["code"] == "invalid_request"


# -- expiry and indistinguishable 404 -------------------------------------

def test_expiry_is_404_with_the_same_bytes(tmp_path: Path) -> None:
    api, _service, _store, clock = make_api(tmp_path)
    thread_id = make_thread(api)
    share = create_share(api, thread_id)

    clock.advance(8 * 24 * 60 * 60)
    response = call(api, "GET", f"/api/shared/{share['token']}", auth=False)
    assert response.status == 404
    assert payload(response)["error"]["code"] == "not_found"


def test_unknown_expired_revoked_malformed_and_purged_are_identical(
    tmp_path: Path,
) -> None:
    api, service, store, clock = make_api(tmp_path)
    thread_id = make_thread(api)
    seed_messages(store, thread_id)

    unknown = call(api, "GET", "/api/shared/" + "A" * 43, auth=False)
    malformed = call(api, "GET", "/api/shared/..", auth=False)
    wellformed_miss = call(api, "GET", "/api/shared/not-a-token", auth=False)

    revoked_token = create_share(api, thread_id)["token"]
    revoked_id = hash_token(revoked_token)[:16]
    call(api, "DELETE", f"/api/threads/{thread_id}/shares/{revoked_id}")
    revoked = call(api, "GET", f"/api/shared/{revoked_token}", auth=False)

    # Purge removes the link's row entirely.
    purged_token = create_share(api, thread_id)["token"]
    service.store.purge_thread(thread_id)
    purged = call(api, "GET", f"/api/shared/{purged_token}", auth=False)

    # Expiry is enforced on the read, not only at creation.
    other_thread = make_thread(api, title="Expiring")
    expired_token = create_share(api, other_thread)["token"]
    clock.advance(8 * 24 * 60 * 60)
    expired = call(api, "GET", f"/api/shared/{expired_token}", auth=False)

    bodies = {
        unknown.body,
        malformed.body,
        wellformed_miss.body,
        expired.body,
        revoked.body,
        purged.body,
    }
    assert len(bodies) == 1
    statuses = {
        unknown.status,
        malformed.status,
        wellformed_miss.status,
        expired.status,
        revoked.status,
        purged.status,
    }
    assert statuses == {404}
    base_headers = header_set(unknown)
    for response in (malformed, wellformed_miss, expired, revoked, purged):
        assert header_set(response) == base_headers
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Referrer-Policy"] == "no-referrer"


# -- no mutation and no bytes ---------------------------------------------


def test_every_non_get_is_405_and_mutates_nothing(tmp_path: Path) -> None:
    api, _service, store, _clock = make_api(tmp_path)
    thread_id = make_thread(api)
    seed_messages(store, thread_id)
    token = create_share(api, thread_id)["token"]

    before = dump_tables(store)
    views_before = dump_tables(store, ("share_views",))
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        for path in (
            f"/api/shared/{token}",
            f"/api/shared/{token}/stream",
        ):
            response = call(api, method, path, body={} if method != "DELETE" else None)
            assert response.status == 405, (method, path, response.body)
            assert response.headers["Allow"] == "GET"
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["Referrer-Policy"] == "no-referrer"
            assert b"report" not in response.body
    assert dump_tables(store) == before
    assert dump_tables(store, ("share_views",)) == views_before


def test_no_artifact_bytes_or_handle_on_any_share_path(tmp_path: Path) -> None:
    api, _service, store, _clock = make_api(tmp_path)
    thread_id = make_thread(api)
    seed_messages(store, thread_id)
    store.create_artifact(
        {
            "handle": "artifact:secretHandle99",
            "thread_id": thread_id,
            "name": "report.pdf",
            "kind": "pdf",
            "mime": "application/pdf",
            "size_bytes": 1234,
            "sha256": "deadbeef",
            "storage_path": str(tmp_path / "secret.pdf"),
        }
    )
    token = create_share(api, thread_id)["token"]

    before = dump_tables(store)
    snapshot = call(api, "GET", f"/api/shared/{token}", auth=False)
    assert snapshot.status == 200
    text = snapshot.body.decode()
    for forbidden in (
        "artifact:secretHandle99",
        "storage_path",
        "sha256",
        "deadbeef",
        "/home/owner/secret",
        "argv-leak",
        "appr_secret",
        "call_secret",
    ):
        assert forbidden not in text
    assert dump_tables(store) == before  # only share_views may change

    for action in ("content", "preview", "download", "bytes"):
        response = call(api, "GET", f"/api/shared/{token}/{action}", auth=False)
        assert response.status == 404
        assert b"PDF" not in response.body
        assert response.headers["Content-Type"].startswith("application/json")
    deeper = call(api, "GET", f"/api/shared/{token}/stream/extra", auth=False)
    assert deeper.status == 404


def test_snapshot_counts_pending_approvals_without_their_payload(
    tmp_path: Path,
) -> None:
    api, _service, store, _clock = make_api(tmp_path)
    thread_id = make_thread(api)
    seed_messages(store, thread_id)
    seed_pending_approval(store, thread_id)

    token = create_share(api, thread_id)["token"]
    snapshot = payload(call(api, "GET", f"/api/shared/{token}", auth=False))["share"]
    assert snapshot["omitted"]["approvals"] == 1
    assert "argv-leak" not in json.dumps(snapshot)


def test_oversized_snapshot_is_refused_not_truncated(tmp_path: Path) -> None:
    api, _service, store, _clock = make_api(tmp_path, share_max_snapshot_bytes=1024)
    thread_id = make_thread(api)
    store.create_message(
        thread_id=thread_id,
        turn_id="turn_big",
        role="user",
        status="complete",
        blocks=[{"type": "text", "text": "x" * 4096}],
    )

    response = call(api, "POST", f"/api/threads/{thread_id}/share", {})
    assert response.status == 413
    assert payload(response)["error"]["code"] == "share_too_large"
    assert payload(call(api, "GET", f"/api/threads/{thread_id}/shares"))["shares"] == []


def test_share_response_never_sets_a_cookie_even_for_an_owner_session(
    tmp_path: Path,
) -> None:
    api, service, store, _clock = make_api(tmp_path)
    thread_id = make_thread(api)
    token = create_share(api, thread_id)["token"]
    session_id, csrf, _record = api.sessions.create(label="laptop")

    response = call(
        api,
        "GET",
        f"/api/shared/{token}",
        auth=False,
        headers={"cookie": f"session={session_id}; csrf={csrf}"},
    )
    assert response.status == 200
    assert response.cookies == []
    assert "Set-Cookie" not in response.headers


def test_metric_route_label_never_carries_the_token(tmp_path: Path) -> None:
    api, _service, _store, _clock = make_api(tmp_path)
    thread_id = make_thread(api)
    token = create_share(api, thread_id)["token"]

    assert _route_label(f"/api/shared/{token}") == "/api/shared/{id}"
    assert _route_label(f"/api/shared/{token}/stream") == "/api/shared/{id}/stream"
    assert token not in _route_label(f"/api/shared/{token}")


def test_create_refuses_a_thread_with_a_live_turn(tmp_path: Path) -> None:
    api, _service, store, _clock = make_api(tmp_path)
    thread_id = make_thread(api)
    store.create_turn(
        thread_id=thread_id,
        user_message_id="msg_u",
        assistant_message_id="msg_a",
        client_turn_id="ct-live",
        budget={},
    )
    response = call(api, "POST", f"/api/threads/{thread_id}/share", {})
    assert response.status == 409
    assert payload(response)["error"]["code"] == "turn_in_progress"


# -- stream ----------------------------------------------------------------


def test_stream_replays_the_frozen_snapshot(tmp_path: Path) -> None:
    api, _service, store, _clock = make_api(tmp_path)
    thread_id = make_thread(api)
    seed_messages(store, thread_id)
    token = create_share(api, thread_id)["token"]

    # Content written after creation must never appear in the share.
    store.create_message(
        thread_id=thread_id,
        turn_id="turn_later",
        role="user",
        status="complete",
        blocks=[{"type": "text", "text": "written after the share"}],
    )

    response = call(api, "GET", f"/api/shared/{token}/stream", auth=False)
    assert response.status == 200
    assert response.headers["Content-Type"].startswith("text/event-stream")
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    frames = []
    for frame in response.stream:
        frames.append(frame)
        if len(frames) >= 3:
            break
    response.stream.close()

    assert "event: snapshot" in frames[0]
    assert "event: message" in frames[1]
    joined = "".join(frames)
    assert "What is in the report?" in joined
    assert "written after the share" not in joined
    assert "artifact:secretHandle99" not in joined


def test_revocation_and_purge_close_an_open_stream(tmp_path: Path) -> None:
    api, _service, store, _clock = make_api(tmp_path)
    api.stream_idle = 5.0
    thread_id = make_thread(api)
    seed_messages(store, thread_id)
    token = create_share(api, thread_id)["token"]
    share_id = payload(call(api, "GET", f"/api/threads/{thread_id}/shares"))[
        "shares"
    ][0]["id"]

    stream = call(api, "GET", f"/api/shared/{token}/stream", auth=False).stream
    next(stream)  # snapshot frame
    call(api, "DELETE", f"/api/threads/{thread_id}/shares/{share_id}")
    _exhaust(stream)

    second_token = create_share(api, thread_id)["token"]
    second = call(api, "GET", f"/api/shared/{second_token}/stream", auth=False).stream
    next(second)
    store.purge_thread(thread_id)
    _exhaust(second)


def _exhaust(stream: Any, limit: int = 500) -> None:
    for _ in range(limit):
        try:
            next(stream)
        except StopIteration:
            return
    raise AssertionError("share stream did not close")


# -- rate limiting ---------------------------------------------------------


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def test_rate_limit_is_keyed_on_the_token_hash(tmp_path: Path) -> None:
    limits = dict(_GENEROUS_LIMITS)
    limits["share"] = (60.0, 1.0)
    api, _service, _store, _clock = make_api(
        tmp_path, limiter=RateLimiter(limits, enabled=True)
    )
    thread_id = make_thread(api)
    first = create_share(api, thread_id)["token"]
    second = create_share(api, thread_id)["token"]

    assert call(api, "GET", f"/api/shared/{first}", auth=False).status == 200
    limited = call(api, "GET", f"/api/shared/{first}", auth=False)
    assert limited.status == 429
    assert payload(limited)["error"]["details"]["class"] == "share"
    assert limited.headers["Retry-After"].isdigit()
    assert limited.headers["Referrer-Policy"] == "no-referrer"
    # A different token has its own bucket.
    assert call(api, "GET", f"/api/shared/{second}", auth=False).status == 200

    keys = set(api.limiter._buckets["share"])
    assert keys == {
        f"sha256:{hash_token(first)[:24]}",
        f"sha256:{hash_token(second)[:24]}",
    }
    assert first not in keys and second not in keys


def test_rate_limit_is_charged_before_the_lookup(tmp_path: Path) -> None:
    limits = dict(_GENEROUS_LIMITS)
    limits["share"] = (60.0, 1.0)
    api, _service, _store, _clock = make_api(
        tmp_path, limiter=RateLimiter(limits, enabled=True)
    )
    unknown = "u" * 43
    assert call(api, "GET", f"/api/shared/{unknown}", auth=False).status == 404
    assert call(api, "GET", f"/api/shared/{unknown}", auth=False).status == 429


def test_global_ceiling_bounds_distinct_tokens(tmp_path: Path) -> None:
    limits = dict(_GENEROUS_LIMITS)
    limits["share_global"] = (60.0, 1.0)
    api, _service, _store, _clock = make_api(
        tmp_path, limiter=RateLimiter(limits, enabled=True)
    )
    thread_id = make_thread(api)
    first = create_share(api, thread_id)["token"]
    second = create_share(api, thread_id)["token"]

    assert call(api, "GET", f"/api/shared/{first}", auth=False).status == 200
    limited = call(api, "GET", f"/api/shared/{second}", auth=False)
    assert limited.status == 429
    assert payload(limited)["error"]["details"]["class"] == "share_global"
    assert payload(limited)["error"]["details"]["reason"] == "global"


# -- audit -----------------------------------------------------------------


def test_views_are_aggregated_per_day_with_one_audit_event(
    tmp_path: Path,
) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    api, _service, store, _clock = make_api(tmp_path, audit=audit)
    thread_id = make_thread(api)
    token = create_share(api, thread_id)["token"]

    for _ in range(3):
        assert call(api, "GET", f"/api/shared/{token}", auth=False).status == 200

    token_hash = hash_token(token)
    row = store._conn.execute(  # noqa: SLF001 - the assertion is the table
        "SELECT day, views FROM share_views WHERE token_hash = ?",
        (token_hash,),
    ).fetchone()
    assert row is not None
    assert row["views"] == 3

    viewed = [item for item in audit.entries() if item["event"] == "share_viewed"]
    assert len(viewed) == 1
    assert viewed[0]["actor"] == "share"
    assert viewed[0]["extra"]["token_hash"] == token_hash
    assert token not in (tmp_path / "audit.jsonl").read_text()


def test_thread_delete_removes_shares_and_view_counts(tmp_path: Path) -> None:
    api, service, store, _clock = make_api(tmp_path)
    thread_id = make_thread(api)
    token = create_share(api, thread_id)["token"]
    call(api, "GET", f"/api/shared/{token}", auth=False)

    response = api.runner.run(service.delete_thread(thread_id))
    assert response["ok"] is True
    assert response["shares"] == 1
    assert response["share_views"] == 1
    assert store._conn.execute("SELECT COUNT(*) AS n FROM shares").fetchone()["n"] == 0
    assert (
        store._conn.execute("SELECT COUNT(*) AS n FROM share_views").fetchone()["n"]
        == 0
    )
    assert call(api, "GET", f"/api/shared/{token}", auth=False).status == 404

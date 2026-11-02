"""Threads, their shares, and the ``/api/threads`` routes."""

from __future__ import annotations

import json
import secrets
from typing import TYPE_CHECKING, Any, Callable, Iterator, Mapping

from ..errors import ChatError
from ..store import ThreadGoneError
from . import stream as stream_routes
from . import turns as turn_routes
from ._util import (
    _DECISION_LIMIT,
    _int_param,
    _json_response,
    _segment,
)
from .body import _json_body
from .shared import (
    _SHARE_ID_LENGTH,
    _scrub_projection,
    _share_hash,
    _share_message,
)

if TYPE_CHECKING:
    from ..api import ChatAPI, Request, Response


def _threads(
    api: "ChatAPI",
    request: "Request",
    method: str,
    rest: list[str],
    *,
    frames_fn: Callable[..., Iterator[str]],
) -> "Response":
    if not rest:
        if method == "GET":
            limit = _int_param(request, "limit", 50, 1, 100)
            threads, cursor = api.service.list_threads(
                limit=limit, cursor=request.query_one("cursor")
            )
            return _json_response(
                200, {"ok": True, "threads": threads, "next_cursor": cursor}
            )
        if method == "POST":
            body = _json_body(api, request, optional=True)
            title = body.get("title")
            if title is not None and not isinstance(title, str):
                raise ChatError(
                    "invalid_request",
                    status=400,
                    message="Title must be a string.",
                    user_fixable=True,
                    field="/title",
                )
            thread = api.service.create_thread(title)
            return _json_response(201, {"ok": True, "thread": thread})
        raise ChatError("not_found", status=404, message="Unknown endpoint.")

    thread_id = _segment(rest[0])
    if len(rest) == 1:
        if method == "GET":
            limit = _int_param(request, "limit", 50, 1, 100)
            snapshot = api.service.get_thread(
                thread_id,
                limit=limit,
                before=request.query_one("before"),
            )
            return _json_response(200, {"ok": True, **snapshot})
        if method == "PATCH":
            body = _json_body(api, request)
            title = body.get("title")
            if not isinstance(title, str):
                raise ChatError(
                    "invalid_request",
                    status=400,
                    message="Title must be a string.",
                    user_fixable=True,
                    field="/title",
                )
            thread = api.service.rename_thread(thread_id, title)
            return _json_response(200, {"ok": True, "thread": thread})
        if method == "DELETE":
            result = api.runner.run(api.service.delete_thread(thread_id))
            return _json_response(200, result)
        raise ChatError("not_found", status=404, message="Unknown endpoint.")

    if rest[1] == "share" and len(rest) == 2 and method == "POST":
        return _create_share(api, request, thread_id)
    if rest[1] == "shares":
        return _thread_shares(api, request, method, thread_id, rest[2:])
    if rest[1] == "stream" and len(rest) == 2 and method == "GET":
        return stream_routes._stream(api, request, thread_id, frames_fn=frames_fn)
    if rest[1] == "turns" and len(rest) == 2 and method == "POST":
        return turn_routes._send(api, request, thread_id)
    raise ChatError("not_found", status=404, message="Unknown endpoint.")


# -- share links (KEL-28 M4-F) ---------------------------------------------


def _create_share(api: "ChatAPI", request: "Request", thread_id: str) -> "Response":
    """Materialise one read-only snapshot and return its token exactly once.

    This is an ordinary owner mutation: ``_authorize`` already enforced the
    bearer/session credential and the CSRF pair, and the ``chat`` class charged
    the request before dispatch. A thread with a live turn is refused because
    the snapshot would otherwise freeze a half-written turn (the same rule the
    purge uses).
    """

    body = _json_body(api, request, optional=True, limit=_DECISION_LIMIT)
    raw_ttl = body.get("ttl_seconds")
    ttl = api.share_ttl
    if raw_ttl is not None:
        if isinstance(raw_ttl, bool) or not isinstance(raw_ttl, (int, float)):
            raise ChatError(
                "invalid_request",
                status=400,
                message="ttl_seconds must be a number.",
                user_fixable=True,
                field="/ttl_seconds",
            )
        ttl = min(max(60.0, float(raw_ttl)), api.share_max_ttl)
    thread = api.service.store.get_thread(thread_id)
    if thread is None:
        raise ChatError("unknown_thread", status=404)
    if thread.get("active_turn_id"):
        raise ChatError(
            "turn_in_progress",
            status=409,
            message="Wait for the turn to finish before sharing.",
            details={"active_turn_id": thread["active_turn_id"]},
        )
    snapshot = _build_share_snapshot(api, thread_id, thread)
    token = secrets.token_urlsafe(32)
    token_hash = _share_hash(token)
    # Sweep on an owner write, never on the read path: an expired or orphaned
    # row cannot grow the table without bound.
    try:
        api.service.store.share_sweep()
    except Exception:  # noqa: BLE001 - maintenance never blocks a create
        pass
    try:
        record = api.service.store.share_create(
            token_hash=token_hash,
            thread_id=thread_id,
            snapshot=snapshot,
            ttl_seconds=ttl,
        )
    except ThreadGoneError:
        # The thread was purged between the read above and this insert (a
        # concurrent delete, possibly another process). Nothing was written;
        # answer the same 404 as an unknown thread.
        raise ChatError("unknown_thread", status=404) from None
    _audit_share(
        api,
        "share_created",
        actor="owner",
        token_hash=token_hash,
        extra={"thread_id": thread_id},
    )
    return _json_response(
        201,
        {
            "ok": True,
            "share": {
                "id": token_hash[:_SHARE_ID_LENGTH],
                "token": token,
                "url": f"/api/shared/{token}",
                "stream_url": f"/api/shared/{token}/stream",
                "thread_id": thread_id,
                "created_at": record["created_at"],
                "expires_at": record["expires_at"],
                "message_count": snapshot["message_count"],
            },
        },
    )


def _thread_shares(
    api: "ChatAPI",
    request: "Request",
    method: str,
    thread_id: str,
    rest: list[str],
) -> "Response":
    """List or revoke this thread's links. The raw token is never returned."""

    if api.service.store.get_thread(thread_id) is None:
        raise ChatError("unknown_thread", status=404)
    if not rest:
        if method == "GET":
            now = api.service.store.now_iso()
            shares = [
                _share_listing(api, record, now=now)
                for record in api.service.store.share_list(thread_id)
            ]
            return _json_response(200, {"ok": True, "shares": shares})
        if method == "DELETE":
            hashes = api.service.store.share_revoke_thread(thread_id)
            for token_hash in hashes:
                _audit_share(
                    api,
                    "share_revoked",
                    actor="owner",
                    token_hash=token_hash,
                    extra={"thread_id": thread_id, "all": True},
                )
            return _json_response(200, {"ok": True, "revoked": len(hashes)})
        raise ChatError("not_found", status=404, message="Unknown endpoint.")
    if len(rest) == 1 and method == "DELETE":
        share_id = _segment(rest[0])
        record = api.service.store.share_revoke_prefix(thread_id, share_id)
        if record is None:
            raise ChatError("unknown_share", status=404, message="Unknown share.")
        _audit_share(
            api,
            "share_revoked",
            actor="owner",
            token_hash=record["token_hash"],
            extra={"thread_id": thread_id},
        )
        return _json_response(
            200,
            {
                "ok": True,
                "revoked": 1,
                "share": {"id": record["token_hash"][:_SHARE_ID_LENGTH]},
            },
        )
    raise ChatError("not_found", status=404, message="Unknown endpoint.")


def _share_listing(
    api: "ChatAPI", record: Mapping[str, Any], *, now: str
) -> dict[str, Any]:
    if record["revoked_at"]:
        status = "revoked"
    elif str(record["expires_at"]) <= now:
        status = "expired"
    else:
        status = "active"
    views = 0
    try:
        views = api.service.store.share_views_total(record["token_hash"])
    except Exception:  # noqa: BLE001 - a listing never fails on a counter
        views = 0
    return {
        "id": str(record["token_hash"])[:_SHARE_ID_LENGTH],
        "status": status,
        "created_at": record["created_at"],
        "expires_at": record["expires_at"],
        "revoked_at": record["revoked_at"],
        "snapshot_bytes": record["snapshot_bytes"],
        "views": views,
    }


def _build_share_snapshot(
    api: "ChatAPI", thread_id: str, thread: Mapping[str, Any]
) -> dict[str, Any]:
    """Project the settled thread once, dropping every owner-only field."""

    messages = _all_messages(api, thread_id)
    pending = api.service.store.list_approvals(
        thread_id=thread_id, status="pending", limit=200
    )
    snapshot = {
        "version": 1,
        "title": str(thread.get("title") or "Shared chat"),
        "message_count": len(messages),
        "messages": [_share_message(message) for message in messages],
        "omitted": {"approvals": len(pending)},
        "viewer": "anonymous-share",
        "read_only": True,
    }
    # The allow-list drops handles structurally, but a handle can also be
    # typed into text, a tool summary, or a citation. Scrub free text with the
    # same pattern the orphan sweep uses before the size check, so the cap is
    # measured on what is actually stored.
    snapshot = _scrub_projection(snapshot)
    # The store stamps two timestamps into the frozen copy, so leave a small
    # margin rather than accepting a snapshot that grows past the cap on
    # insert.
    size = len(
        json.dumps(snapshot, default=str, separators=(",", ":")).encode("utf-8")
    )
    if size + 512 > api.share_max_snapshot_bytes:
        raise ChatError(
            "share_too_large",
            status=413,
            message="This thread is too large to share.",
            details={"limit_bytes": api.share_max_snapshot_bytes},
        )
    return snapshot


def _all_messages(api: "ChatAPI", thread_id: str) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    before: str | None = None
    while True:
        batch, has_more = api.service.store.list_messages(
            thread_id, limit=100, before=before
        )
        collected = list(batch) + collected
        if not has_more or not batch:
            break
        before = str(batch[0]["id"])
    return collected


def _audit_share(
    api: "ChatAPI",
    event: str,
    *,
    actor: str,
    token_hash: str,
    extra: Mapping[str, Any] | None = None,
) -> None:
    """One audit record, carrying the hash and never the token."""

    record = getattr(api.service, "_record_audit", None)
    if record is None:
        return
    fields = {"token_hash": token_hash, **dict(extra or {})}
    try:
        record(event, actor=actor, extra=fields)
    except Exception:  # noqa: BLE001 - accounting is not traffic
        return


__all__ = [
    "_all_messages",
    "_audit_share",
    "_build_share_snapshot",
    "_create_share",
    "_share_listing",
    "_thread_shares",
    "_threads",
]

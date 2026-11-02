"""Read-only share links: the unauth GET surface and its projection helpers."""

from __future__ import annotations

import hashlib
import time
from typing import TYPE_CHECKING, Any, Iterator, Mapping
from urllib.parse import unquote, urlsplit

from ..errors import ChatError
from ..logging import log
from ..store import SHARE_ID_LENGTH, ChatStore, scrub_artifact_handles
from ..stream import format_comment, format_event
from ._util import (
    _RATE_LIMITED_FALLBACK_RETRY,
    _busy_response,
    _json_response,
    _rate_limited_response,
    guarded_stream,
)

if TYPE_CHECKING:
    from ..api import ChatAPI, Request, Response

#: Public id of a link in the owner's listing is the hash prefix; the full
#: hash is never needed to revoke one. The width is the store's constant, so
#: the validator and the listing cannot drift.
_SHARE_ID_LENGTH = SHARE_ID_LENGTH
#: How often an open share stream re-checks its link. Revocation and purge
#: close it within one tick (the KEL-24 rule, applied to a share stream).
_SHARE_STREAM_TICK = 0.5
_SHARE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
}
#: A snapshot block keeps only these display fields. Anything else - an
#: artifact handle, a storage path, a content hash, an approval argv - is
#: dropped structurally rather than filtered by a query someone must remember.
_SHARE_SOURCE_KEYS = (
    "kind",
    "title",
    "url",
    "snippet",
    "locator",
    "published_at",
    "status",
    "tool",
)


def _share_hash(token: str) -> str:
    """The only key a share is stored or looked up by."""

    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()


def _is_share_path(path: str) -> bool:
    """True for ``/api/shared/...``, from a raw path string or a Request path."""

    try:
        segments = [
            segment for segment in unquote(urlsplit(path).path).split("/") if segment
        ]
    except ValueError:  # pragma: no cover - urlsplit rarely raises here
        return False
    return len(segments) >= 2 and segments[0] == "api" and segments[1] == "shared"


def _is_share_request(request: "Request") -> bool:
    return _is_share_path(request.path)


def _share_after(request: "Request") -> int:
    raw = request.header("last-event-id") or request.query_one("after") or "0"
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _share_alive(store: ChatStore, token_hash: str) -> bool:
    try:
        return store.share_get(token_hash) is not None
    except Exception:  # noqa: BLE001 - a liveness check fails closed
        return False


def _apply_share_headers(response: "Response") -> None:
    response.headers.update(_SHARE_HEADERS)


def _share_json_response(status: int, payload: Mapping[str, Any]) -> "Response":
    response = _json_response(status, payload)
    _apply_share_headers(response)
    return response


def _share_source(source: Any) -> dict[str, Any]:
    if not isinstance(source, Mapping):
        return {}
    return {
        key: source[key]
        for key in _SHARE_SOURCE_KEYS
        if source.get(key) is not None
    }


def _share_block(block: Any) -> dict[str, Any]:
    """Allow-list one block; every unlisted shape becomes ``unknown``."""

    if not isinstance(block, Mapping):
        return {"type": "unknown"}
    kind = block.get("type")
    if kind == "text":
        return {"type": "text", "text": str(block.get("text") or "")}
    if kind == "attachment":
        attachment = block.get("attachment")
        attachment = attachment if isinstance(attachment, Mapping) else {}
        return {
            "type": "attachment",
            "attachment": {
                key: attachment[key]
                for key in ("name", "kind", "mime", "size_bytes", "status")
                if attachment.get(key) is not None
            },
        }
    if kind == "tool_call":
        return {
            "type": "tool_call",
            "tool": block.get("tool"),
            "label": block.get("label"),
            "summary": block.get("summary"),
            "status": block.get("status"),
            "duration_ms": block.get("duration_ms"),
            "effects": list(block.get("effects") or []),
        }
    return {"type": "unknown"}


def _share_message(message: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "role": message.get("role"),
        "status": message.get("status"),
        "created_at": message.get("created_at"),
        "blocks": [_share_block(block) for block in message.get("blocks") or []],
        "sources": [
            _share_source(source) for source in message.get("sources") or []
        ],
    }


def _scrub_projection(value: Any) -> Any:
    """Replace every artifact handle anywhere in the share projection.

    Runs over the finished dict rather than at each field, so a block shape
    added later cannot forget it. Keys are never rewritten: they are fixed
    names, not user content.
    """

    if isinstance(value, str):
        return scrub_artifact_handles(value)
    if isinstance(value, Mapping):
        return {key: _scrub_projection(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_projection(item) for item in value]
    return value


# -- public share reads ----------------------------------------------------


def _shared(api: "ChatAPI", request: "Request", rest: list[str]) -> "Response":
    """The unauthenticated GET-only share surface."""

    if request.method.upper() != "GET":
        response = _share_error(
            405,
            "method_not_allowed",
            "Share links are read-only.",
        )
        response.headers["Allow"] = "GET"
        return response
    if not rest:
        return _share_not_found()
    token = rest[0]
    if len(rest) > 1 and (len(rest) != 2 or rest[1] != "stream"):
        return _share_not_found()
    limited = _limit_share(api, token)
    if limited is not None:
        return limited
    token_hash = _share_hash(token)
    record = api.service.store.share_get(token_hash)
    if record is None:
        # Unknown, malformed, expired, revoked and purged all land here.
        return _share_not_found()
    if len(rest) == 2:
        # The stream path owns the view count so an open that is refused for
        # capacity is not counted as a read.
        return _share_stream(api, request, record, token_hash)
    _record_share_view(api, token_hash)
    return _share_snapshot(record, token_hash)


def _limit_share(api: "ChatAPI", token: str) -> "Response | None":
    """Charge the per-token bucket and the global ceiling before lookup.

    Unlike the owner classes this fails closed: a limiter fault refuses the
    read instead of proceeding, because the share surface is the one place
    where "try again unfettered" is the attack.
    """

    try:
        if not api.limiter.enabled:
            return None
        allowed, retry_after = api.limiter.consume(
            "share", f"sha256:{_share_hash(token)[:24]}"
        )
        if not allowed:
            response = _rate_limited_response(
                retry_after, class_name="share", reason="rate"
            )
            _apply_share_headers(response)
            return response
        allowed, retry_after = api.limiter.consume("share_global", "global")
        if not allowed:
            response = _rate_limited_response(
                retry_after, class_name="share_global", reason="global"
            )
            _apply_share_headers(response)
            return response
        return None
    except Exception:  # noqa: BLE001 - this class fails closed
        response = _rate_limited_response(
            _RATE_LIMITED_FALLBACK_RETRY,
            class_name="share",
            reason="limiter_error",
        )
        _apply_share_headers(response)
        return response


def _record_share_view(api: "ChatAPI", token_hash: str) -> int:
    """Aggregate one view for today and audit the first one of the day."""

    from .threads import _audit_share

    try:
        day = api.service.store.now_iso()[:10]
        views = api.service.store.share_record_view(token_hash, day=day)
    except Exception:  # noqa: BLE001 - a counter never breaks a read
        log("share_view_error", level="warning", status="error")
        return 0
    if views == 1:
        _audit_share(
            api,
            "share_viewed",
            actor="share",
            token_hash=token_hash,
            extra={"day": day},
        )
    return views


def _share_snapshot(
    record: Mapping[str, Any], token_hash: str
) -> "Response":
    payload = dict(record["snapshot"])
    payload["id"] = token_hash[:_SHARE_ID_LENGTH]
    return _share_json_response(200, {"ok": True, "share": payload})


def _share_stream(
    api: "ChatAPI",
    request: "Request",
    record: Mapping[str, Any],
    token_hash: str,
) -> "Response":
    from ..api import Response

    # A share stream is the cheapest open connection in the app, but it is
    # still an open connection: a stranger with one leaked link must not be
    # able to hold every stream slot in the process. The slot is process-wide
    # because a share has no owner to key on.
    if not api._share_stream_slots.acquire():
        response = _busy_response(
            class_name="share_streams",
            reason="stream_capacity",
            limit=api._share_stream_slots.limit,
        )
        _apply_share_headers(response)
        return response
    try:
        _record_share_view(api, token_hash)
        after = _share_after(request)
        frames = _share_frames(api, token_hash, record, after=after)
    except BaseException:
        api._share_stream_slots.release()
        raise
    return Response(
        status=200,
        headers={
            **dict(_SHARE_HEADERS),
            "Content-Type": "text/event-stream; charset=utf-8",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
        stream=guarded_stream(
            frames, lambda: api._share_stream_slots.release()
        ),
    )


def _share_frames(
    api: "ChatAPI",
    token_hash: str,
    record: Mapping[str, Any],
    *,
    after: int = 0,
) -> Iterator[str]:
    """Replay the frozen snapshot, then tail its link until it dies.

    The snapshot is immutable by design, so "tail" means keeping the
    connection open with heartbeats while the link is live; no thread event
    written after creation can reach a viewer. Every tick re-checks the row,
    so revocation, expiry and purge close the stream within one tick.
    """

    snapshot = record["snapshot"]
    if after <= 0:
        yield format_event(
            {
                "seq": 0,
                "type": "snapshot",
                "ts": record["created_at"],
                "title": snapshot.get("title"),
                "created_at": record["created_at"],
                "expires_at": record["expires_at"],
                "message_count": int(snapshot.get("message_count") or 0),
            }
        )
    for index, message in enumerate(snapshot.get("messages") or [], start=1):
        if index <= after:
            continue
        yield format_event(
            {
                "seq": index,
                "type": "message",
                "ts": message.get("created_at") or record["created_at"],
                "message": message,
            }
        )
    started = time.monotonic()
    last_beat = started
    while True:
        if not _share_alive(api.service.store, token_hash):
            return
        remaining = api.stream_idle - (time.monotonic() - started)
        if remaining <= 0:
            return
        time.sleep(min(max(0.01, api.share_tick), remaining))
        if not _share_alive(api.service.store, token_hash):
            return
        if time.monotonic() - last_beat >= 15.0:
            yield format_comment()
            last_beat = time.monotonic()


def _share_error(status: int, code: str, message: str) -> "Response":
    error = ChatError(code, status=status, message=message)
    return _share_json_response(status, error.to_body())


def _share_not_found() -> "Response":
    """The one 404 builder; every failure mode goes through it unchanged."""

    return _share_error(404, "not_found", "Unknown share.")


__all__ = [
    "_apply_share_headers",
    "_is_share_path",
    "_is_share_request",
    "_share_hash",
    "_share_json_response",
    "_share_message",
    "_shared",
    "_scrub_projection",
]

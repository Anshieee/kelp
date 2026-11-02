"""Turn start, stop, retry, and the ``/api/turns`` routes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..errors import ChatError
from ._util import (
    _CLIENT_TURN_RE,
    _busy_response,
    _json_response,
    _segment,
)
from .body import _json_body

if TYPE_CHECKING:
    from ..api import ChatAPI, Request, Response


def _turns(
    api: "ChatAPI", request: "Request", method: str, rest: list[str]
) -> "Response":
    if len(rest) != 2 or method != "POST":
        raise ChatError("not_found", status=404, message="Unknown endpoint.")
    turn_id = _segment(rest[0])
    if rest[1] == "stop":
        payload = api.runner.run(api.service.stop(turn_id))
        return _json_response(202, {"ok": True, **payload})
    if rest[1] == "retry":
        return _retry_turn(api, turn_id)
    raise ChatError("not_found", status=404, message="Unknown endpoint.")


def _send(api: "ChatAPI", request: "Request", thread_id: str) -> "Response":
    body = _json_body(api, request)
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
    if not api._turn_slots.acquire():
        return _busy_response(
            class_name="turns",
            reason="turn_capacity",
            limit=api._turn_slots.limit,
            metrics=api.metrics,
        )
    try:
        payload, created = api.runner.run(
            api.service.send(
                thread_id,
                text=text,
                client_turn_id=client_turn_id,
                attachment_handles=handles,
            )
        )
    except BaseException:
        # The turn never started, so its slot is returned immediately.
        api._turn_slots.release()
        raise
    if created:
        _release_turn_slot_when_done(api, str(payload.get("turn_id", "")))
    else:
        # An idempotent replay owns no new work.
        api._turn_slots.release()
    return _json_response(202 if created else 200, {"ok": True, **payload})


def _release_turn_slot_when_done(api: "ChatAPI", turn_id: str) -> None:
    """Return a turn slot when the background turn finalizes."""

    async def waiter() -> None:
        try:
            # None means wait indefinitely; the service default is unbounded.
            await api.service.wait_for_turn(turn_id, timeout=None)  # type: ignore[arg-type]
        except BaseException:  # noqa: BLE001 - release is the contract
            pass
        finally:
            api._turn_slots.release()

    coro = waiter()
    try:
        api.runner.spawn(coro)
    except Exception:  # noqa: BLE001 - a closed runner must not leak the slot
        close = getattr(coro, "close", None)
        if close is not None:
            close()
        api._turn_slots.release()


def _retry_turn(api: "ChatAPI", turn_id: str) -> "Response":
    """Accept a retry as new work, taking a turn slot exactly like a send.

    A retry queues a fresh turn (section 5.4), so it shares the global turn cap
    and returns its slot the same way ``_send`` does: when the retried turn
    finalizes, or immediately when the call fails.
    """

    if not api._turn_slots.acquire():
        return _busy_response(
            class_name="turns",
            reason="turn_capacity",
            limit=api._turn_slots.limit,
            metrics=api.metrics,
        )
    try:
        payload = api.runner.run(api.service.retry(turn_id))
    except BaseException:
        # The retried turn never started, so its slot is returned now.
        api._turn_slots.release()
        raise
    _release_turn_slot_when_done(api, str(payload.get("turn_id", "")))
    return _json_response(202, {"ok": True, **payload})


__all__ = [
    "_release_turn_slot_when_done",
    "_retry_turn",
    "_send",
    "_turns",
]

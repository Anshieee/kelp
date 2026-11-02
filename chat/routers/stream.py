"""The owner thread SSE stream and its slot lifetime guard."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Iterator

from ..errors import ChatError
from . import auth
from ._util import _after_param, _busy_response, guarded_stream

if TYPE_CHECKING:
    from ..api import ChatAPI, Request, Response


def _stream(
    api: "ChatAPI",
    request: "Request",
    thread_id: str,
    *,
    frames_fn: Callable[..., Iterator[str]],
) -> "Response":
    from ..api import Response

    if api.service.store.get_thread(thread_id) is None:
        raise ChatError("unknown_thread", status=404)
    owner = auth._limiter_identity(api, request)
    if not api._stream_slots.acquire(owner):
        return _busy_response(
            class_name="streams",
            reason="stream_capacity",
            limit=api._stream_slots.limit,
            metrics=api.metrics,
        )
    try:
        after = _after_param(request)
        if after > 0:
            # A resume asks for a position it already had: the browser is
            # reconnecting, not opening a fresh stream.
            api.metrics.inc_sse_reconnect()
        frames = frames_fn(
            api.service.store,
            api.service.bus,
            thread_id,
            after=after,
            idle=api.stream_idle,
            auth_check=lambda: auth._authorized_for(api, request),
            thread_check=lambda: api.service.store.get_thread(thread_id) is not None,
        )
    except BaseException:
        api._stream_slots.release(owner)
        raise
    return Response(
        status=200,
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
        stream=_lease_stream(api, frames, owner),
    )


def _lease_stream(
    api: "ChatAPI", frames: Iterator[str], owner: str
) -> Iterator[str]:
    """Release a stream slot on exhaustion, ``close()``, or GC.

    A generator that was never started would not run its ``finally``, so the
    lease object is also released when the generator is collected.
    """

    return _guarded_stream(frames, lambda: api._stream_slots.release(owner))


def _guarded_stream(
    frames: Iterator[str], release: Callable[[], None]
) -> Iterator[str]:
    """Bind one idempotent release to the lifetime of a stream body."""

    return guarded_stream(frames, release)


__all__ = ["_guarded_stream", "_lease_stream", "_stream"]

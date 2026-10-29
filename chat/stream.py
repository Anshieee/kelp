"""SSE framing, replay, and fan-out for one thread (KEL-8 §4).

The persisted event log is the source of truth. A stream replays every event
with ``seq > after`` and then tails the live log; a slow consumer is
disconnected and recovers by reconnecting with its last id. Frames are plain
strings so any HTTP adapter can write them.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Iterator, Mapping

from .store import ChatStore

__all__ = [
    "HEARTBEAT_SECONDS",
    "IDLE_SECONDS",
    "RETRY_MS",
    "EventBus",
    "format_comment",
    "format_event",
    "sse_frames",
]

RETRY_MS = 2000
HEARTBEAT_SECONDS = 15.0
IDLE_SECONDS = 300.0


class EventBus:
    """Wakes tailing streams after an event is persisted.

    The store, not the bus, carries the data: a missed wake-up only means the
    next heartbeat re-reads the log.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conditions: dict[str, threading.Condition] = {}
        self._closed: set[str] = set()

    def _condition(self, thread_id: str) -> threading.Condition:
        with self._lock:
            condition = self._conditions.get(thread_id)
            if condition is None:
                condition = threading.Condition()
                self._conditions[thread_id] = condition
            return condition

    def publish(self, thread_id: str) -> None:
        condition = self._condition(thread_id)
        with condition:
            condition.notify_all()

    def wait(self, thread_id: str, timeout: float) -> bool:
        condition = self._condition(thread_id)
        with condition:
            return condition.wait(timeout=timeout)

    def close(self, thread_id: str) -> None:
        with self._lock:
            self._closed.add(thread_id)
        self.publish(thread_id)

    def is_closed(self, thread_id: str) -> bool:
        with self._lock:
            return thread_id in self._closed

    def forget(self, thread_id: str) -> None:
        with self._lock:
            self._closed.discard(thread_id)
            self._conditions.pop(thread_id, None)


def format_event(event: Mapping[str, Any], *, retry: int = RETRY_MS) -> str:
    seq = int(event["seq"])
    event_type = str(event["type"])
    data = json.dumps(event, ensure_ascii=True, separators=(",", ":"), default=str)
    return (
        f"id: {seq}\n"
        f"event: {event_type}\n"
        f"retry: {retry}\n"
        f"data: {data}\n\n"
    )


def format_comment(text: str = "keep-alive") -> str:
    return f": {text}\n\n"


def sse_frames(
    store: ChatStore,
    bus: EventBus,
    thread_id: str,
    *,
    after: int = 0,
    heartbeat: float = HEARTBEAT_SECONDS,
    idle: float = IDLE_SECONDS,
    auth_check: Callable[[], bool] | None = None,
    thread_check: Callable[[], bool] | None = None,
    batch: int = 200,
    max_events: int | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[str]:
    """Yield SSE frames until the client, auth, or the idle limit closes us."""

    last = max(0, int(after or 0))
    started = clock()
    emitted = 0
    while True:
        if auth_check is not None and not auth_check():
            return
        if thread_check is not None and not thread_check():
            return
        if bus.is_closed(thread_id):
            return

        events = store.events_after(thread_id, last, limit=batch)
        if events:
            for event in events:
                yield format_event(event)
                last = int(event["seq"])
                emitted += 1
                if max_events is not None and emitted >= max_events:
                    return
            started = clock()
            continue

        if clock() - started >= idle:
            return
        timeout = min(max(heartbeat, 0.0), max(0.0, idle - (clock() - started)))
        if timeout <= 0:
            return
        signalled = bus.wait(thread_id, timeout)
        if not signalled:
            if auth_check is not None and not auth_check():
                return
            if thread_check is not None and not thread_check():
                return
            yield format_comment()

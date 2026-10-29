"""SSE framing, replay, heartbeat, and idle close (KEL-8 §4)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from chat.store import ChatStore
from chat.stream import EventBus, format_comment, format_event, sse_frames


def _event(frame: str) -> dict:
    lines = frame.splitlines()
    data = next(line[len("data: ") :] for line in lines if line.startswith("data: "))
    return json.loads(data)


def make_store(tmp_path: Path) -> tuple[ChatStore, EventBus, str]:
    store = ChatStore(tmp_path / "chat.db")
    bus = EventBus()
    thread = store.create_thread()
    return store, bus, thread["id"]


def test_format_event_matches_the_wire_contract() -> None:
    frame = format_event(
        {"seq": 1042, "thread_id": "ses_1", "type": "token_delta", "text": "The current"},
        retry=2000,
    )
    assert frame.startswith("id: 1042\nevent: token_delta\nretry: 2000\ndata: ")
    assert frame.endswith("\n\n")
    payload = _event(frame)
    assert payload["seq"] == 1042
    assert payload["text"] == "The current"
    assert "text/event-stream" not in frame


def test_format_comment_is_an_sse_heartbeat() -> None:
    assert format_comment() == ": keep-alive\n\n"


def test_replay_from_after_and_no_duplicates(tmp_path: Path) -> None:
    store, bus, thread_id = make_store(tmp_path)
    try:
        for index in range(3):
            store.append_event(thread_id, "token_delta", {"text": str(index)})
        frames = list(
            sse_frames(
                store,
                bus,
                thread_id,
                after=1,
                heartbeat=0.01,
                idle=0.05,
                max_events=2,
            )
        )
        assert [_event(frame)["seq"] for frame in frames] == [2, 3]
        assert [_event(frame)["text"] for frame in frames] == ["1", "2"]
    finally:
        store.close()


def test_live_tail_delivers_events_published_after_the_stream_started(
    tmp_path: Path,
) -> None:
    store, bus, thread_id = make_store(tmp_path)

    def publish_later() -> None:
        time.sleep(0.02)
        store.append_event(thread_id, "token_delta", {"text": "live"})
        bus.publish(thread_id)

    try:
        writer = threading.Thread(target=publish_later)
        writer.start()
        frames = []
        for frame in sse_frames(store, bus, thread_id, heartbeat=0.01, idle=0.3):
            frames.append(frame)
            if frame.startswith("id:") and _event(frame)["text"] == "live":
                break
        writer.join(timeout=1)
        assert any(
            _event(frame)["text"] == "live" for frame in frames if frame.startswith("id:")
        )
    finally:
        store.close()


def test_heartbeat_then_idle_close(tmp_path: Path) -> None:
    store, bus, thread_id = make_store(tmp_path)
    try:
        frames = list(
            sse_frames(store, bus, thread_id, heartbeat=0.01, idle=0.06)
        )
        assert frames
        assert all(frame == ": keep-alive\n\n" for frame in frames)
        assert len(frames) >= 2
    finally:
        store.close()


def test_bus_close_stops_the_stream(tmp_path: Path) -> None:
    store, bus, thread_id = make_store(tmp_path)
    try:
        bus.close(thread_id)
        assert list(sse_frames(store, bus, thread_id, heartbeat=0.01, idle=0.05)) == []
    finally:
        store.close()


def test_auth_expiry_stops_the_stream(tmp_path: Path) -> None:
    store, bus, thread_id = make_store(tmp_path)
    calls = {"n": 0}

    def auth() -> bool:
        calls["n"] += 1
        return calls["n"] <= 1

    try:
        frames = list(
            sse_frames(
                store,
                bus,
                thread_id,
                heartbeat=0.01,
                idle=0.2,
                auth_check=auth,
            )
        )
        assert frames == []
        assert calls["n"] >= 1
    finally:
        store.close()


def test_thread_check_stops_the_stream(tmp_path: Path) -> None:
    store, bus, thread_id = make_store(tmp_path)
    try:
        frames = list(
            sse_frames(
                store,
                bus,
                thread_id,
                heartbeat=0.01,
                idle=0.2,
                thread_check=lambda: False,
            )
        )
        assert frames == []
    finally:
        store.close()

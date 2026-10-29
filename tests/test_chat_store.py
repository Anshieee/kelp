"""Durable chat store (KEL-8 §13)."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from chat.store import ChatStore


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 10, 29, 9, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        self.value += timedelta(milliseconds=10)
        return self.value


def make_store(tmp_path: Path) -> tuple[ChatStore, Clock]:
    clock = Clock()
    return ChatStore(tmp_path / "chat.db", now=clock), clock


def test_thread_crud_and_soft_delete(tmp_path: Path) -> None:
    store, _clock = make_store(tmp_path)
    try:
        thread = store.create_thread()
        assert thread["id"].startswith("ses_")
        assert thread["title"] == "New chat"
        assert thread["title_source"] == "auto"
        assert thread["active_turn_id"] is None

        renamed = store.rename_thread(thread["id"], "Nebius GPU pricing")
        assert renamed["title"] == "Nebius GPU pricing"
        assert renamed["title_source"] == "user"

        assert store.set_auto_title(thread["id"], "ignored") is False
        assert store.get_thread(thread["id"])["title"] == "Nebius GPU pricing"

        assert store.soft_delete_thread(thread["id"]) is True
        assert store.get_thread(thread["id"]) is None
        assert store.list_threads()[0] == []
        assert store.soft_delete_thread(thread["id"]) is False
    finally:
        store.close()


def test_events_are_gap_free_per_thread_and_replayable(tmp_path: Path) -> None:
    store, _clock = make_store(tmp_path)
    try:
        a = store.create_thread()
        b = store.create_thread()
        for index in range(3):
            store.append_event(a["id"], "token_delta", {"text": str(index)})
        store.append_event(b["id"], "token_delta", {"text": "b"})
        store.append_event(b["id"], "token_delta", {"text": "b2"})

        assert store.last_seq(a["id"]) == 3
        assert store.last_seq(b["id"]) == 2
        replayed = store.events_after(a["id"], 1)
        assert [event["seq"] for event in replayed] == [2, 3]
        assert [event["text"] for event in replayed] == ["1", "2"]
        assert all(event["thread_id"] == a["id"] for event in replayed)
        assert all(event["type"] == "token_delta" for event in replayed)
        assert replayed[0]["ts"].endswith("Z")
    finally:
        store.close()


def test_messages_keep_block_order_and_sources(tmp_path: Path) -> None:
    store, _clock = make_store(tmp_path)
    try:
        thread = store.create_thread()
        message = store.create_message(
            thread_id=thread["id"],
            turn_id="turn_1",
            role="assistant",
            status="streaming",
            blocks=[
                {"type": "text", "text": "first"},
                {"type": "tool_call", "call_id": "call_1"},
            ],
        )
        store.update_message(
            message["id"],
            status="complete",
            blocks=[
                {"type": "tool_call", "call_id": "call_1"},
                {"type": "text", "text": "second"},
            ],
            sources=[{"id": "src_0", "kind": "web", "url": "https://e.example"}],
            sources_total=3,
        )
        loaded = store.get_message(message["id"])
        assert loaded["status"] == "complete"
        assert [block["type"] for block in loaded["blocks"]] == ["tool_call", "text"]
        assert loaded["blocks"][1]["text"] == "second"
        assert loaded["sources"][0]["url"] == "https://e.example"
        assert loaded["sources_total"] == 3
    finally:
        store.close()


def test_message_pagination_is_oldest_first_with_has_more(tmp_path: Path) -> None:
    store, _clock = make_store(tmp_path)
    try:
        thread = store.create_thread()
        ids = []
        for index in range(3):
            message = store.create_message(
                thread_id=thread["id"],
                turn_id=f"turn_{index}",
                role="user",
                status="complete",
                blocks=[{"type": "text", "text": f"m{index}"}],
            )
            ids.append(message["id"])
        page, has_more = store.list_messages(thread["id"], limit=2)
        assert has_more is True
        assert [message["id"] for message in page] == ids[1:]
        older, has_more = store.list_messages(thread["id"], limit=2, before=ids[1])
        assert has_more is False
        assert [message["id"] for message in older] == [ids[0]]
    finally:
        store.close()


def test_turns_are_unique_per_client_id_and_latest_wins(tmp_path: Path) -> None:
    store, _clock = make_store(tmp_path)
    try:
        thread = store.create_thread()
        first = store.create_turn(
            thread_id=thread["id"],
            user_message_id="msg_1",
            assistant_message_id="msg_2",
            client_turn_id="ct-one",
            budget={"max_steps": 6},
        )
        second = store.create_turn(
            thread_id=thread["id"],
            user_message_id="msg_3",
            assistant_message_id="msg_4",
            client_turn_id="ct-two",
            budget={"max_steps": 6},
        )
        assert store.find_turn_by_client_id(thread["id"], "ct-one")["id"] == first["id"]
        assert store.find_turn_by_client_id(thread["id"], "ct-missing") is None
        assert store.latest_turn(thread["id"])["id"] == second["id"]

        store.update_turn(first["id"], state="finished", reason="stop")
        store.update_turn(second["id"], state="finished", reason="stop")
        assert store.running_turns() == []
        assert store.get_turn(first["id"])["reason"] == "stop"
    finally:
        store.close()


def test_approvals_roundtrip_and_status_updates(tmp_path: Path) -> None:
    store, _clock = make_store(tmp_path)
    try:
        thread = store.create_thread()
        card = {
            "id": "apr_1",
            "thread_id": thread["id"],
            "turn_id": "turn_1",
            "call_id": "call_1",
            "source": "skill",
            "status": "pending",
            "requested_at": "2026-10-29T09:00:00Z",
            "expires_at": "2026-10-29T09:10:00Z",
            "effects": ["write"],
        }
        store.save_approval(card)
        assert store.get_approval("apr_1")["status"] == "pending"
        assert store.list_approvals(status="pending")[0]["id"] == "apr_1"

        store.set_approval_status("apr_1", "approved", decided_at="2026-10-29T09:01:00Z")
        approved = store.get_approval("apr_1")
        assert approved["status"] == "approved"
        assert approved["decided_at"] == "2026-10-29T09:01:00Z"
        assert store.list_approvals(status="pending") == []

        store.set_approval_status("apr_1", "consumed", consumed_at="2026-10-29T09:02:00Z")
        consumed = store.get_approval("apr_1")
        assert consumed["status"] == "consumed"
        assert consumed["consumed_at"] == "2026-10-29T09:02:00Z"
        assert consumed["decided_at"] == "2026-10-29T09:01:00Z"
    finally:
        store.close()


def test_approval_transitions_are_guarded_and_never_reopen(
    tmp_path: Path,
) -> None:
    """A settled approval is terminal; a stale writer cannot revive it."""

    store, _clock = make_store(tmp_path)
    try:
        thread = store.create_thread()
        card = {
            "id": "apr_guard",
            "thread_id": thread["id"],
            "turn_id": None,
            "call_id": None,
            "source": "skill",
            "status": "pending",
            "requested_at": "2026-10-29T09:00:00Z",
            "expires_at": "2026-10-29T09:10:00Z",
            "effects": ["write"],
        }
        store.save_approval(card)

        approved = store.transition_approval(
            "apr_guard", "approved", allowed_from=("pending",)
        )
        assert approved is not None
        assert approved["status"] == "approved"
        # The second caller of the same transition loses the guard.
        assert (
            store.transition_approval(
                "apr_guard", "denied", allowed_from=("pending",)
            )
            is None
        )
        # Re-saving the original pending card must not undo the decision.
        store.save_approval(dict(card))
        assert store.get_approval("apr_guard")["status"] == "approved"

        consumed = store.transition_approval(
            "apr_guard", "consumed", allowed_from=("approved",)
        )
        assert consumed is not None
        assert consumed["status"] == "consumed"
        # And a late approve/expire cannot move a consumed row either way.
        assert (
            store.transition_approval(
                "apr_guard", "expired", allowed_from=("pending", "approved")
            )
            is None
        )
        store.save_approval({**card, "status": "approved"})
        assert store.get_approval("apr_guard")["status"] == "consumed"
        assert store.transition_approval("apr_missing", "approved") is None
    finally:
        store.close()


def test_concurrent_approval_transitions_have_one_winner(tmp_path: Path) -> None:
    """The SQL guard, not a Python compare-and-set, picks the winner."""

    store, _clock = make_store(tmp_path)
    try:
        thread = store.create_thread()
        for round_index in range(20):
            approval_id = f"apr_race{round_index}"
            store.save_approval(
                {
                    "id": approval_id,
                    "thread_id": thread["id"],
                    "turn_id": None,
                    "call_id": None,
                    "source": "skill",
                    "status": "pending",
                    "requested_at": "2026-10-29T09:00:00Z",
                    "expires_at": "2099-01-01T00:00:00Z",
                    "effects": ["write"],
                }
            )
            barrier = threading.Barrier(3)
            wins: list[str | None] = []

            def worker(status: str) -> None:
                barrier.wait()
                result = store.transition_approval(
                    approval_id, status, allowed_from=("pending",)
                )
                wins.append(status if result is not None else None)

            threads = [
                threading.Thread(target=worker, args=("approved",)),
                threading.Thread(target=worker, args=("denied",)),
            ]
            for item in threads:
                item.start()
            barrier.wait()
            for item in threads:
                item.join()
            winners = [win for win in wins if win is not None]
            assert len(winners) == 1
            assert store.get_approval(approval_id)["status"] == winners[0]
    finally:
        store.close()


def test_artifact_binding_is_write_once(tmp_path: Path) -> None:
    store, _clock = make_store(tmp_path)
    try:
        owner = store.create_thread()
        other = store.create_thread()
        meta = store.create_artifact(
            {
                "handle": "artifact:q3report01",
                "thread_id": None,
                "name": "q3.pdf",
                "kind": "pdf",
                "mime": "application/pdf",
                "size_bytes": 10,
                "sha256": "abc",
                "storage_path": "q3.pdf",
            }
        )
        assert meta["thread_id"] is None
        assert store.bind_artifact("artifact:q3report01", owner["id"]) is True
        assert store.bind_artifact("artifact:q3report01", other["id"]) is False
        assert store.get_artifact("artifact:q3report01")["thread_id"] == owner["id"]
        assert store.bind_artifact("artifact:missing", owner["id"]) is False
    finally:
        store.close()


def test_snapshot_reports_thread_messages_and_last_seq(tmp_path: Path) -> None:
    store, _clock = make_store(tmp_path)
    try:
        thread = store.create_thread()
        store.create_message(
            thread_id=thread["id"],
            turn_id="turn_1",
            role="user",
            status="complete",
            blocks=[{"type": "text", "text": "hi"}],
        )
        store.append_event(thread["id"], "message_created", {"message_id": "msg_1"})
        snapshot_thread, messages, last_seq, has_more = store.snapshot(thread["id"])
        assert snapshot_thread["id"] == thread["id"]
        assert len(messages) == 1
        assert last_seq == 1
        assert has_more is False
        assert store.snapshot("ses_missing") == (None, [], 0, False)
    finally:
        store.close()


def test_thread_cursor_pagination(tmp_path: Path) -> None:
    store, _clock = make_store(tmp_path)
    try:
        for index in range(3):
            thread = store.create_thread(title=f"t{index}", title_source="user")
            store.touch_thread(thread["id"])
        first, cursor = store.list_threads(limit=2)
        assert len(first) == 2
        assert cursor is not None
        second, next_cursor = store.list_threads(limit=2, cursor=cursor)
        assert len(second) == 1
        assert next_cursor is None
        assert {thread["id"] for thread in first + second} == {
            thread["id"] for thread in store.list_threads(limit=10)[0]
        }
    finally:
        store.close()

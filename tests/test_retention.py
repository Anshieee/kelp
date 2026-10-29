"""KEL-21 retention: hard purge, orphan sweep, VACUUM, and log rotation."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from chat import ChatService, ChatStore
from chat.artifacts import ArtifactRegistry
from chat.retention import (
    RETENTION_ROWS,
    RetentionPolicy,
    rotate_jsonl,
)
from kernel.provider import StreamEnd, TextDelta, UsageReport
from kernel.usage import JsonlUsageStore, SpendCaps, SpendGuard, UsageRecord
from openshell.audit import AuditLog

ROOT = Path(__file__).resolve().parents[1]
BASE = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _store_and_registry(
    tmp_path: Path, *, now: object | None = None
) -> tuple[ChatStore, ArtifactRegistry, Path]:
    workspace = tmp_path / "workspace"
    artifact_dir = workspace / "var" / "artifacts"
    store = ChatStore(tmp_path / "chat.db")
    registry = ArtifactRegistry(
        store, artifact_dir, now=now, workspace_root=workspace
    )
    return store, registry, workspace


def test_purge_thread_removes_rows_files_and_index_and_is_idempotent(
    tmp_path: Path,
) -> None:
    store, registry, workspace = _store_and_registry(tmp_path)
    try:
        thread = store.create_thread(title="to delete")
        message = store.create_message(
            thread_id=thread["id"],
            turn_id="turn_1",
            role="user",
            status="complete",
            blocks=[{"type": "text", "text": "secret text"}],
        )
        store.create_message(
            thread_id=thread["id"],
            turn_id="turn_1",
            role="assistant",
            status="complete",
            blocks=[{"type": "text", "text": "answer"}],
        )
        store.create_turn(
            thread_id=thread["id"],
            user_message_id=message["id"],
            assistant_message_id=message["id"],
            client_turn_id="ct-purge",
            budget={},
            turn_id="turn_1",
        )
        store.append_event(thread["id"], "token_delta", {"text": "secret"})
        store.save_approval(
            {
                "id": "apr_purge",
                "thread_id": thread["id"],
                "turn_id": "turn_1",
                "source": "skill",
                "status": "pending",
                "requested_at": _iso(BASE),
                "expires_at": _iso(BASE + timedelta(minutes=5)),
            }
        )
        meta = registry.save_upload("q3.pdf", b"%PDF-1.4\n%%EOF")
        registry.claim(meta["handle"], thread["id"])
        storage = registry.root / meta["storage_path"]
        assert storage.exists()
        token = meta["handle"].split(":", 1)[1]
        index_dir = workspace / "var" / "index" / token
        (index_dir / "pdf").mkdir(parents=True)
        (index_dir / "pdf" / "abc.json").write_text("{}", encoding="utf-8")

        report = store.purge_thread(
            thread["id"], on_artifacts=registry.purge
        )

        assert report["deleted"] is True
        assert report["messages"] == 2
        assert report["blocks"] == 2
        assert report["turns"] == 1
        assert report["events"] == 1
        assert report["approvals"] == 1
        assert report["artifacts"] == 1
        assert report["files"] == {
            "files_removed": 1,
            "files_missing": 0,
            "indexes_removed": 1,
            "errors": 0,
        }
        assert not storage.exists()
        assert not index_dir.exists()
        assert store.get_thread(thread["id"]) is None
        assert store.get_turn("turn_1") is None
        assert store.get_message(message["id"]) is None
        assert store.get_approval("apr_purge") is None
        assert store.events_after(thread["id"]) == []
        assert store.get_artifact(meta["handle"]) is None

        second = store.purge_thread(thread["id"], on_artifacts=registry.purge)
        assert second["deleted"] is False
        assert second["messages"] == 0
        assert second["artifacts"] == 0
        assert "files" not in second
    finally:
        store.close()


def test_purge_vacuums_and_shrinks_the_database_file(tmp_path: Path) -> None:
    path = tmp_path / "chat.db"
    store = ChatStore(path)
    thread = store.create_thread()
    payload = "x" * 8192
    with store.transaction():
        for index in range(400):
            store.create_message(
                thread_id=thread["id"],
                turn_id=f"turn_{index}",
                role="user",
                status="complete",
                blocks=[{"type": "text", "text": payload}],
            )
    store.close()
    before = path.stat().st_size
    assert before > 1_000_000

    store = ChatStore(path)
    try:
        report = store.purge_thread(thread["id"])
        assert report["vacuumed"] is True
    finally:
        store.close()
    after = path.stat().st_size
    assert after < before


class _SlowProvider:
    async def stream(self, request: object) -> object:
        yield TextDelta("partial ")
        await asyncio.sleep(10)
        yield UsageReport(10, 1, 11)
        yield StreamEnd("stop")


def test_purge_during_a_running_turn_settles_it_first(tmp_path: Path) -> None:
    store, registry, _workspace = _store_and_registry(tmp_path)
    service = ChatService(
        store=store,
        provider=_SlowProvider(),
        model="nvidia/Nemotron-3_5-Super-VL",
        artifacts=registry,
    )
    try:
        thread = service.create_thread()
        meta = registry.save_upload("a.pdf", b"%PDF-1.4\n%%EOF")
        registry.claim(meta["handle"], thread["id"])
        storage = registry.root / meta["storage_path"]

        async def scenario() -> None:
            payload, _created = await service.send(
                thread["id"], text="go", client_turn_id="ct-retention"
            )
            await asyncio.sleep(0.02)
            result = await service.delete_thread(thread["id"])
            assert result["ok"] is True
            assert result["deleted"] is True
            assert store.get_thread(thread["id"]) is None
            assert store.get_turn(payload["turn_id"]) is None
            assert store.get_message(payload["user_message_id"]) is None
            assert store.get_message(payload["assistant_message_id"]) is None
            assert store.running_turns() == []
            assert store.events_after(thread["id"]) == []
            assert not storage.exists()

        asyncio.run(scenario())
    finally:
        store.close()


def test_orphan_sweep_removes_parentless_rows_files_and_indexes(tmp_path: Path) -> None:
    store, registry, workspace = _store_and_registry(
        tmp_path, now=lambda: _iso(BASE)
    )
    try:
        live = store.create_thread(title="live")
        # Old unbound upload: orphan.
        old = registry.save_upload("old.pdf", b"%PDF-1.4\n%%EOF")
        old_path = registry.root / old["storage_path"]
        os.utime(old_path, (BASE.timestamp(), BASE.timestamp()))
        # Old unbound upload referenced by a message block: must survive.
        referenced = registry.save_upload("ref.pdf", b"%PDF-1.4\n%%EOF")
        store.create_message(
            thread_id=live["id"],
            turn_id="turn_live",
            role="user",
            status="complete",
            blocks=[
                {
                    "type": "attachment",
                    "attachment": {"handle": referenced["handle"]},
                }
            ],
        )
        # Fresh unbound upload: inside the grace window, must survive.
        fresh = store.create_artifact(
            {
                "handle": "artifact:freshupload01",
                "thread_id": None,
                "name": "fresh.pdf",
                "kind": "pdf",
                "mime": "application/pdf",
                "size_bytes": 3,
                "sha256": "fresh",
                "storage_path": "freshupload01.pdf",
                "created_at": _iso(BASE + timedelta(hours=3)),
            }
        )
        (registry.root / fresh["storage_path"]).write_bytes(b"%PDF")

        # A file that never got a row (crash between write and insert).
        orphan_file = registry.root / "crashupload01.pdf"
        orphan_file.write_bytes(b"%PDF")
        os.utime(orphan_file, (BASE.timestamp(), BASE.timestamp()))

        old_token = old["handle"].split(":", 1)[1]
        live_token = referenced["handle"].split(":", 1)[1]
        for token in (old_token, live_token):
            directory = workspace / "var" / "index" / token
            (directory / "pdf").mkdir(parents=True)
            (directory / "pdf" / "x.json").write_text("{}", encoding="utf-8")

        report = registry.sweep_orphans(
            older_than_seconds=3600,
            now=BASE + timedelta(hours=2),
        )

        assert report["artifact_rows"] == 1
        assert store.get_artifact(old["handle"]) is None
        assert store.get_artifact(referenced["handle"]) is not None
        assert store.get_artifact(fresh["handle"]) is not None
        assert not old_path.exists()
        assert (registry.root / referenced["storage_path"]).exists()
        assert not orphan_file.exists()
        assert not (workspace / "var" / "index" / old_token).exists()
        assert (workspace / "var" / "index" / live_token).exists()
    finally:
        store.close()


def test_orphan_sweep_respects_the_age_threshold(tmp_path: Path) -> None:
    store, registry, _workspace = _store_and_registry(tmp_path)
    try:
        orphan = registry.root / "youngupload1.pdf"
        orphan.write_bytes(b"%PDF")
        now = datetime.now(timezone.utc)
        os.utime(orphan, (now.timestamp(), now.timestamp()))
        registry.sweep_orphans(older_than_seconds=3600, now=now)
        assert orphan.exists()
    finally:
        store.close()


def test_audit_rotation_keeps_last_n_and_never_loses_lines(tmp_path: Path) -> None:
    path = tmp_path / "audit" / "audit.jsonl"
    audit = AuditLog(path)
    now = BASE
    audit.record("first", request_id="req_1")
    audit.record("second", request_id="req_2")
    assert len(audit.entries()) == 2

    rotate_jsonl(
        path,
        rotate_seconds=86400,
        ttl_seconds=30 * 86400,
        keep=5,
        now=now,
        force=True,
    )
    audit.record("third", request_id="req_3")
    # The rotated segment is still read: no line was copied or truncated away.
    assert len(audit.entries()) == 3

    for index in range(6):
        audit.record(f"event_{index}", request_id=f"req_{index + 10}")
        rotate_jsonl(
            path,
            rotate_seconds=86400,
            ttl_seconds=30 * 86400,
            keep=2,
            now=now + timedelta(minutes=index + 1),
            force=True,
        )
    segments = sorted(path.parent.glob(path.name + ".*"))
    assert len(segments) == 2
    assert len(audit.entries()) == 2


def test_rotation_expires_segments_older_than_ttl(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    path.write_text('{"event":"old"}\n', encoding="utf-8")
    old = BASE.timestamp()
    os.utime(path, (old, old))
    rotate_jsonl(
        path,
        rotate_seconds=86400,
        ttl_seconds=3600,
        keep=5,
        now=BASE + timedelta(days=2),
        force=True,
    )
    assert sorted(path.parent.glob(path.name + ".*")) == []
    assert not path.exists()

    path.write_text('{"event":"new"}\n', encoding="utf-8")
    rotate_jsonl(
        path,
        rotate_seconds=86400,
        ttl_seconds=3600,
        keep=5,
        now=datetime.now(timezone.utc),
        force=True,
    )
    assert len(list(path.parent.glob(path.name + ".*"))) == 1


def test_usage_rotation_preserves_the_spend_window(tmp_path: Path) -> None:
    path = tmp_path / "usage" / "usage.jsonl"
    store = JsonlUsageStore(path)
    moment = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
    stamp = _iso(moment)
    store.record(
        UsageRecord(
            turn_id="turn_1",
            session_id="ses_1",
            model="nvidia/Nemotron-3_5-Super-VL",
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            estimated_cost_usd=0.25,
            cost_known=True,
            usage_reported=True,
            steps=1,
            tool_calls=0,
            started_at=stamp,
            finished_at=stamp,
            duration_ms=10,
            outcome="stop",
        )
    )
    guard = SpendGuard(store, SpendCaps(day_usd=10.0), now_fn=lambda: moment)
    assert guard.evaluate().day.known_usd == pytest.approx(0.25)

    rotate_jsonl(
        path,
        rotate_seconds=86400,
        ttl_seconds=90 * 86400,
        keep=5,
        now=moment,
        force=True,
    )
    assert len(store.records()) == 1
    assert guard.evaluate().day.known_usd == pytest.approx(0.25)


def test_retention_table_is_documented_in_deploy_runbook() -> None:
    deploy = (ROOT / "docs" / "DEPLOY.md").read_text(encoding="utf-8")
    for row in RETENTION_ROWS:
        assert row["class"] in deploy
    policy = RetentionPolicy()
    assert policy.audit_ttl_seconds == 30 * 86400
    assert policy.usage_ttl_seconds == 90 * 86400
    assert policy.artifact_orphan_seconds == 3600

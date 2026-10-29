"""KEL-21 retention: hard purge, orphan sweep, VACUUM, and log rotation."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from chat import ChatError, ChatService, ChatStore
from chat.artifacts import ArtifactRegistry
from chat.retention import (
    RETENTION_ROWS,
    RetentionMaintenance,
    RetentionPolicy,
    rotate_jsonl,
)
from chat.store import ThreadBusyError
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
        store.update_turn(
            "turn_1", state="finished", reason="stop", finished_at=_iso(BASE)
        )
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


def test_purge_refuses_while_a_turn_is_live_and_deletes_nothing(
    tmp_path: Path,
) -> None:
    store, registry, _workspace = _store_and_registry(tmp_path)
    try:
        thread = store.create_thread(title="busy")
        message = store.create_message(
            thread_id=thread["id"],
            turn_id="turn_live",
            role="user",
            status="streaming",
            blocks=[{"type": "text", "text": "hello"}],
        )
        store.create_turn(
            thread_id=thread["id"],
            user_message_id=message["id"],
            assistant_message_id=message["id"],
            client_turn_id="ct-live",
            budget={},
            turn_id="turn_live",
        )
        meta = registry.save_upload("busy.pdf", b"%PDF-1.4\n%%EOF")
        registry.claim(meta["handle"], thread["id"])

        with pytest.raises(ThreadBusyError) as caught:
            store.purge_thread(thread["id"], on_artifacts=registry.purge)
        assert caught.value.thread_id == thread["id"]
        assert caught.value.turn_ids == ["turn_live"]

        # The transaction aborted before the first delete: nothing was removed,
        # so the client can simply retry the same request.
        assert store.get_thread(thread["id"]) is not None
        assert store.get_turn("turn_live")["state"] == "queued"
        assert store.get_message(message["id"]) is not None
        assert store.get_artifact(meta["handle"]) is not None

        store.update_turn("turn_live", state="finished", reason="stop")
        report = store.purge_thread(thread["id"], on_artifacts=registry.purge)
        assert report["deleted"] is True
        assert store.get_thread(thread["id"]) is None
    finally:
        store.close()


def test_delete_thread_maps_a_live_turn_refusal_to_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, registry, _workspace = _store_and_registry(tmp_path)
    service = ChatService(
        store=store,
        provider=None,
        model="nvidia/Nemotron-3_5-Super-VL",
        artifacts=registry,
    )
    try:
        thread = service.create_thread()

        def refuse(thread_id: str, **kwargs: object) -> dict[str, object]:
            raise ThreadBusyError(thread_id, ["turn_race"])

        monkeypatch.setattr(store, "purge_thread", refuse)

        async def scenario() -> None:
            with pytest.raises(ChatError) as caught:
                await service.delete_thread(thread["id"])
            error = caught.value
            assert error.code == "turn_in_progress"
            assert error.status == 409
            assert error.error["details"] == {"active_turn_id": "turn_race"}

        asyncio.run(scenario())
        # A refused delete keeps the thread alive and returns no payload.
        assert store.get_thread(thread["id"]) is not None
    finally:
        store.close()


def test_delete_thread_settles_a_taskless_stale_turn_then_purges(
    tmp_path: Path,
) -> None:
    store, registry, _workspace = _store_and_registry(tmp_path)
    service = ChatService(
        store=store,
        provider=None,
        model="nvidia/Nemotron-3_5-Super-VL",
        artifacts=registry,
    )
    try:
        thread = service.create_thread()
        message = store.create_message(
            thread_id=thread["id"],
            turn_id="turn_stale",
            role="user",
            status="streaming",
            blocks=[],
        )
        store.create_turn(
            thread_id=thread["id"],
            user_message_id=message["id"],
            assistant_message_id=message["id"],
            client_turn_id="ct-stale",
            budget={},
            turn_id="turn_stale",
        )

        async def scenario() -> dict[str, object]:
            return await service.delete_thread(thread["id"])

        result = asyncio.run(scenario())
        assert result["ok"] is True
        assert result["deleted"] is True
        assert result["turns"] == 1  # settled as interrupted, then purged
        assert store.get_thread(thread["id"]) is None
        assert store.get_turn("turn_stale") is None
    finally:
        store.close()


def test_orphan_delete_only_removes_rows_parentless_at_delete_time(
    tmp_path: Path,
) -> None:
    store, _registry, _workspace = _store_and_registry(tmp_path)
    try:
        live = store.create_thread(title="live")
        old = _iso(BASE)

        def row(handle: str, thread_id: str | None) -> dict[str, object]:
            return store.create_artifact(
                {
                    "handle": handle,
                    "thread_id": thread_id,
                    "name": f"{handle}.pdf",
                    "kind": "pdf",
                    "mime": "application/pdf",
                    "size_bytes": 3,
                    "sha256": handle,
                    "storage_path": f"{handle.split(':', 1)[1]}.pdf",
                    "created_at": old,
                }
            )

        orphan = row("artifact:orphanrow01", None)
        # Claimed after it aged: a concurrent claim wins, and the count must
        # reflect only what the DELETE actually removed.
        claimed = row("artifact:claimedrow1", None)
        assert store.bind_artifact(claimed["handle"], live["id"]) is True
        skipped = row("artifact:skippedrow1", None)
        # A row whose thread row is gone is still an orphan (interrupted delete).
        dangling = row("artifact:danglingrow", "ses_missing")

        deleted = store.delete_orphan_artifacts(
            _iso(BASE + timedelta(hours=2)),
            skip_handles=(skipped["handle"],),
        )
        handles = {record["handle"] for record in deleted}

        assert handles == {orphan["handle"], dangling["handle"]}
        assert store.get_artifact(orphan["handle"]) is None
        assert store.get_artifact(dangling["handle"]) is None
        assert store.get_artifact(claimed["handle"]) is not None
        assert store.get_artifact(skipped["handle"]) is not None

        # Idempotent: a second sweep finds nothing and reports zero.
        assert (
            store.delete_orphan_artifacts(
                _iso(BASE + timedelta(hours=2)), skip_handles=(skipped["handle"],)
            )
            == []
        )
    finally:
        store.close()


class _SpyStore:
    def __init__(self) -> None:
        self.vacuums = 0

    def vacuum(self) -> bool:
        self.vacuums += 1
        return True


class _SpyArtifacts:
    def __init__(self, rows: int = 0, *, fail: bool = False) -> None:
        self.calls = 0
        self._rows = rows
        self._fail = fail
        self._lock = threading.Lock()

    def sweep_orphans(self, *, older_than_seconds: float, now: object) -> dict[str, int]:
        with self._lock:
            self.calls += 1
        if self._fail:
            raise RuntimeError("sweep exploded")
        return {
            "artifact_rows": self._rows,
            "files_removed": 0,
            "files_missing": 0,
            "indexes_removed": 0,
            "skipped": 0,
            "errors": 0,
        }


def test_maintenance_run_once_wires_sweep_rotation_and_vacuum(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit" / "audit.jsonl"
    usage_path = tmp_path / "usage" / "usage.jsonl"
    audit_path.parent.mkdir(parents=True)
    usage_path.parent.mkdir(parents=True)
    stamp = _iso(BASE)
    audit_path.write_text(
        json.dumps({"event": "old", "ts": stamp}) + "\n", encoding="utf-8"
    )
    usage_path.write_text(json.dumps({"ts": stamp, "usd": 0.1}) + "\n", encoding="utf-8")

    store = _SpyStore()
    artifacts = _SpyArtifacts(rows=2)
    maintenance = RetentionMaintenance(
        store=store,
        artifacts=artifacts,
        audit_path=audit_path,
        usage_path=usage_path,
        policy=RetentionPolicy(
            audit_ttl_seconds=86400,
            usage_ttl_seconds=86400,
            audit_keep=2,
            usage_keep=2,
            log_rotate_seconds=3600,
            log_max_bytes=10 * 1024 * 1024,
            artifact_orphan_seconds=3600,
            maintenance_interval_seconds=86400,
        ),
        now=lambda: BASE + timedelta(days=2),
    )

    report = maintenance.run_once()

    assert artifacts.calls == 1
    assert report["orphans"]["artifact_rows"] == 2
    assert report["audit"]["rotated"] is True
    assert report["usage"]["rotated"] is True
    assert report["vacuumed"] is True
    assert store.vacuums == 1

    # Nothing removed this pass: the pass reports no vacuum instead of calling
    # VACUUM on a database it did not touch.
    artifacts._rows = 0
    quiet = maintenance.run_once()
    assert quiet["vacuumed"] is False
    assert store.vacuums == 1


def test_maintenance_daemon_runs_until_stop_and_survives_failures(
    tmp_path: Path,
) -> None:
    artifacts = _SpyArtifacts(rows=0, fail=True)
    maintenance = RetentionMaintenance(
        store=_SpyStore(),
        artifacts=artifacts,
        audit_path=tmp_path / "audit.jsonl",
        policy=RetentionPolicy(maintenance_interval_seconds=0.02),
    )
    named = lambda: [  # noqa: E731 - tiny predicate for the assertion below
        thread for thread in threading.enumerate() if thread.name == "kelp-retention"
    ]
    maintenance.start()
    try:
        deadline = time.monotonic() + 5.0
        while artifacts.calls < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        # A failing pass is logged, never fatal: the daemon keeps going.
        assert artifacts.calls >= 3
        assert len(named()) == 1
        # start() is idempotent: a second call does not spawn a second thread.
        maintenance.start()
        assert len(named()) == 1
    finally:
        maintenance.stop(timeout=5.0)
    assert named() == []
    settled = artifacts.calls
    time.sleep(0.05)
    assert artifacts.calls == settled


def test_maintenance_start_is_a_noop_when_the_interval_is_zero() -> None:
    maintenance = RetentionMaintenance(
        store=_SpyStore(),
        artifacts=_SpyArtifacts(),
        audit_path="/nonexistent/audit.jsonl",
        policy=RetentionPolicy(maintenance_interval_seconds=0),
    )
    maintenance.start()
    assert [
        thread for thread in threading.enumerate() if thread.name == "kelp-retention"
    ] == []
    maintenance.stop()


def test_request_log_row_matches_the_compose_size_bound() -> None:
    """B4: the table claims a size bound, so the compose block must show one."""

    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert 'max-size: "10m"' in compose
    assert 'max-file: "3"' in compose
    assert "max-age" not in compose

    row = next(
        entry for entry in RETENTION_ROWS if entry["class"] == "Request/correlation logs"
    )
    assert "10 MiB" in row["ttl"]
    assert "3 files" in row["ttl"]
    assert "14 days" not in row["ttl"]
    assert "docker-compose.yml" in row["override"]

    deploy = (ROOT / "docs" / "DEPLOY.md").read_text(encoding="utf-8")
    assert "14 days" not in deploy
    assert 'max-file: "14"' not in deploy
    assert "Size-bounded, not age-bounded" in deploy

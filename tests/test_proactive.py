"""KEL-27 M4-B proactive turn runner tests.

Everything here is offline: a stub provider, a real ``ChatStore`` in a
``tmp_path``, the M4-A scheduler with an injected clock, and the API's
``LoopRunner`` so proactive turns run on the same event loop as interactive
ones. No test sleeps for the scheduler's tick interval.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from chat import ChatService, ChatStore
from chat.api import LoopRunner
from chat.proactive import (
    TRIGGER_BLOCK_TYPE,
    ProactiveConfig,
    ProactiveRunner,
)
from chat.scheduler import ScheduleStore, SchedulerRuntime
from chat.server import ServerConfig, build_app
from chat.stream import sse_frames
from kernel import CallableToolExecutor
from kernel.provider import StreamEnd, TextDelta, UsageReport
from kernel.usage import (
    JsonlUsageStore,
    SpendCaps,
    SpendGuard,
    UsageRecord,
)
from openshell.audit import AuditLog
from skills.context import ApprovalRequest

from .chatkit import MODEL, StubProvider, text_script, tool_script

BASE = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
DUE = BASE + timedelta(seconds=60)


class Clock:
    def __init__(self, moment: datetime = BASE):
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **kwargs: float) -> None:
        self.moment = self.moment + timedelta(**kwargs)


class ForbiddenProvider:
    """A provider that fails the test if a turn ever reaches it."""

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, request: Any) -> Any:
        self.calls += 1
        raise AssertionError("a skipped proactive run must not call the provider")
        yield  # pragma: no cover - makes this an async generator


class GateProvider:
    """Holds the first turn open until the test releases it."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    async def stream(self, request: Any) -> Any:
        self.started.set()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.release.wait)
        yield TextDelta("done")
        yield UsageReport(1, 1, 2)
        yield StreamEnd("stop")


class Rig:
    def __init__(
        self,
        tmp_path: Path,
        *,
        scripts: list[list[Any]] | None = None,
        provider: Any | None = None,
        config: ProactiveConfig | None = None,
        **service_kwargs: Any,
    ):
        self.tmp_path = tmp_path
        self.clock = Clock()
        self.audit = AuditLog(tmp_path / "audit" / "audit.jsonl")
        self.store = ChatStore(tmp_path / "chat.db")
        self.provider = provider or StubProvider(scripts or [text_script("ok")])
        self.service = ChatService(
            store=self.store,
            provider=self.provider,
            model=MODEL,
            audit=self.audit,
            **service_kwargs,
        )
        self.schedule_store = ScheduleStore(tmp_path / "workspace", now=self.clock)
        self.runner = LoopRunner()
        self.proactive = ProactiveRunner(
            service=self.service,
            store=self.store,
            schedule_store=self.schedule_store,
            audit=self.audit,
            runner=self.runner,
            config=config or ProactiveConfig(),
            now=self.clock,
        )
        self.scheduler = SchedulerRuntime(
            self.schedule_store,
            on_fire=self.proactive.fire,
            audit=self.proactive.audit_gate,
            now=self.clock,
        )

    def create_due_task(self, **overrides: Any) -> dict[str, Any]:
        values: dict[str, Any] = {
            "title": "Morning brief",
            "prompt": "Summarize the open issues",
            "expr": "* * * * *",
            "timezone": "UTC",
            "now": self.clock(),
            "audit": self.audit,
        }
        values.update(overrides)
        return self.schedule_store.create_task(**values)

    def tick(self, moment: datetime | None = None) -> dict[str, Any]:
        return self.scheduler.tick(now=moment or DUE)

    def close(self) -> None:
        self.scheduler.stop()
        try:
            self.runner.run(self.service.aclose(), timeout=5.0)
        except Exception:  # noqa: BLE001 - a fixture must not mask the failure
            pass
        self.proactive.close(5.0)
        self.runner.close()
        self.store.close()


def _turns(store_path: Path, thread_id: str) -> int:
    connection = sqlite3.connect(str(store_path))
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM turns WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return int(row[0])
    finally:
        connection.close()


def test_one_due_task_runs_one_turn_and_writes_one_run_row(tmp_path: Path) -> None:
    rig = Rig(tmp_path, scripts=[text_script("Morning brief ready")])
    try:
        task = rig.create_due_task()
        report = rig.tick()

        assert report["due"] == 1
        assert rig.proactive.wait_for_idle(5.0) is True

        thread_id = rig.proactive.thread_id_for(task["task_id"])
        thread = rig.store.get_thread(thread_id)
        assert thread is not None
        assert thread["title_source"] == "schedule"
        assert thread["title"] == "Scheduled: Morning brief"

        messages, _ = rig.store.list_messages(thread_id)
        assert len(messages) == 2
        user = messages[0]
        assert user["role"] == "user"
        assert user["blocks"][0]["type"] == TRIGGER_BLOCK_TYPE
        assert user["blocks"][0]["origin"] == "schedule"
        assert user["blocks"][0]["schedule_id"] == task["task_id"]
        assert user["blocks"][0]["scheduled_for"] == "2026-10-30T12:01:00Z"
        assert user["blocks"][1]["text"] == "Summarize the open issues"

        turn = rig.store.latest_turn(thread_id)
        assert turn is not None and turn["state"] == "finished"
        assert turn["reason"] == "stop"
        assert turn["usage"]["input_tokens"] == 10
        assert turn["usage"]["output_tokens"] == 5

        rows = rig.audit.entries(event="schedule_run")
        assert len(rows) == 1
        row = rows[0]
        assert row["actor"] == "schedule"
        assert row["outcome"] == "ok"
        assert row["turn_id"] == turn["id"]
        extra = row["extra"]
        assert extra["schedule_id"] == task["task_id"]
        assert extra["run_id"] == user["blocks"][0]["run_id"]
        assert extra["origin"] == "schedule"
        assert extra["on_behalf_of"] == "owner"
        assert extra["origin_ref"] == {
            "schedule_id": task["task_id"],
            "run_id": extra["run_id"],
        }
        assert extra["input_tokens"] == 10
        assert extra["output_tokens"] == 5
        assert extra["reason"] == "stop"
        # The prompt never reaches the audit trail.
        assert "Summarize the open issues" not in (
            tmp_path / "audit" / "audit.jsonl"
        ).read_text(encoding="utf-8")
    finally:
        rig.close()


def test_second_fire_reuses_the_dedicated_thread(tmp_path: Path) -> None:
    rig = Rig(tmp_path, scripts=[text_script("first"), text_script("second")])
    try:
        task = rig.create_due_task()
        rig.tick()
        assert rig.proactive.wait_for_idle(5.0) is True
        thread_id = rig.proactive.thread_id_for(task["task_id"])

        rig.tick(DUE + timedelta(seconds=60))
        assert rig.proactive.wait_for_idle(5.0) is True

        assert rig.store.get_thread(thread_id) is not None
        threads, _ = rig.store.list_threads()
        assert [item["id"] for item in threads] == [thread_id]
        messages, _ = rig.store.list_messages(thread_id)
        assert len(messages) == 4
        assert _turns(tmp_path / "chat.db", thread_id) == 2
        assert len(rig.audit.entries(event="schedule_run")) == 2
    finally:
        rig.close()


def test_headless_approval_denies_and_parks_the_schedule(tmp_path: Path) -> None:
    marker = tmp_path / "side-effect.txt"

    def factory(hook: Any) -> Any:
        async def handler(call: Any) -> dict[str, Any]:
            request = ApprovalRequest(
                skill="fs.write",
                tool="fs.write",
                arguments=dict(call.arguments),
                argument_hash="a" * 64,
                effects=("fs:write",),
                scopes={"paths": (str(marker),)},
                manifest_version="1.0",
                requested_at="2026-10-30T12:00:00Z",
                expires_at="2026-10-30T12:10:00Z",
            )
            approved = await hook(request)
            if not approved:
                return {
                    "ok": False,
                    "skill": "fs.write",
                    "tool": "fs.write",
                    "duration_ms": 1,
                    "error": {
                        "code": "approval_denied",
                        "message": "The owner denied this action.",
                        "retryable": False,
                        "user_fixable": False,
                    },
                }
            marker.write_text("ran", encoding="utf-8")
            return {
                "ok": True,
                "skill": "fs.write",
                "tool": "fs.write",
                "duration_ms": 1,
                "output": {"written": str(marker)},
            }

        return CallableToolExecutor(handler)

    rig = Rig(
        tmp_path,
        scripts=[tool_script("fs.write", {"path": str(marker)}), text_script("Paused.")],
        tool_executor_factory=factory,
    )
    try:
        task = rig.create_due_task()
        rig.tick()
        assert rig.proactive.wait_for_idle(5.0) is True

        thread_id = rig.proactive.thread_id_for(task["task_id"])
        cards = rig.store.list_approvals(status="denied", limit=50)
        assert len(cards) == 1
        assert cards[0]["turn_id"] == rig.store.latest_turn(thread_id)["id"]

        assert marker.exists() is False
        parked = rig.schedule_store.read_task(task["task_id"])
        assert parked["state"] == "paused_needs_approval"

        rows = rig.audit.entries(event="schedule_run")
        assert len(rows) == 1
        assert rows[0]["outcome"] == "paused_needs_approval"
        assert rows[0]["extra"]["reason"] == "approval_required"

        # A paused schedule is not runnable: the next tick starts nothing.
        rig.clock.advance(minutes=2)
        rig.scheduler.tick(now=rig.clock())
        assert rig.proactive.wait_for_idle(2.0) is True
        assert _turns(tmp_path / "chat.db", thread_id) == 1
        assert len(rig.audit.entries(event="schedule_run")) == 1
    finally:
        rig.close()


def test_twenty_due_tasks_are_capped_not_queued(tmp_path: Path) -> None:
    provider = GateProvider()
    rig = Rig(
        tmp_path,
        provider=provider,
        config=ProactiveConfig(max_concurrent=1, max_runs_per_hour=100),
    )
    try:
        tasks = [rig.create_due_task(title=f"Task {index}") for index in range(20)]
        report = rig.tick()

        assert report["due"] == 20
        # The first run is held open by the provider while the ticker walks the
        # rest of the due set, so the cap is observed deterministically.
        assert provider.started.wait(5.0) is True
        provider.release.set()
        assert rig.proactive.wait_for_idle(5.0) is True

        rows = rig.audit.entries(event="schedule_run")
        assert len(rows) == 20
        outcomes = sorted(row["outcome"] for row in rows)
        assert outcomes.count("ok") == 1
        assert outcomes.count("skipped") == 19
        skipped = [row for row in rows if row["outcome"] == "skipped"]
        assert {row["extra"]["reason"] for row in skipped} == {"concurrency_cap"}

        # Exactly one turn exists, in the one schedule that was admitted.
        thread_ids = {rig.proactive.thread_id_for(task["task_id"]) for task in tasks}
        assert sum(_turns(tmp_path / "chat.db", thread_id) for thread_id in thread_ids) == 1
    finally:
        rig.close()


def test_spend_cap_refuses_before_any_turn_or_provider_call(tmp_path: Path) -> None:
    usage_path = tmp_path / "usage" / "usage.jsonl"
    usage_path.parent.mkdir(parents=True)
    JsonlUsageStore(usage_path).record(
        UsageRecord(
            turn_id="turn_old",
            session_id="ses_old",
            model=MODEL,
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            estimated_cost_usd=2.0,
            cost_known=True,
            usage_reported=True,
            steps=1,
            tool_calls=0,
            started_at=BASE.isoformat(),
            finished_at=BASE.isoformat(),
            duration_ms=10,
            outcome="stop",
        )
    )
    guard = SpendGuard(
        JsonlUsageStore(usage_path),
        SpendCaps(day_usd=1.0),
        now_fn=lambda: BASE,
    )
    provider = ForbiddenProvider()
    rig = Rig(tmp_path, provider=provider, spend_guard=guard)
    try:
        task = rig.create_due_task()
        rig.tick()
        assert rig.proactive.wait_for_idle(2.0) is True

        assert provider.calls == 0
        assert rig.store.list_threads()[0] == []
        thread_id = rig.proactive.thread_id_for(task["task_id"])
        assert _turns(tmp_path / "chat.db", thread_id) == 0

        rows = rig.audit.entries(event="schedule_run")
        assert len(rows) == 1
        assert rows[0]["outcome"] == "skipped"
        assert rows[0]["extra"]["reason"] == "spend_cap"
        assert rows[0].get("turn_id") is None

        attempted = rig.schedule_store.read_task(task["task_id"])
        assert attempted["skipped_count"] == 1
        assert attempted["state"] in {"active", "pending"}
    finally:
        rig.close()


def test_sse_stream_sees_the_proactive_turn(tmp_path: Path) -> None:
    rig = Rig(tmp_path, scripts=[text_script("Streamed proactively")])
    try:
        task = rig.create_due_task()
        thread_id = rig.proactive.thread_id_for(task["task_id"])
        frames: list[str] = []
        finished = threading.Event()

        def consume() -> None:
            for frame in sse_frames(
                rig.store,
                rig.service.bus,
                thread_id,
                heartbeat=0.05,
                idle=5.0,
                max_events=200,
            ):
                frames.append(frame)
                if "event: turn_finished" in frame:
                    finished.set()
                    return

        consumer = threading.Thread(target=consume, name="proactive-sse", daemon=True)
        consumer.start()
        time.sleep(0.05)  # let the tail attach before the thread exists
        rig.tick()

        assert finished.wait(5.0) is True
        rig.service.bus.close(thread_id)
        consumer.join(2.0)

        text = "".join(frames)
        assert f"event: {TRIGGER_BLOCK_TYPE}" in text
        assert "event: message_created" in text
        assert "event: turn_finished" in text
        assert "Streamed proactively" in text
    finally:
        rig.close()


# ---------------------------------------------------------------------------
# kill -9 mid-run, then a real restart
# ---------------------------------------------------------------------------

_CHILD = r"""
import asyncio, os, signal, time
from chat.server import ServerConfig, build_app
from kernel.provider import StreamEnd, TextDelta, UsageReport

class SlowProvider:
    async def stream(self, request):
        for index in range(1000):
            await asyncio.sleep(0.05)
            yield TextDelta("token-{0} ".format(index))
        yield UsageReport(10, 1000, 1010)
        yield StreamEnd("stop")

app = build_app(ServerConfig.from_env(), provider=SlowProvider())
deadline = time.time() + 15
while time.time() < deadline:
    threads, _ = app.store.list_threads(limit=20)
    for thread in threads:
        if thread["active_turn_id"]:
            print("RUNNING", flush=True)
            time.sleep(0.3)
            os.kill(os.getpid(), signal.SIGKILL)
    time.sleep(0.05)
print("NO_TURN", flush=True)
raise SystemExit(3)
"""


def _server_env(root: Path) -> dict[str, str]:
    return {
        "NEBIUS_API_KEY": "test-api-key-never-printed-12345",
        "NEBIUS_TOKEN_FACTORY_BASE_URL": "http://127.0.0.1:9/v1",
        "KELP_AUTH_TOKEN": "owner-token-1234567890",
        "KELP_DB_PATH": str(root / "db" / "chat.db"),
        "KELP_ARTIFACT_DIR": str(root / "artifacts"),
        "KELP_AUDIT_DIR": str(root / "audit"),
        "KELP_USAGE_DIR": str(root / "usage"),
        "KELP_WORKSPACE_ROOT": str(root / "workspace"),
        "KELP_HOST": "127.0.0.1",
        "KELP_PORT": "0",
    }


class NoopProvider:
    async def stream(self, request: Any) -> Any:  # pragma: no cover
        raise AssertionError("the recovery boot must not start a turn")
        yield  # pragma: no cover


def test_sigkill_mid_run_settles_interrupted_with_one_run_row(tmp_path: Path) -> None:
    env = _server_env(tmp_path)
    workspace = Path(env["KELP_WORKSPACE_ROOT"])
    anchor = datetime.now(timezone.utc) - timedelta(minutes=5)
    seed_store = ScheduleStore(workspace, now=lambda: anchor)
    task = seed_store.create_task(
        title="Nightly sweep",
        prompt="Do the nightly thing",
        expr="* * * * *",
        timezone="UTC",
        now=anchor,
        audit=AuditLog(tmp_path / "seed-audit.jsonl"),
    )

    child_env = {**os.environ, **env}
    child_env["KELP_SCHEDULER_TICK_SECONDS"] = "1"
    completed = subprocess.run(
        [sys.executable, "-c", _CHILD],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=child_env,
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert completed.returncode < 0, (completed.stdout, completed.stderr)
    assert "RUNNING" in completed.stdout

    recovery_env = {**os.environ, **env, "KELP_SCHEDULER_ENABLED": "0"}
    app = build_app(
        ServerConfig.from_env(recovery_env),
        provider=NoopProvider(),
        environ=recovery_env,
    )
    try:
        assert len(app.recovery.closed_turns) == 1
        turn_id = str(app.recovery.closed_turns[0])
        turn = app.store.get_turn(turn_id)
        assert turn is not None
        assert turn["state"] == "finished"
        assert turn["reason"] == "interrupted"

        thread_id = str(turn["thread_id"])
        # One turn, one user message, one assistant message: no duplicate work.
        assert _turns(Path(env["KELP_DB_PATH"]), thread_id) == 1
        messages, _ = app.store.list_messages(thread_id)
        assert len(messages) == 2
        assert [message["role"] for message in messages] == ["user", "assistant"]

        trigger = messages[0]["blocks"][0]
        assert trigger["type"] == TRIGGER_BLOCK_TYPE
        assert trigger["schedule_id"] == task["task_id"]
        run_id = trigger["run_id"]

        rows = [
            row
            for row in app.audit.entries(event="schedule_run")
            if row.get("extra", {}).get("run_id") == run_id
        ]
        assert len(rows) == 1
        assert rows[0]["outcome"] == "interrupted"
        assert rows[0]["turn_id"] == turn_id
        assert rows[0]["extra"]["recovered"] is True
        assert rows[0]["extra"]["reason"] == "server_restart"

        # Nothing is auto-retried: a second boot finds nothing to settle and
        # writes no second row.
        app.close()
        second = build_app(
            ServerConfig.from_env(recovery_env),
            provider=NoopProvider(),
            environ=recovery_env,
        )
        try:
            assert second.recovery.closed_turns == ()
            rows = [
                row
                for row in second.audit.entries(event="schedule_run")
                if row.get("extra", {}).get("run_id") == run_id
            ]
            assert len(rows) == 1
            assert _turns(Path(env["KELP_DB_PATH"]), thread_id) == 1
        finally:
            second.close()
    finally:
        app.close()

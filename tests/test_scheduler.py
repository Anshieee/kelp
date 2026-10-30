"""KEL-23 scheduler store, cron parser, and ticker tests.

Every test is offline and uses an injected clock; there is no real sleep, no
network, and no provider. The DST fixtures are pinned to the 2026 US
transitions (8 March spring forward, 1 November fall back) so they do not
depend on the machine's local time.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from chat.scheduler import (
    CronError,
    ScheduleStore,
    SchedulerError,
    SchedulerRuntime,
    next_run_at,
    next_run_local,
    parse_cron,
    resolve_timezone,
)
from openshell.audit import AuditLog
from skills import SkillContext, SkillRegistry
from skills.builtin.schedule_create import handler as schedule_handler
from skills.errors import SkillHandlerError

BASE = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)  # Friday


class Clock:
    def __init__(self, moment: datetime = BASE):
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **kwargs: float) -> None:
        self.moment = self.moment + timedelta(**kwargs)


def _workspace(tmp_path: Path) -> Path:
    return tmp_path / "workspace"


def _store(tmp_path: Path, clock: Clock | None = None, **kwargs: Any) -> ScheduleStore:
    return ScheduleStore(_workspace(tmp_path), now=clock or Clock(), **kwargs)


def _task_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "title": "Morning brief",
        "prompt": "Summarize open issues; token=TOP-SECRET-VALUE",
        "schedule": {"kind": "cron", "expr": "0 9 * * *"},
        "timezone": "UTC",
        "next_run_at": "2026-10-31T09:00:00Z",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Cron acceptance / rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr",
    [
        "* * * * *",
        "0 9 * * *",
        "30 8 * * 1-5",
        "0 9 * * 7",  # cron Sunday
        "0 9 * * 0",  # cron Sunday
        "0 0 29 2 *",  # leap day
        "0 0 31 * *",  # month end
        "*/15 * * * *",
        "0 0 1 JAN *",
        "0 9 * * MON-FRI",
        "5/10 * * * *",
        "0 0 * * 1,2,3",
    ],
)
def test_cron_fixture_accepts(expr: str) -> None:
    spec = parse_cron(expr)
    assert spec.minutes
    assert spec.hours
    assert spec.days
    assert spec.months
    assert spec.weekdays


@pytest.mark.parametrize(
    ("expr", "code"),
    [
        ("* * * * * *", "cron_field_count"),  # six fields (seconds)
        ("@daily", "cron_shortcut"),
        ("@every 5m", "cron_shortcut"),
        ("* * * *", "cron_field_count"),
        ("* * * * * * *", "cron_field_count"),
        ("60 * * * *", "cron_range"),
        ("* 24 * * *", "cron_range"),
        ("* * 0 * *", "cron_range"),
        ("* * 32 * *", "cron_range"),
        ("* * * 0 *", "cron_range"),
        ("* * * 13 *", "cron_range"),
        ("* * * * 8", "cron_range"),
        ("*/0 * * * *", "cron_step"),
        ("*/x * * * *", "cron_step"),
        ("5-3 * * * *", "cron_range"),
        ("1-2-3 * * * *", "cron_range"),
        ("a * * * *", "cron_syntax"),
        ("? * * * *", "cron_unsupported"),
        ("* * L * *", "cron_unsupported"),
        ("* * * * 1#2", "cron_unsupported"),
    ],
)
def test_cron_fixture_rejects_with_named_error(expr: str, code: str) -> None:
    with pytest.raises(CronError) as info:
        parse_cron(expr)
    assert info.value.code == code


def test_six_field_and_shortcut_messages_are_explicit() -> None:
    with pytest.raises(CronError) as six:
        parse_cron("0 0 9 * * *")
    assert "six-field" in str(six.value)

    with pytest.raises(CronError) as shortcut:
        parse_cron("@hourly")
    assert "shortcut" in str(shortcut.value)


def test_cron_day_of_month_and_weekday_are_or_ed_when_both_restricted() -> None:
    # The 13th OR any Friday, standard Vixie semantics.
    spec = parse_cron("0 0 13 * 5")
    assert spec.date_matches(datetime(2026, 11, 13).date()) is True  # Friday 13th
    assert spec.date_matches(datetime(2026, 11, 6).date()) is True  # Friday
    assert spec.date_matches(datetime(2026, 12, 13).date()) is True  # Sunday 13th
    assert spec.date_matches(datetime(2026, 11, 10).date()) is False


# ---------------------------------------------------------------------------
# Next-run computation, including DST
# ---------------------------------------------------------------------------


def test_next_run_sunday_and_every_minute() -> None:
    sunday = next_run_at("0 9 * * 7", "UTC", after=BASE)
    assert sunday == datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
    assert sunday.weekday() == 6

    minute = next_run_at(
        "* * * * *", "UTC", after=datetime(2026, 10, 30, 12, 0, 30, tzinfo=timezone.utc)
    )
    assert minute == datetime(2026, 10, 30, 12, 1, tzinfo=timezone.utc)


def test_next_run_leap_day_and_month_end() -> None:
    leap = next_run_at(
        "0 0 29 2 *", "UTC", after=datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    assert leap == datetime(2028, 2, 29, 0, 0, tzinfo=timezone.utc)

    month_end = next_run_at(
        "0 0 31 * *", "UTC", after=datetime(2026, 4, 1, tzinfo=timezone.utc)
    )
    assert month_end == datetime(2026, 5, 31, 0, 0, tzinfo=timezone.utc)


def test_next_run_spring_forward_gap_fires_at_first_valid_instant() -> None:
    # 2026-03-08: 02:00 EST jumps to 03:00 EDT. The 02:30 wall time does not
    # exist; the rule is one fire at 03:00 local (07:00Z).
    result = next_run_at(
        "30 2 * * *",
        "America/New_York",
        after=datetime(2026, 3, 8, 5, 0, tzinfo=timezone.utc),
    )
    assert result == datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc)
    assert _local(result, "America/New_York").strftime("%Y-%m-%d %H:%M") == "2026-03-08 03:00"

    # Having fired at the gap's end, the next run is the following day.
    after_fire = next_run_at("30 2 * * *", "America/New_York", after=result)
    assert after_fire == datetime(2026, 3, 9, 6, 30, tzinfo=timezone.utc)


def test_next_run_fall_back_hour_fires_exactly_once() -> None:
    # 2026-11-01: 01:30 occurs twice (05:30Z EDT and 06:30Z EST). Only the
    # first occurrence is a run; the repeated copy is skipped.
    before = next_run_at(
        "30 1 * * *",
        "America/New_York",
        after=datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc),
    )
    assert before == datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)

    after = next_run_at("30 1 * * *", "America/New_York", after=before)
    assert after == datetime(2026, 11, 2, 6, 30, tzinfo=timezone.utc)
    assert after.date() == datetime(2026, 11, 2).date()


def test_next_run_nine_am_across_both_dst_transitions() -> None:
    spring = next_run_at(
        "0 9 * * *",
        "America/New_York",
        after=datetime(2026, 3, 7, 14, 0, tzinfo=timezone.utc),
    )
    assert spring == datetime(2026, 3, 8, 13, 0, tzinfo=timezone.utc)
    assert _local(spring, "America/New_York").hour == 9

    fall = next_run_at(
        "0 9 * * *",
        "America/New_York",
        after=datetime(2026, 10, 31, 13, 0, tzinfo=timezone.utc),
    )
    assert fall == datetime(2026, 11, 1, 14, 0, tzinfo=timezone.utc)
    assert _local(fall, "America/New_York").hour == 9


def test_next_run_local_renders_task_timezone() -> None:
    local = next_run_local(
        "0 9 * * *",
        "America/New_York",
        after=datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc),
    )
    assert local.utcoffset() == timedelta(hours=-4)
    assert local.hour == 9


def test_unknown_timezone_names_the_field() -> None:
    with pytest.raises(SchedulerError) as info:
        resolve_timezone("Mars/Olympus")
    assert info.value.code == "invalid_timezone"
    assert info.value.field == "/timezone"

    with pytest.raises(SchedulerError):
        next_run_at("0 9 * * *", "Mars/Olympus", after=BASE)


def _local(moment: datetime, name: str) -> datetime:
    return moment.astimezone(resolve_timezone(name))


# ---------------------------------------------------------------------------
# Store: atomic writes, validation, quarantine, bounds
# ---------------------------------------------------------------------------


def test_store_create_writes_one_atomic_private_file(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    task = store.create_task(
        title="Morning brief",
        prompt="Summarize open issues",
        expr="0 9 * * *",
        timezone="America/New_York",
        now=BASE,
    )

    path = store.task_path(task["task_id"])
    assert path.exists()
    assert path.name.endswith(".json")
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert not list(store.root.glob(".*.tmp.*"))

    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["task_id"] == task["task_id"]
    assert document["schedule"] == {"kind": "cron", "expr": "0 9 * * *"}
    assert document["timezone"] == "America/New_York"
    assert document["next_run_at"] == "2026-10-30T13:00:00Z"
    assert document["state"] == "active"
    assert document["enabled"] is True


def test_store_enforces_max_tasks(tmp_path: Path) -> None:
    store = _store(tmp_path, max_tasks=2)
    store.create_task(title="one", prompt="p", expr="* * * * *", timezone="UTC", now=BASE)
    store.create_task(title="two", prompt="p", expr="* * * * *", timezone="UTC", now=BASE)
    with pytest.raises(SchedulerError) as info:
        store.create_task(
            title="three", prompt="p", expr="* * * * *", timezone="UTC", now=BASE
        )
    assert info.value.code == "scheduler_full"


def test_corrupt_task_file_is_quarantined_and_boot_continues(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure_root()
    (store.root / "broken.json").write_text("{ this is not JSON", encoding="utf-8")
    (store.root / "missing-prompt.json").write_text(
        json.dumps({"task_id": "task_noprompt", "title": "x"}), encoding="utf-8"
    )
    good = store.create_task(
        title="good", prompt="p", expr="0 9 * * *", timezone="UTC", now=BASE
    )

    loaded = store.load_tasks(now=BASE)
    assert [task["task_id"] for task in loaded] == [good["task_id"]]
    assert (store.root / "broken.json.bad").exists()
    assert (store.root / "missing-prompt.json.bad").exists()
    assert len(store.last_quarantined) == 2


def test_load_task_accepts_minimal_document_with_defaults(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure_root()
    (store.root / "task_minimal.json").write_text(
        json.dumps(
            {
                "task_id": "task_minimal",
                "title": "Minimal",
                "prompt": "Do the thing",
                "schedule": {"kind": "cron", "expr": "* * * * *"},
                "timezone": "UTC",
                "next_run_at": "2026-10-30T12:01:00Z",
            }
        ),
        encoding="utf-8",
    )

    loaded = store.load_tasks(now=BASE)
    assert len(loaded) == 1
    assert loaded[0]["enabled"] is True
    assert loaded[0]["state"] == "active"
    assert loaded[0]["failure_count"] == 0


def test_task_id_must_match_filename_or_the_file_is_quarantined(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure_root()
    canonical = _task_body(task_id="task_canon")
    (store.root / "task_canon.json").write_text(json.dumps(canonical), encoding="utf-8")
    # Sorts before the canonical name: the stray must lose regardless of order.
    (store.root / "aaa_stray.json").write_text(json.dumps(canonical), encoding="utf-8")

    loaded = store.load_tasks(now=BASE)
    assert [task["task_id"] for task in loaded] == ["task_canon"]
    assert (store.root / "task_canon.json").exists()
    assert (store.root / "aaa_stray.json.bad").exists()
    assert (store.root / "aaa_stray.json").exists() is False
    assert store.last_quarantined == ["aaa_stray.json.bad"]


def test_stray_duplicate_never_fires_and_quarantine_does_not_grow(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    task = store.create_task(
        title="canonical", prompt="p", expr="* * * * *", timezone="UTC", now=BASE
    )
    canonical = store.task_path(task["task_id"])
    (store.root / "aaa_stray.json").write_text(
        canonical.read_text(encoding="utf-8"), encoding="utf-8"
    )
    clock.advance(minutes=2)

    fired: list[str] = []
    runtime = SchedulerRuntime(
        store, on_fire=lambda task, when: fired.append(task["task_id"]), now=clock
    )

    first = runtime.tick()
    bad_after_first = sorted(path.name for path in store.root.glob("*.bad"))
    second = runtime.tick()
    third = runtime.tick()

    assert first["quarantined"] == 1
    assert len(fired) == 1  # only the canonical file fired, exactly once
    assert second["fired"] == 0 and third["fired"] == 0
    assert second["quarantined"] == 0 and third["quarantined"] == 0
    assert bad_after_first == ["aaa_stray.json.bad"]
    assert sorted(path.name for path in store.root.glob("*.bad")) == bad_after_first
    assert store.load_tasks(now=clock())[0]["task_id"] == task["task_id"]


def test_store_forces_scheduler_root_0700_without_touching_parent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.root.parent.mkdir(parents=True)
    os.chmod(store.root.parent, 0o755)
    store.root.mkdir()
    os.chmod(store.root, 0o755)

    store.create_task(title="mode", prompt="p", expr="0 9 * * *", timezone="UTC", now=BASE)

    assert store.root.stat().st_mode & 0o777 == 0o700
    assert store.root.parent.stat().st_mode & 0o777 == 0o755


def test_task_file_with_bad_cron_or_timezone_is_quarantined(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure_root()
    for name, expr, zone in (
        ("badcron", "0 0 9 * * *", "UTC"),
        ("badzone", "0 9 * * *", "Mars/Olympus"),
    ):
        (store.root / f"{name}.json").write_text(
            json.dumps(
                _task_body(
                    task_id=f"task_{name}",
                    schedule={"kind": "cron", "expr": expr},
                    timezone=zone,
                )
            ),
            encoding="utf-8",
        )
    assert store.load_tasks(now=BASE) == []
    assert (store.root / "badcron.json.bad").exists()
    assert (store.root / "badzone.json.bad").exists()


# ---------------------------------------------------------------------------
# Handler (approved create)
# ---------------------------------------------------------------------------


async def _approve(_request: Any) -> bool:
    return True


def test_approved_create_writes_file_and_correct_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(schedule_handler, "utc_now", lambda: BASE)
    registry = SkillRegistry.load()
    workspace = _workspace(tmp_path)
    audit = AuditLog(tmp_path / "audit.jsonl")

    result = asyncio.run(
        registry.invoke(
            "schedule.create",
            {
                "title": "Morning brief",
                "prompt": "Summarize open issues; token=TOP-SECRET-VALUE",
                "schedule": {"kind": "cron", "expr": "0 9 * * *"},
                "timezone": "America/New_York",
            },
            approval=_approve,
            workspace_root=workspace,
            audit=audit,
        )
    )

    assert result["ok"] is True, result
    output = result["output"]
    assert output["status"] == "scheduled"
    assert output["next_run_at"] == "2026-10-30T13:00:00Z"

    files = list((workspace / "var" / "scheduler").glob("*.json"))
    assert len(files) == 1
    document = json.loads(files[0].read_text(encoding="utf-8"))
    assert document["task_id"] == output["task_id"]
    assert document["next_run_at"] == output["next_run_at"]

    rows = audit.entries(event="schedule_created")
    assert len(rows) == 1
    blob = json.dumps(rows[0])
    assert "TOP-SECRET-VALUE" not in blob
    assert "Summarize open issues" not in blob
    assert rows[0]["extra"]["schedule_id"] == output["task_id"]


def test_direct_handler_create_writes_local_audit_and_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(schedule_handler, "utc_now", lambda: BASE)
    workspace = _workspace(tmp_path)
    output = asyncio.run(
        schedule_handler.run(
            {
                "title": "Direct",
                "prompt": "secret prompt",
                "schedule": {"kind": "cron", "expr": "* * * * *"},
                "timezone": "UTC",
            },
            SkillContext("schedule.create", workspace),
        )
    )
    assert output["status"] == "scheduled"
    assert list((workspace / "var" / "scheduler").glob("*.json"))
    audit_path = workspace / "var" / "scheduler" / "audit.jsonl"
    rows = AuditLog(audit_path).entries(event="schedule_created")
    assert len(rows) == 1
    assert "secret prompt" not in json.dumps(rows[0])


@pytest.mark.parametrize(
    ("arguments", "field"),
    [
        (
            {
                "title": "x",
                "prompt": "p",
                "schedule": {"kind": "cron", "expr": "0 0 9 * * *"},
                "timezone": "UTC",
            },
            "/schedule/expr",
        ),
        (
            {
                "title": "x",
                "prompt": "p",
                "schedule": {"kind": "cron", "expr": "0 9 * * *"},
                "timezone": "Mars/Olympus",
            },
            "/timezone",
        ),
        (
            {
                "title": "x" * 121,
                "prompt": "p",
                "schedule": {"kind": "cron", "expr": "0 9 * * *"},
                "timezone": "UTC",
            },
            "/title",
        ),
        (
            {
                "title": "x",
                "prompt": "p" * 2001,
                "schedule": {"kind": "cron", "expr": "0 9 * * *"},
                "timezone": "UTC",
            },
            "/prompt",
        ),
        (
            {
                "title": "x",
                "prompt": "p",
                "schedule": {"kind": "cron", "expr": "0 9 * * *"},
                "timezone": "UTC",
                "surprise": True,
            },
            "/surprise",
        ),
    ],
)
def test_handler_rejects_bad_arguments_with_field(
    tmp_path: Path, arguments: dict[str, Any], field: str
) -> None:
    with pytest.raises(SkillHandlerError) as info:
        asyncio.run(
            schedule_handler.run(arguments, SkillContext("schedule.create", _workspace(tmp_path)))
        )
    assert info.value.code == "invalid_arguments"
    assert info.value.details["field"] == field


def test_handler_start_at_is_the_first_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schedule_handler, "utc_now", lambda: BASE)
    start = BASE + timedelta(hours=2)
    output = asyncio.run(
        schedule_handler.run(
            {
                "title": "Later",
                "prompt": "p",
                "schedule": {"kind": "cron", "expr": "* * * * *"},
                "timezone": "UTC",
                "start_at": start.isoformat(),
            },
            SkillContext("schedule.create", _workspace(tmp_path)),
        )
    )
    # start_at lands on a whole minute, so it is included rather than skipped.
    assert output["next_run_at"] == "2026-10-30T14:00:00Z"


# ---------------------------------------------------------------------------
# Ticker: one fire, catch-up, missed, restart, audit
# ---------------------------------------------------------------------------


def test_double_tick_same_minute_runs_once(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    task = store.create_task(
        title="tick", prompt="p", expr="* * * * *", timezone="UTC", now=BASE
    )
    clock.advance(minutes=2)
    fired: list[tuple[str, datetime]] = []
    runtime = SchedulerRuntime(
        store, on_fire=lambda task, when: fired.append((task["task_id"], when)), now=clock
    )

    first = runtime.tick()
    second = runtime.tick()
    assert first["fired"] == 1
    assert len(fired) == 1
    assert fired[0][0] == task["task_id"]
    assert fired[0][1] == datetime(2026, 10, 30, 12, 1, tzinfo=timezone.utc)
    assert second["fired"] == 0
    assert second["due"] == 0


def test_many_missed_windows_catch_up_once(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    store.create_task(
        title="catch up", prompt="p", expr="* * * * *", timezone="UTC", now=BASE
    )
    clock.advance(minutes=40)  # 39 missed windows, within the one-hour grace
    fired: list[datetime] = []
    runtime = SchedulerRuntime(store, on_fire=lambda task, when: fired.append(when), now=clock)

    report = runtime.tick()
    assert report["fired"] == 1
    assert report["missed"] == 0
    assert fired == [datetime(2026, 10, 30, 12, 1, tzinfo=timezone.utc)]

    task = store.load_tasks(now=clock())[0]
    assert task["next_run_at"] == "2026-10-30T12:41:00Z"
    assert runtime.tick()["fired"] == 0


def test_window_older_than_grace_is_recorded_missed_not_backfilled(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    store.create_task(title="old", prompt="p", expr="* * * * *", timezone="UTC", now=BASE)
    clock.advance(hours=2)
    fired: list[datetime] = []
    runtime = SchedulerRuntime(store, on_fire=lambda task, when: fired.append(when), now=clock)

    report = runtime.tick()
    assert report["fired"] == 0
    assert report["missed"] == 1
    assert fired == []
    task = store.load_tasks(now=clock())[0]
    assert task["skipped_count"] == 1
    assert task["last_skipped_for"] == "2026-10-30T12:01:00Z"
    assert task["next_run_at"] == "2026-10-30T14:01:00Z"

    second = runtime.tick()
    assert second["missed"] == 0
    assert store.load_tasks(now=clock())[0]["skipped_count"] == 1


def test_restart_after_fire_does_not_double_run(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    store.create_task(title="restart", prompt="p", expr="* * * * *", timezone="UTC", now=BASE)
    clock.advance(minutes=2)
    first = SchedulerRuntime(store, on_fire=lambda task, when: None, now=clock)
    assert first.tick()["fired"] == 1

    # A second process starts in the same minute and sees the persisted claim.
    second = SchedulerRuntime(store, on_fire=lambda task, when: None, now=clock)
    assert second.tick()["fired"] == 0

    # Advancing to the next minute fires exactly once, not twice.
    clock.advance(minutes=1)
    assert second.tick()["fired"] == 1


def test_failed_run_claim_prevents_restart_double_fire(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    store.create_task(title="crash", prompt="p", expr="* * * * *", timezone="UTC", now=BASE)
    clock.advance(minutes=2)

    def fail(_task: Any, _when: datetime) -> None:
        raise RuntimeError("provider down")

    runtime = SchedulerRuntime(store, on_fire=fail, now=clock)
    report = runtime.tick()
    assert report["fired"] == 1
    assert report["errors"] == 1
    task = store.load_tasks(now=clock())[0]
    assert task["failure_count"] == 1
    assert task["next_run_at"] == "2026-10-30T12:03:00Z"

    restarted = SchedulerRuntime(store, on_fire=fail, now=clock)
    assert restarted.tick()["fired"] == 0


def test_each_fire_writes_one_content_free_schedule_run_row(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    audit = AuditLog(tmp_path / "audit.jsonl")
    store.create_task(
        title="audit",
        prompt="the prompt contains TOP-SECRET-VALUE",
        expr="* * * * *",
        timezone="UTC",
        now=BASE,
        audit=audit,
    )
    clock.advance(minutes=2)
    runtime = SchedulerRuntime(store, on_fire=lambda task, when: None, now=clock, audit=audit)
    runtime.tick()

    created = audit.entries(event="schedule_created")
    runs = audit.entries(event="schedule_run")
    assert len(created) == 1
    assert len(runs) == 1
    blob = json.dumps(created + runs)
    assert "TOP-SECRET-VALUE" not in blob
    assert "the prompt contains" not in blob
    assert runs[0]["extra"]["scheduled_for"] == "2026-10-30T12:01:00Z"
    assert runs[0]["extra"]["run_id"].startswith("run_")


# ---------------------------------------------------------------------------
# Lifecycle / environment
# ---------------------------------------------------------------------------


def test_disabled_env_makes_tick_and_start_no_ops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KELP_SCHEDULER_ENABLED", "0")
    clock = Clock()
    store = _store(tmp_path, clock)
    store.create_task(title="off", prompt="p", expr="* * * * *", timezone="UTC", now=BASE)
    clock.advance(minutes=2)
    fired: list[datetime] = []
    runtime = SchedulerRuntime(store, on_fire=lambda task, when: fired.append(when), now=clock)

    assert runtime.enabled is False
    report = runtime.tick()
    assert report["enabled"] is False
    assert report["fired"] == 0
    runtime.start()
    assert runtime.running is False
    runtime.stop()
    assert fired == []


def test_start_stop_uses_no_sleeps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    store.create_task(title="loop", prompt="p", expr="* * * * *", timezone="UTC", now=BASE)
    monkeypatch.setattr(time, "sleep", lambda _seconds: (_ for _ in ()).throw(AssertionError("sleep")))

    runtime = SchedulerRuntime(store, on_fire=lambda task, when: None, now=clock)
    started = time.perf_counter()
    runtime.start()
    runtime.stop()
    elapsed = time.perf_counter() - started
    # The loop ticked once, then stopped on the event; no polling sleep.
    assert elapsed < 0.5


def test_start_at_and_end_at_bound_the_window(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    audit = AuditLog(tmp_path / "audit.jsonl")
    start = BASE + timedelta(hours=1)
    end = BASE + timedelta(hours=3)
    task = store.create_task(
        title="window",
        prompt="p",
        expr="* * * * *",
        timezone="UTC",
        start_at=start,
        end_at=end,
        now=BASE,
        audit=audit,
    )
    assert task["next_run_at"] == "2026-10-30T13:00:00Z"

    clock.moment = end + timedelta(minutes=1)
    fired: list[datetime] = []
    runtime = SchedulerRuntime(
        store, on_fire=lambda task, when: fired.append(when), now=clock, audit=audit
    )
    report = runtime.tick()
    assert report["fired"] == 0
    assert fired == []
    assert report["completed"] == 1
    assert report["missed"] == 1
    assert store.load_tasks(now=clock())[0]["state"] == "completed"

    # An expired window is a missed run, not a silent completion.
    runs = audit.entries(event="schedule_run")
    assert len(runs) == 1
    assert runs[0]["outcome"] == "missed"
    assert runs[0]["extra"]["scheduled_for"] == "2026-10-30T13:00:00Z"
    assert report["runs"][0]["outcome"] == "missed"

    # The completed state means the expired window is recorded once, not per tick.
    assert runtime.tick()["missed"] == 0
    assert len(audit.entries(event="schedule_run")) == 1


def test_end_before_first_run_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(SchedulerError) as info:
        store.create_task(
            title="impossible",
            prompt="p",
            expr="0 9 * * *",
            timezone="UTC",
            start_at=BASE,
            end_at=BASE + timedelta(minutes=30),
            now=BASE,
        )
    assert info.value.field == "/end_at"

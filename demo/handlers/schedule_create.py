"""Fixture ``schedule.create`` handler.

The only write in the demo, and the one the approval gate exists for. It
appends one JSON line under ``${WORKSPACE}/var/scheduler/tasks.jsonl`` — the
path the manifest scopes to — and returns the id and next run time.

The cron evaluator is deliberately small: the demo uses five-field expressions
with ``*``, integers, ranges, lists, and ``*/n`` steps. It is enough to prove
the write happened and to show a real next run time.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from skills.context import SkillContext
from skills.errors import SkillHandlerError

__all__ = ["list_tasks", "next_run_at", "run", "tasks_path"]

_FIELD_RE = re.compile(r"^[0-9*/,\-]+$")
# Cron weekdays run 0-7 (both 0 and 7 are Sunday); next_run_at folds 7 to 0.
_LIMITS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_MAX_DAYS = 366


def tasks_path(workspace_root: str | Path) -> Path:
    return Path(workspace_root) / "var" / "scheduler" / "tasks.jsonl"


def _field_values(spec: str, low: int, high: int) -> set[int]:
    if not _FIELD_RE.match(spec):
        raise ValueError(f"unsupported cron field {spec!r}")
    values: set[int] = set()
    for part in spec.split(","):
        step = 1
        base, _, step_text = part.partition("/")
        if step_text:
            step = int(step_text)
            if step < 1:
                raise ValueError(f"invalid cron step {part!r}")
        if base == "*":
            start, end = low, high
        elif "-" in base:
            start_text, _, end_text = base.partition("-")
            start, end = int(start_text), int(end_text)
        else:
            start = end = int(base)
            if step_text:  # "5/10" means every 10 from 5 to the field maximum
                end = high
        if start < low or end > high or start > end:
            raise ValueError(f"cron field {part!r} is out of range {low}-{high}")
        values.update(range(start, end + 1, step))
    return values


def _matches(moment: datetime, fields: tuple[set[int], ...]) -> bool:
    minutes, hours, days, months, weekdays = fields
    # cron: 0 and 7 are both Sunday; Python: Monday is 0.
    weekday = (moment.weekday() + 1) % 7
    return (
        moment.minute in minutes
        and moment.hour in hours
        and moment.day in days
        and moment.month in months
        and weekday in weekdays
    )


def next_run_at(expr: str, timezone_name: str, *, after: datetime | None = None) -> datetime:
    fields = tuple(
        _field_values(spec, low, high)
        for spec, (low, high) in zip(expr.split(), _LIMITS)
    )
    if len(fields) != 5:  # pragma: no cover - manifest pattern already rejects
        raise ValueError("a cron expression has five fields")
    # Cron allows 7 for Sunday; Python's weekday numbering stops at 6.
    weekdays = fields[-1]
    if 7 in weekdays:
        fields = fields[:-1] + ((weekdays - {7}) | {0},)
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone {timezone_name!r}") from exc
    moment = (after or datetime.now(timezone.utc)).astimezone(zone)
    moment = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = moment + timedelta(days=_MAX_DAYS)
    while moment < limit:
        if _matches(moment, fields):
            return moment
        moment += timedelta(minutes=1)
    raise ValueError("cron expression has no run within a year")


async def run(arguments: Mapping[str, Any], context: SkillContext) -> Mapping[str, Any]:
    schedule = arguments["schedule"]
    expr = str(schedule["expr"])
    timezone_name = str(arguments["timezone"])
    try:
        next_run = next_run_at(expr, timezone_name)
    except ValueError as exc:
        raise SkillHandlerError(
            "handler_error",
            str(exc),
            retryable=False,
            user_fixable=True,
            details={"schedule": expr},
        ) from exc

    task = {
        "task_id": f"task_{uuid.uuid4().hex[:12]}",
        "title": str(arguments["title"]),
        "prompt": str(arguments["prompt"]),
        "schedule": {"kind": str(schedule["kind"]), "expr": expr},
        "timezone": timezone_name,
        "status": "scheduled",
        "next_run_at": next_run.isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    path = tasks_path(context.workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:  # pragma: no cover - platform dependent
        pass
    line = json.dumps(task, sort_keys=True, separators=(",", ":")) + "\n"
    is_new = not path.exists()
    with path.open("a", encoding="utf-8") as handle:
        if is_new:
            try:
                os.chmod(path, 0o600)
            except OSError:  # pragma: no cover - platform dependent
                pass
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    return {
        "task_id": task["task_id"],
        "status": "scheduled",
        "next_run_at": task["next_run_at"],
    }


def list_tasks(workspace_root: str | Path) -> list[dict[str, Any]]:
    path = tasks_path(workspace_root)
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

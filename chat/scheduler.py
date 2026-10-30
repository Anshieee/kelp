"""Durable scheduler store and the in-process cron ticker (KEL-23, M4-A).

The scheduler is deliberately small and local. One owner, one process, one
directory:

* **Task files** live under ``${WORKSPACE}/var/scheduler/<task_id>.json``. Each
  file is one task and is replaced atomically (temporary file in the same
  directory, ``fsync``, ``os.replace``), so a reader never sees a half-written
  document and a crash leaves either the old task or the new one. Files are
  created ``0600`` inside a ``0700`` directory.
* **Cron** is the standard five-field form (minute, hour, day-of-month, month,
  day-of-week) evaluated with :mod:`zoneinfo`. Six-field (seconds) expressions,
  ``@daily``-style shortcuts, and Quartz modifiers are rejected with a named
  :class:`CronError`; they are never silently approximated.
* **DST** is resolved on the wall clock in the task's timezone. A wall time
  inside a spring-forward gap fires once at the first valid instant after the
  gap (for ``02:30`` in ``America/New_York`` on the spring transition, that is
  ``03:00`` local). A wall time that occurs twice in the fall-back hour fires
  exactly once, on the first occurrence; the repeated copy is skipped.
* **Catch-up** is bounded. A run whose scheduled time is no more than
  :data:`DEFAULT_GRACE_SECONDS` late fires once; the next run is then placed in
  the future, so a long downtime never produces a burst. A run older than the
  grace window is recorded as ``missed`` and skipped. A window that closes
  (``end_at``) before its scheduled run is likewise recorded ``missed`` and the
  task becomes ``completed``. Either way at most one catch-up happens per task
  per tick.
* **Ownership** mirrors :class:`chat.retention.RetentionMaintenance`:
  :meth:`SchedulerRuntime.start` runs a daemon thread and
  :meth:`SchedulerRuntime.stop` joins it. The server calls ``start`` after boot
  recovery has settled interrupted turns and calls ``stop`` before the chat
  store is closed. ``KELP_SCHEDULER_ENABLED=0`` makes both a no-op and keeps
  ``tick`` from touching files, which is what tests and the offline demo use.

The module is stdlib-only. It does not import the chat store, the skill loader,
or a provider, so the skill handler can share the cron parser without a cycle.

Audit rows are content-free. :class:`SchedulerStore` writes one
``schedule_created`` row per created task and :class:`SchedulerRuntime` writes
one ``schedule_run`` row per fire, missed window, or expired window. Neither row
contains the task title or prompt; only ids, the cron expression, the timezone,
timestamps, outcome, duration, and cost class.

**Audit sink.** The server passes its retained :class:`openshell.audit.AuditLog`
(``KELP_AUDIT_DIR/audit.jsonl``) into both the store and the runtime, and that
file is rotated by :class:`chat.retention.RetentionMaintenance`. A direct store
or handler call with no server trail falls back to
``${WORKSPACE}/var/scheduler/audit.jsonl``. That fallback is intentionally a
local sink, not a second row in :data:`chat.retention.RETENTION_ROWS`: it exists
only for callers that bypass the server (tests, embedders, direct skill calls),
it stays inside the declared ``var/scheduler`` write scope, and it is discarded
with the workspace. Production rows therefore live in the retained audit log,
and the local file never becomes a second, unrotated copy of them.
"""

from __future__ import annotations

import asyncio
import fcntl
import inspect
import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time as wall_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from openshell.audit import AuditLog

from .logging import log

__all__ = [
    "Cron",
    "CronError",
    "CronSpec",
    "DEFAULT_GRACE_SECONDS",
    "DEFAULT_MAX_TASKS",
    "DEFAULT_TICK_SECONDS",
    "ENABLED_ENV",
    "MAX_EXPR_LENGTH",
    "MAX_PROMPT_LENGTH",
    "MAX_SKIPS",
    "MAX_TITLE_LENGTH",
    "RUNNABLE_STATES",
    "STATES",
    "ScheduleStore",
    "Scheduler",
    "SchedulerError",
    "SchedulerRuntime",
    "SchedulerStore",
    "ScheduledTask",
    "create_task",
    "load_tasks",
    "next_run_at",
    "next_run_local",
    "parse_cron",
    "resolve_timezone",
    "utc_now",
]

# ---------------------------------------------------------------------------
# Bounds and configuration
# ---------------------------------------------------------------------------

#: A tick runs at most this often. The loop is cheap: one directory scan and a
#: comparison per task, no provider call and no sleep between tasks.
DEFAULT_TICK_SECONDS = 30
#: A due task up to this late fires once as a catch-up. Anything older is
#: recorded ``missed`` instead of backfilled.
DEFAULT_GRACE_SECONDS = 60 * 60
#: Hard ceiling on stored tasks. Creation beyond it fails; loading more than it
#: loads the first names in order and reports the rest as overflow.
DEFAULT_MAX_TASKS = 200
#: Per-task retention is not this module's job, but a corrupt or hostile file
#: must not make one tick unbounded. These cap the numbers written to disk.
MAX_SKIPS = 1_000
#: Manifest limits, repeated here so the store rejects a hand-written file too.
MAX_TITLE_LENGTH = 120
MAX_PROMPT_LENGTH = 2_000
MAX_EXPR_LENGTH = 128

ENABLED_ENV = "KELP_SCHEDULER_ENABLED"
TICK_ENV = "KELP_SCHEDULER_TICK_SECONDS"
GRACE_ENV = "KELP_SCHEDULER_GRACE_SECONDS"
MAX_TASKS_ENV = "KELP_SCHEDULER_MAX_TASKS"

STATES = frozenset({"pending", "active", "paused_needs_approval", "disabled", "completed"})
#: States the ticker may dispatch. ``paused_needs_approval`` is owned by the
#: approval flow; ``disabled`` and ``completed`` are terminal until changed.
RUNNABLE_STATES = frozenset({"pending", "active"})

_SCHEMA_VERSION = 1
_MAX_HORIZON_DAYS = 366 * 8
_TASK_ID_RE = re.compile(r"^task_[A-Za-z0-9_-]{1,64}$")
_TICK_LOCK_NAME = ".tick.lock"

_MONTH_NAMES = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}
_WEEKDAY_NAMES = {
    "SUN": 0,
    "MON": 1,
    "TUE": 2,
    "WED": 3,
    "THU": 4,
    "FRI": 5,
    "SAT": 6,
}
_FIELD_RE = re.compile(r"^[0-9A-Za-z*/,\-]+$")


class SchedulerError(ValueError):
    """A named scheduler failure with a stable code and JSON-Pointer field."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        details: Mapping[str, Any] | None = None,
    ):
        self.code = code
        self.field = field or ""
        self.details = dict(details) if details else None
        super().__init__(message)

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else self.code


class CronError(SchedulerError):
    """A five-field cron expression that cannot be evaluated as written."""


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    """Return the current instant. Tests monkeypatch or inject another clock."""

    return datetime.now(timezone.utc)


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    text = _as_utc(moment).isoformat(timespec="seconds")
    return text[:-6] + "Z" if text.endswith("+00:00") else text


def _parse_iso(value: Any, *, field: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_utc(value)
    if not isinstance(value, str) or not value.strip():
        raise SchedulerError("invalid_task", f"{field} must be an ISO-8601 timestamp.", field=field)
    text = value.strip()
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise SchedulerError(
            "invalid_task", f"{field} must be an ISO-8601 timestamp.", field=field
        ) from None
    return _as_utc(moment)


def resolve_timezone(name: Any) -> ZoneInfo:
    """Load an IANA timezone or raise an error naming the field.

    ``ZoneInfo`` is the only source of truth: a name that the host cannot load
    is rejected at create time instead of being silently treated as UTC.
    """

    if not isinstance(name, str) or not name.strip():
        raise SchedulerError(
            "invalid_timezone", "timezone must be a non-empty IANA name.", field="/timezone"
        )
    candidate = name.strip()
    if len(candidate) > 64 or any(character.isspace() for character in candidate):
        raise SchedulerError(
            "invalid_timezone",
            f"timezone {candidate!r} is not a valid IANA name.",
            field="/timezone",
        )
    try:
        return ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise SchedulerError(
            "invalid_timezone",
            f"unknown timezone {candidate!r}; use an IANA name such as America/New_York.",
            field="/timezone",
        ) from None


# ---------------------------------------------------------------------------
# Five-field cron
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CronSpec:
    """A parsed five-field expression with standard cron day semantics."""

    expr: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday, as in cron
    dom_wildcard: bool
    dow_wildcard: bool

    def date_matches(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        day_ok = day.day in self.days
        # Python: Monday is 0; cron: Sunday is 0. ``7`` is normalised at parse.
        weekday_ok = ((day.weekday() + 1) % 7) in self.weekdays
        if self.dom_wildcard and self.dow_wildcard:
            return True
        if self.dom_wildcard:
            return weekday_ok
        if self.dow_wildcard:
            return day_ok
        # Vixie cron: two restricted day fields are OR-ed, not AND-ed.
        return day_ok or weekday_ok


def _cron_value(
    token: str, low: int, high: int, names: Mapping[str, int] | None, field: str
) -> int:
    if token.isdigit():
        return int(token, 10)
    if names is not None:
        mapped = names.get(token.upper())
        if mapped is not None:
            return mapped
    raise CronError(
        "cron_syntax", f"{field} value {token!r} is not valid.", field=field
    )


def _parse_field(
    spec: str,
    low: int,
    high: int,
    *,
    field: str,
    names: Mapping[str, int] | None = None,
    normalize: Callable[[int], int] | None = None,
) -> frozenset[int]:
    text = spec.strip()
    if not text:
        raise CronError("cron_syntax", f"{field} field is empty.", field=field)
    if any(character in text for character in "?LW#"):
        raise CronError(
            "cron_unsupported",
            f"{field} field {text!r} uses a non-standard modifier.",
            field=field,
        )
    if not _FIELD_RE.fullmatch(text):
        raise CronError(
            "cron_syntax", f"{field} field {text!r} contains invalid characters.", field=field
        )

    values: set[int] = set()
    for part in text.split(","):
        if not part:
            raise CronError("cron_syntax", f"{field} field has an empty list item.", field=field)
        base, separator, step_text = part.partition("/")
        if separator:
            if "/" in step_text or not step_text.isdigit():
                raise CronError(
                    "cron_step", f"{field} step in {part!r} must be a positive integer.", field=field
                )
            step = int(step_text, 10)
            if step < 1:
                raise CronError(
                    "cron_step", f"{field} step in {part!r} must be at least 1.", field=field
                )
        else:
            step = 1

        if base == "*":
            start, end = low, high
        elif "-" in base:
            if base.count("-") != 1:
                raise CronError("cron_range", f"{field} range {base!r} is malformed.", field=field)
            start_text, _, end_text = base.partition("-")
            start = _cron_value(start_text, low, high, names, field)
            end = _cron_value(end_text, low, high, names, field)
            if start > end:
                raise CronError(
                    "cron_range", f"{field} range {base!r} is backwards.", field=field
                )
        else:
            start = _cron_value(base, low, high, names, field)
            # ``5/10`` means every ten starting at five up to the field max.
            end = high if separator else start

        if start < low or end > high:
            raise CronError(
                "cron_range",
                f"{field} field {part!r} is outside {low}-{high}.",
                field=field,
            )
        values.update(range(start, end + 1, step))

    if normalize is not None:
        values = {normalize(value) for value in values}
    if not values:
        raise CronError("cron_syntax", f"{field} field {text!r} selects no values.", field=field)
    return frozenset(values)


def parse_cron(expr: Any) -> CronSpec:
    """Parse exactly five cron fields; reject shortcuts and seconds explicitly."""

    if not isinstance(expr, str):
        raise CronError("cron_syntax", "cron expression must be a string.", field="/schedule/expr")
    text = expr.strip()
    if not text:
        raise CronError("cron_syntax", "cron expression is empty.", field="/schedule/expr")
    if len(text) > MAX_EXPR_LENGTH:
        raise CronError(
            "cron_syntax",
            f"cron expression is longer than {MAX_EXPR_LENGTH} characters.",
            field="/schedule/expr",
        )
    if text.startswith("@"):
        raise CronError(
            "cron_shortcut",
            f"cron shortcut {text!r} is not supported; use five fields.",
            field="/schedule/expr",
        )
    fields = text.split()
    if len(fields) != 5:
        if len(fields) == 6:
            message = (
                "cron expressions have exactly five fields; six-field (seconds) "
                "expressions are not supported."
            )
        else:
            message = f"cron expressions have exactly five fields, not {len(fields)}."
        raise CronError("cron_field_count", message, field="/schedule/expr")

    minute_text, hour_text, dom_text, month_text, dow_text = fields
    minutes = _parse_field(minute_text, 0, 59, field="/schedule/expr/minute")
    hours = _parse_field(hour_text, 0, 23, field="/schedule/expr/hour")
    days = _parse_field(dom_text, 1, 31, field="/schedule/expr/day_of_month")
    months = _parse_field(
        month_text, 1, 12, field="/schedule/expr/month", names=_MONTH_NAMES
    )
    weekdays = _parse_field(
        dow_text,
        0,
        7,
        field="/schedule/expr/day_of_week",
        names=_WEEKDAY_NAMES,
        # Cron accepts 7 as Sunday; normalise it so comparisons use 0..6.
        normalize=lambda value: 0 if value == 7 else value,
    )
    return CronSpec(
        expr=text,
        minutes=minutes,
        hours=hours,
        days=days,
        months=months,
        weekdays=weekdays,
        dom_wildcard=dom_text in {"*", "*/1"},
        dow_wildcard=dow_text in {"*", "*/1"},
    )


def _round_trips(utc: datetime, naive: datetime, zone: ZoneInfo) -> bool:
    return utc.astimezone(zone).replace(tzinfo=None) == naive


def _first_instant_after_gap(
    naive: datetime, zone: ZoneInfo, first: datetime, second: datetime
) -> datetime:
    """Return the first real instant whose local wall time is at/after ``naive``.

    ``first`` and ``second`` are the two interpretations of a nonexistent wall
    time (fold 0 and fold 1). They straddle the gap, so a one-second bisection
    finds the transition without relying on private ``zoneinfo`` internals.
    """

    low, high = sorted((first, second))
    if low > high:  # pragma: no cover - defensive
        low, high = high, low
    # A gap is normally one hour; 48 h covers civil-time date-line changes too.
    if high - low > timedelta(days=2):
        high = low + timedelta(days=2)
    while high - low > timedelta(seconds=1):
        midpoint = low + (high - low) / 2
        midpoint = midpoint.replace(microsecond=0)
        if midpoint <= low:
            break
        wall = midpoint.astimezone(zone).replace(tzinfo=None)
        if wall >= naive:
            high = midpoint
        else:
            low = midpoint
    candidate = high.replace(microsecond=0)
    if candidate.astimezone(zone).replace(tzinfo=None) < naive:
        candidate += timedelta(seconds=1)
    return candidate


def _resolve_wall(naive: datetime, zone: ZoneInfo) -> datetime:
    """Resolve a local wall time to one UTC instant using the DST rules above."""

    aware0 = naive.replace(tzinfo=zone, fold=0)
    utc0 = aware0.astimezone(timezone.utc)
    aware1 = naive.replace(tzinfo=zone, fold=1)
    utc1 = aware1.astimezone(timezone.utc)

    valid0 = _round_trips(utc0, naive, zone)
    valid1 = _round_trips(utc1, naive, zone)
    if valid0 and valid1:
        # Ambiguous (fall back): fire on the first occurrence only.
        return min(utc0, utc1)
    if valid0:
        return utc0
    if valid1:
        return utc1
    # Nonexistent (spring forward): fire once at the first valid post-gap instant.
    return _first_instant_after_gap(naive, zone, utc0, utc1)


def _next_run(spec: CronSpec, zone: ZoneInfo, after: datetime) -> datetime:
    local_after = after.astimezone(zone)
    start_naive = local_after.replace(tzinfo=None)
    start_day = start_naive.date()
    horizon = start_day + timedelta(days=_MAX_HORIZON_DAYS)
    hours = sorted(spec.hours)
    minutes = sorted(spec.minutes)

    day = start_day
    while day <= horizon:
        if spec.date_matches(day):
            for hour in hours:
                for minute in minutes:
                    naive = datetime.combine(day, wall_time(hour, minute))
                    if naive < start_naive:
                        continue
                    candidate = _resolve_wall(naive, zone)
                    if candidate > after:
                        return candidate
        day += timedelta(days=1)

    raise CronError(
        "cron_no_match",
        f"cron expression {spec.expr!r} has no run in the next "
        f"{_MAX_HORIZON_DAYS // 365} years.",
        field="/schedule/expr",
    )


def next_run_at(
    expr: str, timezone_name: str, *, after: datetime | None = None
) -> datetime:
    """Return the first UTC instant strictly after ``after`` that matches.

    Pure and side-effect free: no store, no ticker, no clock lookup when
    ``after`` is supplied. This is the function the handler uses at create
    time and the ticker uses after every fire, so the two cannot disagree.
    """

    spec = parse_cron(expr)
    zone = resolve_timezone(timezone_name)
    moment = _as_utc(after) if after is not None else utc_now()
    return _next_run(spec, zone, moment)


def next_run_local(
    expr: str, timezone_name: str, *, after: datetime | None = None
) -> datetime:
    """Same instant as :func:`next_run_at`, rendered in the task timezone."""

    moment = next_run_at(expr, timezone_name, after=after)
    return moment.astimezone(resolve_timezone(timezone_name))


# ---------------------------------------------------------------------------
# Task documents
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScheduledTask:
    """A validated task document. ``to_dict`` returns the on-disk shape."""

    task_id: str
    title: str
    prompt: str
    expr: str
    timezone_name: str
    next_run_at: str
    enabled: bool = True
    state: str = "active"
    last_run_at: str | None = None
    last_scheduled_for: str | None = None
    last_skipped_for: str | None = None
    failure_count: int = 0
    skipped_count: int = 0
    created_by: str = "agent"
    created_at: str | None = None
    updated_at: str | None = None
    start_at: str | None = None
    end_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "task_id": self.task_id,
            "title": self.title,
            "prompt": self.prompt,
            "schedule": {"kind": "cron", "expr": self.expr},
            "timezone": self.timezone_name,
            "enabled": self.enabled,
            "state": self.state,
            "next_run_at": self.next_run_at,
            "last_run_at": self.last_run_at,
            "last_scheduled_for": self.last_scheduled_for,
            "last_skipped_for": self.last_skipped_for,
            "failure_count": self.failure_count,
            "skipped_count": self.skipped_count,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "start_at": self.start_at,
            "end_at": self.end_at,
        }


def _require_text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value:
        raise SchedulerError(
            "invalid_task", f"{name} must be a non-empty string.", field=f"/{name}"
        )
    if len(value) > maximum:
        raise SchedulerError(
            "invalid_task",
            f"{name} must be at most {maximum} characters.",
            field=f"/{name}",
        )
    if "\x00" in value:
        raise SchedulerError("invalid_task", f"{name} contains a NUL byte.", field=f"/{name}")
    return value


def _coerce_task(data: Any, *, now: datetime | None = None) -> ScheduledTask:
    """Validate and normalise one task document, filling optional defaults."""

    if not isinstance(data, Mapping):
        raise SchedulerError("invalid_task", "a task file must contain a JSON object.")

    task_id = data.get("task_id")
    if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
        raise SchedulerError(
            "invalid_task",
            "task_id must match task_<alphanumeric>.",
            field="/task_id",
        )
    title = _require_text(data.get("title"), "title", MAX_TITLE_LENGTH)
    prompt = _require_text(data.get("prompt"), "prompt", MAX_PROMPT_LENGTH)

    schedule = data.get("schedule")
    if not isinstance(schedule, Mapping):
        raise SchedulerError("invalid_task", "schedule must be an object.", field="/schedule")
    if schedule.get("kind", "cron") != "cron":
        raise SchedulerError(
            "invalid_task", 'schedule.kind must be "cron".', field="/schedule/kind"
        )
    expr = schedule.get("expr")
    parse_cron(expr)  # raises a CronError naming /schedule/expr
    timezone_name = resolve_timezone(data.get("timezone")).key

    next_run = _parse_iso(data.get("next_run_at"), field="/next_run_at")
    if next_run is None:
        raise SchedulerError(
            "invalid_task", "next_run_at is required.", field="/next_run_at"
        )

    state = data.get("state", "active")
    if not isinstance(state, str) or state not in STATES:
        raise SchedulerError(
            "invalid_task",
            f"state must be one of {', '.join(sorted(STATES))}.",
            field="/state",
        )
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise SchedulerError("invalid_task", "enabled must be a boolean.", field="/enabled")

    created_at = _parse_iso(data.get("created_at"), field="/created_at") or now or utc_now()
    updated_at = _parse_iso(data.get("updated_at"), field="/updated_at") or created_at
    last_run_at = _parse_iso(data.get("last_run_at"), field="/last_run_at")
    last_scheduled_for = _parse_iso(
        data.get("last_scheduled_for"), field="/last_scheduled_for"
    )
    last_skipped_for = _parse_iso(data.get("last_skipped_for"), field="/last_skipped_for")
    start_at = _parse_iso(data.get("start_at"), field="/start_at")
    end_at = _parse_iso(data.get("end_at"), field="/end_at")
    if start_at is not None and end_at is not None and end_at <= start_at:
        raise SchedulerError(
            "invalid_task", "end_at must be after start_at.", field="/end_at"
        )

    failure_count = data.get("failure_count", 0)
    if not isinstance(failure_count, int) or isinstance(failure_count, bool) or failure_count < 0:
        raise SchedulerError(
            "invalid_task", "failure_count must be a non-negative integer.", field="/failure_count"
        )
    skipped_count = data.get("skipped_count", 0)
    if not isinstance(skipped_count, int) or isinstance(skipped_count, bool) or skipped_count < 0:
        raise SchedulerError(
            "invalid_task", "skipped_count must be a non-negative integer.", field="/skipped_count"
        )

    created_by = data.get("created_by", "agent")
    if not isinstance(created_by, str) or not created_by:
        created_by = "agent"

    return ScheduledTask(
        task_id=task_id,
        title=title,
        prompt=prompt,
        expr=expr.strip(),
        timezone_name=timezone_name,
        next_run_at=_iso(next_run) or "",
        enabled=enabled,
        state=state,
        last_run_at=_iso(last_run_at),
        last_scheduled_for=_iso(last_scheduled_for),
        last_skipped_for=_iso(last_skipped_for),
        failure_count=failure_count,
        skipped_count=min(skipped_count, MAX_SKIPS),
        created_by=created_by,
        created_at=_iso(created_at),
        updated_at=_iso(updated_at),
        start_at=_iso(start_at),
        end_at=_iso(end_at),
    )


# ---------------------------------------------------------------------------
# Atomic file writes
# ---------------------------------------------------------------------------


def _ensure_private_directory(path: Path) -> None:
    """Create a directory tree and force ``0700`` on the final directory.

    Choice: the final directory is chmodded ``0700`` *unconditionally*, even
    when it already existed. This matches :class:`chat.artifacts.ArtifactRegistry`,
    which does the same for ``var/artifacts``. A task file is ``0600``, but the
    file *names* still disclose schedule ids and update times, so an operator
    (or a careless ``mkdir``) leaving ``var/scheduler`` group- or
    world-readable must not turn the store into a listing. Parent directories
    are touched only when this function created them: ``var/`` may live on a
    shared volume with a deliberate group mode, and the store has no business
    changing it.
    """

    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:  # pragma: no cover - filesystem root
            break
        current = parent
    path.mkdir(parents=True, exist_ok=True)
    for created in reversed(missing):
        try:
            os.chmod(created, 0o700)
        except OSError:  # pragma: no cover - platform dependent
            pass
    try:
        os.chmod(path, 0o700)
    except OSError:  # pragma: no cover - platform dependent
        pass


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:  # pragma: no cover - some filesystems do not support it
        pass
    finally:
        os.close(descriptor)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write one JSON document with tmp + fsync + rename and mode ``0600``.

    The temporary file is created in the same directory (so the rename is on
    one filesystem), flushed and fsynced before ``os.replace``, and removed if
    anything fails. A concurrent reader sees either the previous document or
    the complete new one.
    """

    _ensure_private_directory(path.parent)
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        try:
            os.write(descriptor, data)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.chmod(temporary, 0o600)
        except OSError:  # pragma: no cover - platform dependent
            pass
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class ScheduleStore:
    """Read, validate, quarantine, and create task documents.

    A corrupt file is renamed ``<name>.bad`` and the scan continues, so one bad
    byte cannot stop boot or hide the other tasks. A file whose ``task_id`` does
    not match its own name is treated as corrupt and quarantined: the canonical
    ``task_<id>.json`` is the only document that may define ``<id>``. The store
    never rewrites a file it did not just write, and it never follows a
    ``task_id`` containing a path separator.
    """

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        now: Callable[[], datetime] | None = None,
        max_tasks: int | None = None,
    ):
        self.workspace_root = Path(workspace_root)
        self.root = self.workspace_root / "var" / "scheduler"
        self.max_tasks = _env_int(MAX_TASKS_ENV, DEFAULT_MAX_TASKS, minimum=1) if max_tasks is None else max_tasks
        self._now = now or utc_now
        self._create_lock = threading.Lock()
        self.last_quarantined: list[str] = []
        self.last_overflow = 0

    # -- paths -------------------------------------------------------------

    def ensure_root(self) -> Path:
        _ensure_private_directory(self.root)
        return self.root

    def task_path(self, task_id: str) -> Path:
        if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
            raise SchedulerError("invalid_task_id", "invalid task id.", field="/task_id")
        return self.root / f"{task_id}.json"

    # -- read --------------------------------------------------------------

    def load_tasks(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """Load every valid ``*.json`` task, quarantining files that fail."""

        self.last_quarantined = []
        self.last_overflow = 0
        if not self.root.exists():
            return []
        moment = _as_utc(now) if now is not None else _as_utc(self._now())
        tasks: list[dict[str, Any]] = []
        seen: set[str] = set()
        for path in sorted(self.root.glob("*.json")):
            try:
                raw = path.read_text(encoding="utf-8")
                data = json.loads(raw)
                task = _coerce_task(data, now=moment)
                if task.task_id != path.stem:
                    # The file name is the authoritative id: it is what
                    # ``read_task`` and the ticker address, so a document that
                    # disagrees is stray data, not a task with an alias.
                    raise SchedulerError(
                        "task_id_mismatch",
                        f"task file {path.name!r} declares task_id {task.task_id!r}.",
                        field="/task_id",
                    )
                if task.task_id in seen:
                    # Unreachable while every accepted document is named after
                    # its id, but kept as a guard: the first canonical file
                    # wins and any later document with the same id is
                    # quarantined rather than replacing it.
                    raise SchedulerError(
                        "invalid_task", f"duplicate task_id {task.task_id!r}.", field="/task_id"
                    )
                if len(tasks) >= self.max_tasks:
                    self.last_overflow += 1
                    continue
                seen.add(task.task_id)
                tasks.append(task.to_dict())
            except (OSError, ValueError, SchedulerError, json.JSONDecodeError) as exc:
                self._quarantine(path, exc)
        if self.last_overflow:
            log(
                "scheduler_task_overflow",
                error_code="scheduler_full",
                count=self.last_overflow,
                status="warning",
            )
        return tasks

    #: Short alias for embedders that already speak "load the store".
    load = load_tasks

    def read_task(self, task_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        path = self.task_path(task_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise SchedulerError("unknown_task", f"task {task_id!r} does not exist.", field="/task_id") from None
        except (OSError, ValueError) as exc:
            raise SchedulerError("invalid_task", f"task {task_id!r} is not readable.", field="/task_id") from exc
        return _coerce_task(data, now=now).to_dict()

    # -- write -------------------------------------------------------------

    def write_task(self, task: Mapping[str, Any] | ScheduledTask, *, now: datetime | None = None) -> dict[str, Any]:
        moment = _as_utc(now) if now is not None else _as_utc(self._now())
        if isinstance(task, ScheduledTask):
            normalised = task
        else:
            normalised = _coerce_task(task, now=moment)
        document = normalised.to_dict()
        path = self.task_path(normalised.task_id)
        _atomic_write_json(path, document)
        return document

    def create_task(
        self,
        *,
        title: Any,
        prompt: Any,
        schedule: Any = None,
        expr: Any = None,
        timezone: Any = None,
        start_at: Any = None,
        end_at: Any = None,
        created_by: str = "agent",
        now: datetime | None = None,
        audit: AuditLog | None = None,
    ) -> dict[str, Any]:
        """Validate and atomically persist one new task.

        The caller has already passed the approval gate; this method only
        enforces the scheduler's own bounds. ``schedule`` may be the manifest's
        ``{"kind": "cron", "expr": ...}`` object, or ``expr`` may be passed
        directly for tests and embedders.
        """

        moment = _as_utc(now) if now is not None else _as_utc(self._now())
        if schedule is not None:
            if not isinstance(schedule, Mapping):
                raise SchedulerError("invalid_arguments", "schedule must be an object.", field="/schedule")
            if schedule.get("kind", "cron") != "cron":
                raise SchedulerError(
                    "invalid_arguments", 'schedule.kind must be "cron".', field="/schedule/kind"
                )
            expr = schedule.get("expr", expr)
        if expr is None:
            raise SchedulerError(
                "invalid_arguments", "schedule.expr is required.", field="/schedule/expr"
            )
        title = _require_text(title, "title", MAX_TITLE_LENGTH)
        prompt = _require_text(prompt, "prompt", MAX_PROMPT_LENGTH)
        spec = parse_cron(expr)
        zone = resolve_timezone(timezone)

        start = _parse_iso(start_at, field="/start_at")
        end = _parse_iso(end_at, field="/end_at")
        if start is not None and end is not None and end <= start:
            raise SchedulerError("invalid_arguments", "end_at must be after start_at.", field="/end_at")
        anchor = moment
        if start is not None and start > moment:
            # ``next_run_at`` is strictly after its anchor; back up one
            # microsecond so a start_at that lands on a cron minute is included.
            anchor = start - timedelta(microseconds=1)
        first_run = _next_run(spec, zone, anchor)
        if end is not None and first_run > end:
            raise SchedulerError(
                "invalid_arguments",
                "the schedule ends before its first run.",
                field="/end_at",
            )

        with self._create_lock:
            self.ensure_root()
            existing = sum(1 for _ in self.root.glob("*.json"))
            if existing >= self.max_tasks:
                raise SchedulerError(
                    "scheduler_full",
                    f"the scheduler already holds {existing} tasks (max {self.max_tasks}).",
                    field="",
                )
            task_id = self._new_task_id()
            task = ScheduledTask(
                task_id=task_id,
                title=title,
                prompt=prompt,
                expr=spec.expr,
                timezone_name=zone.key,
                next_run_at=_iso(first_run) or "",
                enabled=True,
                state="active",
                created_by=created_by or "agent",
                created_at=_iso(moment),
                updated_at=_iso(moment),
                start_at=_iso(start),
                end_at=_iso(end),
            ).to_dict()
            _atomic_write_json(self.task_path(task_id), task)

        # One content-free row per created task. A caller with the server's
        # audit trail passes it in; a direct store call still gets a row in
        # the scheduler directory rather than silently skipping the audit. The
        # local file is the deliberate fallback sink described in the module
        # docstring, not a second retained audit log.
        _audit_created(audit or AuditLog(self.root / "audit.jsonl"), task)
        return task

    #: Short alias; the long name is clearer in the handler and docs.
    create = create_task

    def _new_task_id(self) -> str:
        for _ in range(16):
            candidate = f"task_{uuid.uuid4().hex[:12]}"
            if not self.task_path(candidate).exists():
                return candidate
        raise SchedulerError("scheduler_full", "could not allocate a task id.")

    def _quarantine(self, path: Path, exc: BaseException) -> None:
        target = path.with_name(f"{path.name}.bad")
        counter = 1
        while target.exists():
            target = path.with_name(f"{path.name}.{counter}.bad")
            counter += 1
        try:
            os.replace(path, target)
        except OSError:
            # Leaving the file is safer than deleting it; the next scan retries.
            return
        self.last_quarantined.append(target.name)
        log(
            "scheduler_task_quarantined",
            error_code=getattr(exc, "code", None) or "invalid_task",
            count=len(self.last_quarantined),
            status="warning",
        )


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        return default
    try:
        value = int(text, 10)
    except ValueError:
        raise SchedulerError("invalid_config", f"{name} must be an integer.") from None
    if value < minimum:
        raise SchedulerError("invalid_config", f"{name} must be >= {minimum}.")
    return value


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"", "0", "false", "no", "off", "disabled"}


def _audit_created(audit: AuditLog, task: Mapping[str, Any]) -> None:
    """One content-free ``schedule_created`` row; never the title or prompt."""

    try:
        audit.record(
            "schedule_created",
            actor=str(task.get("created_by") or "agent"),
            outcome="created",
            extra={
                "schedule_id": task.get("task_id"),
                "cron": (task.get("schedule") or {}).get("expr"),
                "timezone": task.get("timezone"),
                "next_run_at": task.get("next_run_at"),
                "title_length": len(str(task.get("title") or "")),
                "prompt_length": len(str(task.get("prompt") or "")),
                "cost_class": "free",
            },
        )
    except Exception as exc:  # noqa: BLE001 - audit is defence in depth, not the write
        log(
            "scheduler_audit_failed",
            error_code=type(exc).__name__,
            status="error",
        )


class SchedulerRuntime:
    """One process-local ticker over a :class:`ScheduleStore`.

    ``tick`` is synchronous and safe to call from the retention-style daemon
    thread or directly in a test. It loads the store, dispatches due tasks,
    persists state *before* invoking the callback, and returns a content-free
    report. ``start``/``stop`` own the daemon thread; ``KELP_SCHEDULER_ENABLED=0``
    disables both.
    """

    def __init__(
        self,
        store: ScheduleStore,
        *,
        on_fire: Callable[..., Any] | None = None,
        audit: AuditLog | None = None,
        now: Callable[[], datetime] | None = None,
        enabled: bool | None = None,
        tick_seconds: int | None = None,
        grace_seconds: int | None = None,
    ):
        self.store = store
        self.on_fire = on_fire
        self._now = now or utc_now
        self.enabled = _env_flag(ENABLED_ENV, True) if enabled is None else bool(enabled)
        self.tick_seconds = (
            _env_int(TICK_ENV, DEFAULT_TICK_SECONDS, minimum=1)
            if tick_seconds is None
            else max(1, int(tick_seconds))
        )
        self.grace_seconds = (
            _env_int(GRACE_ENV, DEFAULT_GRACE_SECONDS, minimum=0)
            if grace_seconds is None
            else max(0, int(grace_seconds))
        )
        self.audit = audit if audit is not None else AuditLog(store.root / "audit.jsonl")
        # No server audit trail passed: keep the row beside the tasks (inside
        # the scheduler write scope) instead of dropping it. This is the
        # intentional local fallback sink; production passes the retained
        # ``KELP_AUDIT_DIR`` log, which retention rotates.
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()

    # -- tick --------------------------------------------------------------

    def run_once(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Alias matching :class:`~chat.retention.RetentionMaintenance`'s boot pass."""

        return self.tick(now=now)

    def tick(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Run one due-set pass. Never raises for a single bad task."""

        report: dict[str, Any] = {
            "enabled": self.enabled,
            "loaded": 0,
            "quarantined": 0,
            "due": 0,
            "fired": 0,
            "missed": 0,
            "completed": 0,
            "errors": 0,
            "runs": [],
        }
        if not self.enabled:
            return report

        moment = _as_utc(now) if now is not None else _as_utc(self._now())
        with _tick_lock(self.store.root) as acquired:
            if not acquired:
                report["skipped"] = True
                return report
            tasks = self.store.load_tasks(now=moment)
            report["loaded"] = len(tasks)
            report["quarantined"] = len(self.store.last_quarantined)
            for task in tasks:
                try:
                    self._tick_task(task, moment, report)
                except Exception as exc:  # noqa: BLE001 - one task cannot stop the pass
                    report["errors"] += 1
                    log(
                        "scheduler_tick_failed",
                        error_code=type(exc).__name__,
                        tool="schedule.create",
                        status="error",
                    )
        return report

    def _tick_task(
        self, task: dict[str, Any], now: datetime, report: dict[str, Any]
    ) -> None:
        if not task.get("enabled", True):
            return
        if task.get("state") not in RUNNABLE_STATES:
            return
        next_run = _parse_iso(task.get("next_run_at"), field="/next_run_at")
        if next_run is None or next_run > now:
            return

        report["due"] += 1
        end_at = _parse_iso(task.get("end_at"), field="/end_at")
        if end_at is not None and now > end_at:
            # The window closed before this run. That is a missed run, not a
            # silent completion: record it like the grace-expired case so the
            # audit trail explains why the callback never fired.
            task["state"] = "completed"
            task["updated_at"] = _iso(now)
            self.store.write_task(task, now=now)
            missed_run_id = _run_id()
            report["completed"] += 1
            report["missed"] += 1
            self._audit_run(task, missed_run_id, next_run, "missed", 0, now)
            report["runs"].append(
                {
                    "task_id": task["task_id"],
                    "run_id": missed_run_id,
                    "scheduled_for": _iso(next_run),
                    "outcome": "missed",
                    "duration_ms": 0,
                }
            )
            return

        if (now - next_run).total_seconds() > self.grace_seconds:
            # Too old to catch up. Record one miss, then move the next run into
            # the future; no burst, no repeated replay.
            following = next_run_at(task["schedule"]["expr"], task["timezone"], after=now)
            task["last_skipped_for"] = _iso(next_run)
            task["skipped_count"] = min(
                MAX_SKIPS, int(task.get("skipped_count") or 0) + 1
            )
            task["next_run_at"] = _iso(following)
            task["updated_at"] = _iso(now)
            self.store.write_task(task, now=now)
            missed_run_id = _run_id()
            report["missed"] += 1
            self._audit_run(task, missed_run_id, next_run, "missed", 0, now)
            report["runs"].append(
                {
                    "task_id": task["task_id"],
                    "run_id": missed_run_id,
                    "scheduled_for": _iso(next_run),
                    "outcome": "missed",
                    "duration_ms": 0,
                }
            )
            return

        # Claim before dispatch: a crash or a second ticker sees the advanced
        # next_run_at and cannot repeat this window.
        scheduled_for = next_run
        following = next_run_at(task["schedule"]["expr"], task["timezone"], after=now)
        if end_at is not None and following > end_at:
            following = None
        run_id = _run_id()
        task["last_run_at"] = _iso(now)
        task["last_scheduled_for"] = _iso(scheduled_for)
        task["next_run_at"] = _iso(following) if following is not None else _iso(scheduled_for)
        task["state"] = "completed" if following is None else "active"
        task["updated_at"] = _iso(now)
        self.store.write_task(task, now=now)

        started = time.perf_counter()
        outcome = "ok"
        try:
            if self.on_fire is not None:
                _invoke_callback(self.on_fire, task, scheduled_for)
        except Exception as exc:  # noqa: BLE001 - a failed run is data, not a crash
            outcome = "error"
            task["failure_count"] = int(task.get("failure_count") or 0) + 1
            task["updated_at"] = _iso(now)
            try:
                self.store.write_task(task, now=now)
            except Exception:  # noqa: BLE001 - the claim is already durable
                pass
            report["errors"] += 1
            log(
                "scheduler_run_failed",
                error_code=type(exc).__name__,
                tool="schedule.create",
                status="error",
            )
        duration_ms = int((time.perf_counter() - started) * 1000)
        report["fired"] += 1
        if outcome == "ok" and task.get("state") == "completed":
            report["completed"] += 1
        self._audit_run(task, run_id, scheduled_for, outcome, duration_ms, now)
        report["runs"].append(
            {
                "task_id": task["task_id"],
                "run_id": run_id,
                "scheduled_for": _iso(scheduled_for),
                "outcome": outcome,
                "duration_ms": duration_ms,
            }
        )

    def _audit_run(
        self,
        task: Mapping[str, Any],
        run_id: str,
        scheduled_for: datetime,
        outcome: str,
        duration_ms: int,
        now: datetime,
    ) -> None:
        try:
            self.audit.record(
                "schedule_run",
                actor=str(task.get("created_by") or "agent"),
                outcome=outcome,
                duration_ms=duration_ms,
                tool="schedule.create",
                extra={
                    "schedule_id": task.get("task_id"),
                    "run_id": run_id,
                    "cron": (task.get("schedule") or {}).get("expr"),
                    "timezone": task.get("timezone"),
                    "scheduled_for": _iso(scheduled_for),
                    "started_at": _iso(now),
                    "cost_class": "free",
                },
            )
        except Exception as exc:  # noqa: BLE001 - audit failure does not undo a fire
            log(
                "scheduler_audit_failed",
                error_code=type(exc).__name__,
                status="error",
            )

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Start the daemon loop: one tick immediately, then one per interval."""

        if not self.enabled:
            return
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="kelp-scheduler", daemon=True
            )
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - never kill the process
                log(
                    "scheduler_tick_failed",
                    error_code=type(exc).__name__,
                    tool="schedule.create",
                    status="error",
                )
            if self._stop.wait(self.tick_seconds):
                break

    def stop(self, timeout: float = 5.0) -> None:
        with self._thread_lock:
            thread, self._thread = self._thread, None
            self._stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    # ``RetentionMaintenance`` calls this ``stop`` too; the alias keeps the
    # ownership order obvious in a server shutdown path.
    close = stop

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


def _run_id() -> str:
    return f"run_{uuid.uuid4().hex[:12]}"


def _invoke_callback(
    callback: Callable[..., Any], task: Mapping[str, Any], scheduled_for: datetime
) -> None:
    """Call a sync or async callback with one or two positional arguments."""

    try:
        signature = inspect.signature(callback)
        signature.bind(task, scheduled_for)
        args: tuple[Any, ...] = (dict(task), scheduled_for)
    except (TypeError, ValueError):
        args = (dict(task),)
    result = callback(*args)
    if inspect.isawaitable(result):
        asyncio.run(result)


@contextmanager
def _tick_lock(root: Path) -> Iterator[bool]:
    """Serialize tickers in one process and across processes on POSIX.

    ``flock`` is advisory and per open file description, which is enough for a
    single-node deployment. If the lock cannot be taken, the caller reports a
    skipped tick rather than running the due set twice.
    """

    if not root.exists():
        yield True
        return
    descriptor: int | None = None
    locked = False
    try:
        descriptor = os.open(
            root / _TICK_LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600
        )
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
    except OSError:
        locked = False
    try:
        yield locked
    finally:
        if descriptor is not None:
            if locked:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:  # pragma: no cover - best effort
                    pass
            os.close(descriptor)


def load_tasks(
    workspace_root: str | Path,
    *,
    now: datetime | None = None,
    max_tasks: int | None = None,
) -> list[dict[str, Any]]:
    """Convenience wrapper for a one-shot, validated scan (boot or tests)."""

    store = ScheduleStore(workspace_root, max_tasks=max_tasks)
    return store.load_tasks(now=now)


def create_task(
    workspace_root: str | Path,
    *,
    title: Any,
    prompt: Any,
    schedule: Any = None,
    expr: Any = None,
    timezone: Any = None,
    start_at: Any = None,
    end_at: Any = None,
    created_by: str = "agent",
    now: datetime | None = None,
    audit: AuditLog | None = None,
    max_tasks: int | None = None,
) -> dict[str, Any]:
    """Convenience wrapper around :meth:`ScheduleStore.create_task`."""

    store = ScheduleStore(workspace_root, max_tasks=max_tasks)
    return store.create_task(
        title=title,
        prompt=prompt,
        schedule=schedule,
        expr=expr,
        timezone=timezone,
        start_at=start_at,
        end_at=end_at,
        created_by=created_by,
        now=now,
        audit=audit,
    )


# Conventional aliases: both spellings read naturally, and the shorter one is
# what callers tend to guess.
SchedulerStore = ScheduleStore
Scheduler = SchedulerRuntime
Cron = CronSpec

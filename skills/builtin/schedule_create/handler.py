"""`schedule.create` handler — persist one approved scheduled task.

The handler is the only write skill in the prototype. It does four things and
nothing else:

1. Validate the manifest arguments again (the loader validates registry calls,
   but a direct call must fail with the same field pointers).
2. Parse the five-field cron expression and load the IANA timezone with the
   scheduler's own parser, so create time and fire time cannot disagree.
3. Resolve the scheduler directory through ``ctx.write_path`` (the policy
   chokepoint for the declared ``${WORKSPACE}/var/scheduler/**`` scope) and let
   :class:`chat.scheduler.ScheduleStore` write the task atomically: temporary
   file in the same directory, ``fsync``, ``rename``, mode ``0600``.
4. Write exactly one content-free ``schedule_created`` audit row. The title and
   prompt are never copied into the audit trail; only ids, cron, timezone,
   timestamps, and lengths.

Non-idempotent by contract: a retry creates a second task, which is why the
manifest keeps ``requires_approval`` true. The handler never retries a write.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from chat.scheduler import (
    MAX_PROMPT_LENGTH,
    MAX_TITLE_LENGTH,
    ScheduleStore,
    SchedulerError,
    resolve_timezone,
    utc_now,
)
from openshell.audit import AuditLog
from skills.context import SkillContext
from skills.errors import SkillHandlerError

__all__ = ["run"]

_ALLOWED_KEYS = frozenset(
    {"title", "prompt", "schedule", "timezone", "start_at", "end_at"}
)
_SCHEDULE_KEYS = frozenset({"kind", "expr"})


def _invalid(field: str, message: str) -> SkillHandlerError:
    return SkillHandlerError(
        "invalid_arguments",
        message,
        retryable=False,
        user_fixable=True,
        details={"field": field},
    )


def _audit_target(context: Any, workspace_root: Path) -> AuditLog | None:
    """Use the invocation's audit trail when present, else a local one.

    ``GuardedSkillContext`` carries the server's :class:`AuditLog` for the
    invocation. A direct handler call (tests, embedders) has no server trail,
    so the row goes to ``var/scheduler/audit.jsonl`` inside the declared write
    scope rather than creating a file outside it. That local file is a
    deliberate fallback sink, not a second entry in the retention inventory:
    production calls pass the retained ``KELP_AUDIT_DIR`` log, which
    :class:`chat.retention.RetentionMaintenance` rotates.
    """

    audit = getattr(context, "audit", None)
    if isinstance(audit, AuditLog):
        return audit
    audit = getattr(context, "_audit", None)
    if isinstance(audit, AuditLog):
        return audit
    return AuditLog(workspace_root / "var" / "scheduler" / "audit.jsonl")


def _resolve_root(context: Any, workspace_root: Path) -> Path:
    root = workspace_root / "var" / "scheduler"
    check = getattr(context, "write_path", None)
    if callable(check):
        # Raises PolicyDenied for anything outside the manifest scope. The
        # store then performs the actual I/O under this already-checked path.
        return Path(check(root))
    return root


def _validate(arguments: Any) -> tuple[str, str, Mapping[str, Any], str, Any, Any]:
    if not isinstance(arguments, Mapping):
        raise _invalid("", "Arguments must be an object.")
    unknown = sorted(set(arguments) - _ALLOWED_KEYS)
    if unknown:
        raise _invalid(f"/{unknown[0]}", f"Unknown argument {unknown[0]!r}.")

    title = arguments.get("title")
    if not isinstance(title, str) or not title:
        raise _invalid("/title", "title must be a non-empty string.")
    if len(title) > MAX_TITLE_LENGTH:
        raise _invalid(
            "/title", f"title must be at most {MAX_TITLE_LENGTH} characters."
        )
    if "\x00" in title:
        raise _invalid("/title", "title must not contain a NUL byte.")

    prompt = arguments.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        raise _invalid("/prompt", "prompt must be a non-empty string.")
    if len(prompt) > MAX_PROMPT_LENGTH:
        raise _invalid(
            "/prompt", f"prompt must be at most {MAX_PROMPT_LENGTH} characters."
        )
    if "\x00" in prompt:
        raise _invalid("/prompt", "prompt must not contain a NUL byte.")

    schedule = arguments.get("schedule")
    if not isinstance(schedule, Mapping):
        raise _invalid("/schedule", "schedule must be an object.")
    unknown_schedule = sorted(set(schedule) - _SCHEDULE_KEYS)
    if unknown_schedule:
        raise _invalid(
            f"/schedule/{unknown_schedule[0]}",
            f"Unknown schedule argument {unknown_schedule[0]!r}.",
        )
    if schedule.get("kind", "cron") != "cron":
        raise _invalid("/schedule/kind", 'schedule.kind must be "cron".')
    expr = schedule.get("expr")
    if not isinstance(expr, str) or not expr.strip():
        raise _invalid("/schedule/expr", "schedule.expr must be a non-empty string.")

    timezone_name = arguments.get("timezone")
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise _invalid("/timezone", "timezone must be a non-empty IANA name.")
    try:
        resolve_timezone(timezone_name)
    except SchedulerError as exc:
        raise _invalid("/timezone", str(exc)) from None

    return title, prompt, schedule, timezone_name.strip(), arguments.get("start_at"), arguments.get("end_at")


async def run(arguments: Mapping[str, Any], context: SkillContext) -> Mapping[str, Any]:
    """Persist a scheduled task and return its id and next run time."""

    title, prompt, schedule, timezone_name, start_at, end_at = _validate(arguments)
    workspace_root = Path(context.workspace_root)
    root = _resolve_root(context, workspace_root)
    store = ScheduleStore(workspace_root, now=utc_now)
    store.root = root
    audit = _audit_target(context, workspace_root)
    try:
        task = store.create_task(
            title=title,
            prompt=prompt,
            schedule=schedule,
            timezone=timezone_name,
            start_at=start_at,
            end_at=end_at,
            created_by="agent",
            audit=audit,
        )
    except SchedulerError as exc:
        if exc.code in {
            "invalid_arguments",
            "invalid_task",
            "invalid_timezone",
            "cron_field_count",
            "cron_shortcut",
            "cron_unsupported",
            "cron_syntax",
            "cron_range",
            "cron_step",
            "cron_no_match",
        }:
            raise _invalid(exc.field or "/schedule/expr", str(exc)) from None
        raise SkillHandlerError(
            "handler_error",
            str(exc),
            retryable=False,
            user_fixable=True,
            details={"field": exc.field} if exc.field else None,
        ) from None
    except OSError as exc:
        raise SkillHandlerError(
            "handler_error",
            f"Could not write the schedule: {type(exc).__name__}.",
            retryable=False,
            user_fixable=False,
        ) from exc

    return {
        "task_id": task["task_id"],
        "status": "scheduled",
        "next_run_at": task["next_run_at"],
    }

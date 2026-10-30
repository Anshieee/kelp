"""Proactive turn runner (KEL-27, M4-B).

M4-A's scheduler (:mod:`chat.scheduler`) knows *when* a task is due. This
module turns one due task into an ordinary chat turn: the same
:class:`~chat.service.ChatService` path, the same kernel loop, the same policy
gate, and the same append-only event stream. There is no shadow execution path
and no second message store.

What one fire does, in order:

1. **Spend gate.** :meth:`ChatService._refuse_if_over_spend` runs before a
   thread, a message, a turn, or a provider call exists. A capped window skips
   the run; an approval can never buy past a ceiling.
2. **Concurrency gate.** At most ``KELP_PROACTIVE_MAX_CONCURRENT`` (default 1)
   proactive runs are in flight. Being over the cap is a skip, never a queue:
   the due window is consumed and a reason code is recorded.
3. **Hourly gate.** At most ``KELP_PROACTIVE_MAX_RUNS_PER_HOUR`` (default 5)
   runs start in a rolling hour, counted from this process. Again a skip, not a
   queue.
4. **Dispatch.** A stable dedicated thread per schedule receives the task's
   system-authored intent, prefixed with one ``schedule_trigger`` block that
   carries ``origin=schedule``, the schedule id, the run id, and the scheduled
   instant. The turn then streams over the existing SSE path like any other.

Headless approvals. A scheduled actor can never approve anything (ADR 0002 §5).
When a tool asks for a decision during a proactive turn, the runner records a
denial through the same service path an owner's Deny would use and pauses the
schedule in ``paused_needs_approval``. Nothing is auto-approved, the tool does
not run, and the schedule does not advance until the owner intervenes.

Audit. Every fire produces exactly one content-free ``schedule_run`` row: the
schedule id, run id, turn id, outcome, duration, token counts, and estimated
cost. The task title and prompt never enter it. A pre-turn skip writes the same
shape with outcome ``skipped`` and a reason code, so "nothing ran" is data, not
a log line the owner cannot see.

Boot. The scheduler claims a window before dispatching, so a crash cannot fire
it twice; boot recovery settles the lost turn ``interrupted`` and this module
writes the missing run row (``outcome="interrupted"``) rather than retrying it.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from openshell.audit import AuditLog

from .errors import ChatError
from .logging import log
from .scheduler import MAX_SKIPS, RUNNABLE_STATES, ScheduleStore, utc_now
from .service import ChatService
from .store import ChatStore

__all__ = [
    "DEFAULT_MAX_CONCURRENT",
    "DEFAULT_MAX_RUNS_PER_HOUR",
    "MAX_CONCURRENT_ENV",
    "MAX_RUNS_PER_HOUR_ENV",
    "ProactiveConfig",
    "ProactiveRunner",
    "SCHEDULE_ORIGIN",
    "TRIGGER_BLOCK_TYPE",
    "trigger_block",
]

#: A proactive turn is always this origin. It appears on the trigger block, in
#: the thread's ``title_source``, and in every run row.
SCHEDULE_ORIGIN = "schedule"
TRIGGER_BLOCK_TYPE = "schedule_trigger"

MAX_CONCURRENT_ENV = "KELP_PROACTIVE_MAX_CONCURRENT"
MAX_RUNS_PER_HOUR_ENV = "KELP_PROACTIVE_MAX_RUNS_PER_HOUR"
DEFAULT_MAX_CONCURRENT = 1
DEFAULT_MAX_RUNS_PER_HOUR = 5

#: The rolling window for :data:`MAX_RUNS_PER_HOUR_ENV`.
HOURLY_WINDOW_SECONDS = 3600.0
#: How often a running proactive turn is checked for a pending approval. The
#: service polls its own waiter at the same cadence, so a denial lands within
#: one slice without busy-waiting.
_APPROVAL_POLL_SECONDS = 0.05
_APPROVAL_SCAN_LIMIT = 200


def _env_int(source: Mapping[str, str], name: str, default: int, *, minimum: int) -> int:
    raw = source.get(name)
    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        return default
    try:
        value = int(text, 10)
    except ValueError:
        raise ValueError(f"{name} must be an integer.") from None
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}.")
    return value


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    text = _as_utc(moment).isoformat(timespec="seconds")
    return text[:-6] + "Z" if text.endswith("+00:00") else text


def _run_id() -> str:
    return f"run_{uuid.uuid4().hex[:12]}"


def _error_code(value: Any) -> str:
    if isinstance(value, BaseException):
        value = type(value).__name__
    if not isinstance(value, str):
        return "unknown"
    text = value.strip()
    return text[:64] if text else "unknown"


def _usage_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _usage_cost(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def trigger_block(
    *,
    schedule_id: str,
    run_id: str,
    scheduled_for: str,
    title: str | None = None,
) -> dict[str, Any]:
    """The user-message block that marks a turn as schedule-originated.

    It is deliberately small and content-free beyond the task title: the prompt
    itself is the text block that follows, exactly as a human message would be.
    """

    block: dict[str, Any] = {
        "type": TRIGGER_BLOCK_TYPE,
        "origin": SCHEDULE_ORIGIN,
        "schedule_id": schedule_id,
        "run_id": run_id,
        "scheduled_for": scheduled_for,
    }
    if title:
        block["title"] = title
    return block


@dataclass(frozen=True)
class ProactiveConfig:
    """The two process-wide bounds on proactive work."""

    max_concurrent: int = DEFAULT_MAX_CONCURRENT
    max_runs_per_hour: int = DEFAULT_MAX_RUNS_PER_HOUR

    def __post_init__(self) -> None:
        if self.max_concurrent < 1:
            raise ValueError(f"{MAX_CONCURRENT_ENV} must be >= 1.")
        if self.max_runs_per_hour < 0:
            raise ValueError(f"{MAX_RUNS_PER_HOUR_ENV} must be >= 0.")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ProactiveConfig":
        source = os.environ if env is None else env
        return cls(
            max_concurrent=_env_int(
                source,
                MAX_CONCURRENT_ENV,
                DEFAULT_MAX_CONCURRENT,
                minimum=1,
            ),
            max_runs_per_hour=_env_int(
                source,
                MAX_RUNS_PER_HOUR_ENV,
                DEFAULT_MAX_RUNS_PER_HOUR,
                minimum=0,
            ),
        )


@dataclass(frozen=True)
class _ActiveRun:
    run_id: str
    schedule_id: str
    scheduled_for: datetime
    scheduled_iso: str
    started_wall: datetime
    started_mono: float


class _RunAuditGate:
    """The audit sink handed to :class:`~chat.scheduler.SchedulerRuntime`.

    The scheduler writes its own ``schedule_run`` row after every callback.
    This runner writes the richer row (with turn id, tokens, and cost) once the
    turn settles, so the scheduler's row for a handled fire is suppressed here.
    Missed and expired windows never reach the runner and keep their row.
    """

    def __init__(self, audit: AuditLog):
        self._audit = audit
        self._lock = threading.Lock()
        self._handled: set[tuple[str, str]] = set()

    def expect(self, schedule_id: str, scheduled_for: str) -> None:
        with self._lock:
            self._handled.add((schedule_id, scheduled_for))

    def record(self, event: str, **fields: Any) -> dict[str, Any] | None:
        if event == "schedule_run":
            extra = fields.get("extra") or {}
            key = (str(extra.get("schedule_id") or ""), str(extra.get("scheduled_for") or ""))
            with self._lock:
                if key in self._handled:
                    self._handled.discard(key)
                    return None
        return self._audit.record(event, **fields)


class ProactiveRunner:
    """Turn due scheduler tasks into ordinary chat turns.

    ``runner`` is the process's event-loop runner (``chat.api.LoopRunner`` in
    the server): turn coroutines must run on the same loop as every interactive
    turn so :meth:`ChatService.aclose` can settle them during shutdown.
    """

    def __init__(
        self,
        *,
        service: ChatService,
        store: ChatStore,
        schedule_store: ScheduleStore,
        audit: AuditLog | None = None,
        runner: Any | None = None,
        config: ProactiveConfig | None = None,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ):
        self._service = service
        self._store = store
        self._schedule_store = schedule_store
        self._audit = audit if audit is not None else AuditLog(
            schedule_store.root / "audit.jsonl"
        )
        self.audit_gate = _RunAuditGate(self._audit)
        self._runner = runner
        self.config = config or ProactiveConfig()
        self._now = now or utc_now
        self._monotonic = monotonic or time.monotonic

        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._in_flight: dict[str, _ActiveRun] = {}
        self._started: deque[float] = deque()
        self._futures: dict[str, Any] = {}
        self._fallback_threads: dict[str, str] = {}
        self._thread_lock = threading.Lock()
        self._closed = False

    # -- read-only helpers -------------------------------------------------

    @property
    def in_flight(self) -> int:
        with self._lock:
            return len(self._in_flight)

    def thread_id_for(self, schedule_id: str) -> str:
        """The dedicated thread id for one schedule (stable across restarts)."""

        with self._lock:
            fallback = self._fallback_threads.get(schedule_id)
        return fallback or f"ses_{schedule_id}"

    # -- scheduler callback ------------------------------------------------

    def fire(self, task: Mapping[str, Any], scheduled_for: datetime) -> dict[str, Any]:
        """The :class:`SchedulerRuntime` ``on_fire`` callback.

        Synchronous and fast: it runs the pre-turn guards, reserves a slot, and
        hands the coroutine to the event-loop runner. It never raises, so one
        bad fire cannot stop the tick or be counted as a callback failure.
        """

        run: _ActiveRun | None = None
        recorded = False
        try:
            schedule_id = str(task.get("task_id") or "")
            when = _as_utc(scheduled_for)
            run = _ActiveRun(
                run_id=_run_id(),
                schedule_id=schedule_id,
                scheduled_for=when,
                scheduled_iso=_iso(when),
                started_wall=self._now(),
                started_mono=self._monotonic(),
            )
            # From here on the scheduler's own row is replaced by the richer
            # one this runner writes, so exactly one row exists per fire.
            self.audit_gate.expect(schedule_id, run.scheduled_iso)

            reason = self._pre_turn_refusal()
            if reason is None:
                reason = self._admit(run)
            if reason is not None:
                self._write_run_row(task, run, "skipped", reason, 0)
                self._note_skipped(task, run)
                recorded = True
                log(
                    "proactive_run_skipped",
                    thread_id=None,
                    turn=None,
                    tool="schedule.create",
                    error_code=reason,
                    status="warning",
                )
                return {"run_id": run.run_id, "outcome": "skipped", "reason": reason}

            try:
                future = self._submit(self._execute(run, dict(task)))
            except Exception as exc:  # noqa: BLE001 - a dead loop is a failed run
                self._release(run.run_id)
                reason = _error_code(exc)
                self._write_run_row(task, run, "failed", reason, 0)
                recorded = True
                return {"run_id": run.run_id, "outcome": "failed", "reason": reason}

            with self._lock:
                self._futures[run.run_id] = future
                if run.run_id not in self._in_flight:
                    # A very fast turn can settle before the future is stored.
                    self._futures.pop(run.run_id, None)
            return {"run_id": run.run_id, "outcome": "started"}
        except Exception as exc:  # noqa: BLE001 - never raise into the ticker
            if run is not None and not recorded:
                try:
                    self._write_run_row(task, run, "failed", _error_code(exc), 0)
                except Exception:  # pragma: no cover - audit may be unwritable
                    pass
                if run.run_id in self._in_flight:
                    self._release(run.run_id)
            return {
                "run_id": run.run_id if run is not None else None,
                "outcome": "failed",
                "reason": _error_code(exc),
            }

    # -- pre-turn guards ---------------------------------------------------

    def _pre_turn_refusal(self) -> str | None:
        """Guard 1: the persisted spend ceiling, before anything is written."""

        try:
            self._service._refuse_if_over_spend(action="schedule")  # noqa: SLF001
        except ChatError as exc:
            return "spend_cap" if exc.code == "spend_cap_reached" else "spend_cap_error"
        except Exception:  # noqa: BLE001 - unreadable accounting fails closed
            return "spend_cap_error"
        return None

    def _admit(self, run: _ActiveRun) -> str | None:
        """Guards 2 and 3: concurrency and the rolling hourly ceiling.

        Admission and reservation happen under one lock, so two tasks due in
        one tick cannot both take the last slot. A refusal leaves no trace on
        the thread: the run is skipped, not queued.
        """

        now = self._monotonic()
        with self._cond:
            if self._closed:
                return "shutting_down"
            cutoff = now - HOURLY_WINDOW_SECONDS
            while self._started and self._started[0] <= cutoff:
                self._started.popleft()
            if len(self._in_flight) >= self.config.max_concurrent:
                return "concurrency_cap"
            if len(self._started) >= self.config.max_runs_per_hour:
                return "hourly_cap"
            self._in_flight[run.run_id] = run
            self._started.append(now)
            return None

    def _release(self, run_id: str) -> None:
        with self._cond:
            self._in_flight.pop(run_id, None)
            self._futures.pop(run_id, None)
            self._cond.notify_all()

    def _submit(self, coro: Any) -> Any:
        if self._runner is None:
            raise RuntimeError("no event-loop runner is wired for proactive turns")
        spawn = getattr(self._runner, "spawn", None)
        if callable(spawn):
            return spawn(coro)
        submit = getattr(self._runner, "submit", None)
        if callable(submit):
            return submit(coro)
        return self._runner.run(coro)

    # -- one turn ----------------------------------------------------------

    async def _execute(self, run: _ActiveRun, task: Mapping[str, Any]) -> None:
        outcome = "failed"
        reason = "internal_error"
        turn_id: str | None = None
        thread_id: str | None = None
        usage: Mapping[str, Any] = {}
        try:
            thread_id = self._ensure_thread(run, task)
            prompt = str(task.get("prompt") or "")
            payload, _created = await self._service.send(
                thread_id,
                text=prompt,
                client_turn_id=f"schedule:{run.schedule_id}:{run.scheduled_iso}",
            )
            turn_id = str(payload["turn_id"])
            # The send above did not yield: the turn task starts only when this
            # coroutine next awaits, so the trigger block is persisted before
            # the model can see the message.
            self._attach_trigger(thread_id, str(payload["user_message_id"]), run, task, turn_id)
            denied = await self._watch_turn(thread_id, turn_id)
            turn = self._store.get_turn(turn_id)
            usage = dict((turn or {}).get("usage") or {})
            if denied or self._denied_for_turn(thread_id, turn_id):
                outcome, reason = "paused_needs_approval", "approval_required"
            elif turn is None:
                outcome, reason = "failed", "turn_missing"
            elif turn.get("state") != "finished":
                outcome, reason = "failed", "turn_unsettled"
            else:
                why = str(turn.get("reason") or "")
                if why == "interrupted":
                    outcome, reason = "interrupted", "interrupted"
                elif why == "cancelled":
                    outcome, reason = "cancelled", "cancelled"
                elif why == "error":
                    outcome, reason = "failed", "error"
                elif why == "budget":
                    outcome, reason = "failed", "budget"
                else:
                    # ``stop`` and ``length`` are both normal completions; the
                    # assistant message is complete either way.
                    outcome, reason = "ok", why or "stop"
            if outcome == "paused_needs_approval":
                self._pause_schedule(run.schedule_id)
        except asyncio.CancelledError:
            outcome, reason = "interrupted", "shutdown"
            raise
        except ChatError as exc:
            outcome, reason = "failed", _error_code(exc.code)
        except Exception as exc:  # noqa: BLE001 - a failed turn is data
            outcome, reason = "failed", _error_code(exc)
        finally:
            duration_ms = int((self._monotonic() - run.started_mono) * 1000)
            self._finish_run(
                run,
                task,
                outcome,
                reason,
                turn_id=turn_id,
                thread_id=thread_id,
                usage=usage,
                duration_ms=duration_ms,
            )

    def _finish_run(
        self,
        run: _ActiveRun,
        task: Mapping[str, Any],
        outcome: str,
        reason: str,
        *,
        turn_id: str | None,
        thread_id: str | None,
        usage: Mapping[str, Any],
        duration_ms: int,
    ) -> None:
        try:
            self._write_run_row(
                task,
                run,
                outcome,
                reason,
                duration_ms,
                turn_id=turn_id,
                thread_id=thread_id,
                usage=usage,
            )
            if outcome == "paused_needs_approval":
                self._pause_schedule(run.schedule_id)
        finally:
            self._release(run.run_id)

    # -- approval handling -------------------------------------------------

    async def _watch_turn(self, thread_id: str, turn_id: str) -> bool:
        """Deny every gate this turn opens, then wait for it to settle.

        The scheduled actor has no approval authority. Denying through
        :meth:`ChatService.decide` closes the card, unblocks the kernel's
        waiter, and leaves the tool unexecuted; nothing is auto-approved.
        """

        denied = False
        while True:
            turn = self._store.get_turn(turn_id)
            if turn is None or turn.get("state") == "finished":
                return denied or self._denied_for_turn(thread_id, turn_id)
            for card in self._store.list_approvals(
                thread_id=thread_id, status="pending", limit=_APPROVAL_SCAN_LIMIT
            ):
                if str(card.get("turn_id") or "") != turn_id:
                    continue
                try:
                    self._service.decide(str(card["id"]), "deny")
                    denied = True
                except ChatError:
                    # Expired or already settled by another writer: the turn
                    # cannot be allowed to proceed, but there is nothing left
                    # to deny.
                    denied = True
                except Exception:  # noqa: BLE001 - never let a card stop the watch
                    denied = True
            await asyncio.sleep(_APPROVAL_POLL_SECONDS)

    def _denied_for_turn(self, thread_id: str, turn_id: str) -> bool:
        for card in self._store.list_approvals(
            thread_id=thread_id, status="denied", limit=_APPROVAL_SCAN_LIMIT
        ):
            if str(card.get("turn_id") or "") == turn_id:
                return True
        return False

    # -- thread and message identity --------------------------------------

    def _ensure_thread(self, run: _ActiveRun, task: Mapping[str, Any]) -> str:
        schedule_id = run.schedule_id
        title = str(task.get("title") or "Scheduled task").strip() or "Scheduled task"
        label = f"Scheduled: {title}"[:120]
        with self._thread_lock:
            cached = self._fallback_threads.get(schedule_id)
            if cached is not None:
                return cached
            thread_id = f"ses_{schedule_id}"
            if self._store.get_thread(thread_id) is not None:
                return thread_id
            try:
                self._store.create_thread(
                    title=label,
                    title_source=SCHEDULE_ORIGIN,
                    thread_id=thread_id,
                )
                return thread_id
            except sqlite3.IntegrityError:
                # The deterministic thread existed and was soft-deleted. Its
                # id cannot be reused, so start a fresh dedicated thread and
                # remember it for the rest of this process.
                fresh = self._store.create_thread(
                    title=label, title_source=SCHEDULE_ORIGIN
                )
                self._fallback_threads[schedule_id] = str(fresh["id"])
                return str(fresh["id"])

    def _attach_trigger(
        self,
        thread_id: str,
        user_message_id: str,
        run: _ActiveRun,
        task: Mapping[str, Any],
        turn_id: str,
    ) -> None:
        message = self._store.get_message(user_message_id)
        if message is None:  # pragma: no cover - send just created it
            return
        blocks = list(message.get("blocks") or [])
        if any(block.get("type") == TRIGGER_BLOCK_TYPE for block in blocks):
            return
        block = trigger_block(
            schedule_id=run.schedule_id,
            run_id=run.run_id,
            scheduled_for=run.scheduled_iso,
            title=str(task.get("title") or ""),
        )
        self._store.update_message(user_message_id, blocks=[block, *blocks])
        # Additive chat-layer event: the live stream learns which schedule drove
        # the message without re-reading history, and replay gets the same
        # marker. The message history carries the block itself.
        self._store.append_event(
            thread_id,
            TRIGGER_BLOCK_TYPE,
            {"turn_id": turn_id, "message_id": user_message_id, **block},
        )
        self._service.bus.publish(thread_id)

    # -- task state --------------------------------------------------------

    def _note_skipped(self, task: Mapping[str, Any], run: _ActiveRun) -> None:
        """Bump the task's skip counter; the schedule keeps its next window."""

        schedule_id = str(task.get("task_id") or "")
        try:
            current = self._schedule_store.read_task(schedule_id, now=self._now())
            if current.get("state") not in RUNNABLE_STATES:
                return
            current["skipped_count"] = min(
                MAX_SKIPS, int(current.get("skipped_count") or 0) + 1
            )
            current["last_skipped_for"] = run.scheduled_iso
            current["updated_at"] = _iso(self._now())
            self._schedule_store.write_task(current, now=self._now())
        except Exception as exc:  # noqa: BLE001 - the run row already explains it
            log(
                "proactive_skip_note_failed",
                error_code=_error_code(exc),
                status="warning",
            )

    def _pause_schedule(self, schedule_id: str) -> None:
        """Park the schedule until the owner decides; never auto-resume."""

        try:
            current = self._schedule_store.read_task(schedule_id, now=self._now())
            if current.get("state") not in RUNNABLE_STATES:
                return
            current["state"] = "paused_needs_approval"
            current["updated_at"] = _iso(self._now())
            self._schedule_store.write_task(current, now=self._now())
        except Exception as exc:  # noqa: BLE001 - the run row already says paused
            log(
                "proactive_pause_failed",
                error_code=_error_code(exc),
                status="error",
            )

    # -- audit -------------------------------------------------------------

    def _write_run_row(
        self,
        task: Mapping[str, Any],
        run: _ActiveRun,
        outcome: str,
        reason: str,
        duration_ms: int,
        *,
        turn_id: str | None = None,
        thread_id: str | None = None,
        usage: Mapping[str, Any] | None = None,
    ) -> None:
        """One content-free ``schedule_run`` row; the prompt never appears."""

        usage = usage or {}
        schedule = task.get("schedule") if isinstance(task.get("schedule"), Mapping) else {}
        extra = {
            "origin": SCHEDULE_ORIGIN,
            "on_behalf_of": "owner",
            "origin_ref": {"schedule_id": run.schedule_id, "run_id": run.run_id},
            "schedule_id": run.schedule_id,
            "run_id": run.run_id,
            "turn_id": turn_id,
            "thread_id": thread_id,
            "cron": (schedule or {}).get("expr"),
            "timezone": task.get("timezone"),
            "scheduled_for": run.scheduled_iso,
            "started_at": _iso(run.started_wall),
            "reason": reason,
            "input_tokens": _usage_int(usage.get("input_tokens")),
            "output_tokens": _usage_int(usage.get("output_tokens")),
            "estimated_cost_usd": _usage_cost(usage.get("estimated_cost_usd")),
            "cost_class": "chat",
        }
        try:
            self._audit.record(
                "schedule_run",
                actor=SCHEDULE_ORIGIN,
                turn_id=turn_id,
                outcome=outcome,
                duration_ms=max(0, int(duration_ms)),
                tool="schedule.create",
                extra=extra,
            )
        except Exception as exc:  # noqa: BLE001 - audit must not undo a run
            log(
                "proactive_audit_failed",
                error_code=_error_code(exc),
                status="error",
            )

    # -- boot reconciliation ----------------------------------------------

    def after_recovery(self, closed_turns: Sequence[str]) -> int:
        """Write the run row a kill prevented, without retrying the turn.

        Boot recovery settles the orphaned turn ``interrupted``. The scheduler
        had already claimed the window, so nothing will fire again. All that is
        missing is the run row for that fire; this writes exactly one, and only
        when no row for the trigger's run id exists yet.
        """

        written = 0
        known = self._known_run_ids()
        for turn_id in closed_turns:
            try:
                turn = self._store.get_turn(str(turn_id))
                if turn is None:
                    continue
                blocks = self._store.get_blocks(str(turn["user_message_id"]))
                trigger = next(
                    (block for block in blocks if block.get("type") == TRIGGER_BLOCK_TYPE),
                    None,
                )
                if trigger is None:
                    continue
                run_id = str(trigger.get("run_id") or "")
                schedule_id = str(trigger.get("schedule_id") or "")
                if not run_id or not schedule_id or run_id in known:
                    continue
                scheduled = str(trigger.get("scheduled_for") or "")
                self._audit.record(
                    "schedule_run",
                    actor=SCHEDULE_ORIGIN,
                    turn_id=str(turn_id),
                    outcome="interrupted",
                    duration_ms=0,
                    tool="schedule.create",
                    extra={
                        "origin": SCHEDULE_ORIGIN,
                        "on_behalf_of": "owner",
                        "origin_ref": {"schedule_id": schedule_id, "run_id": run_id},
                        "schedule_id": schedule_id,
                        "run_id": run_id,
                        "turn_id": str(turn_id),
                        "thread_id": turn.get("thread_id"),
                        "scheduled_for": scheduled,
                        "reason": "server_restart",
                        "recovered": True,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "estimated_cost_usd": None,
                        "cost_class": "chat",
                    },
                )
                known.add(run_id)
                written += 1
            except Exception as exc:  # noqa: BLE001 - recovery is best effort
                log(
                    "proactive_recovery_failed",
                    error_code=_error_code(exc),
                    status="error",
                )
        return written

    def _known_run_ids(self) -> set[str]:
        try:
            entries = self._audit.entries(event="schedule_run")
        except Exception:  # noqa: BLE001 - a missing log means nothing to dedupe
            return set()
        known: set[str] = set()
        for entry in entries:
            extra = entry.get("extra")
            if isinstance(extra, Mapping) and extra.get("run_id"):
                known.add(str(extra["run_id"]))
        return known

    # -- lifecycle ---------------------------------------------------------

    def wait_for_idle(self, timeout: float = 10.0) -> bool:
        """Block until no proactive run is in flight. Returns ``False`` on timeout."""

        deadline = self._monotonic() + max(0.0, timeout)
        with self._cond:
            while self._in_flight:
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True

    def close(self, timeout: float = 5.0) -> None:
        """Stop accepting fires and wait for in-flight turns to settle."""

        with self._cond:
            self._closed = True
            self._cond.notify_all()
        deadline = self._monotonic() + max(0.0, timeout)
        with self._cond:
            while self._in_flight:
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            leftover = list(self._futures.values())
        for future in leftover:
            cancel = getattr(future, "cancel", None)
            if callable(cancel):
                try:
                    cancel()
                except Exception:  # pragma: no cover - best effort
                    pass

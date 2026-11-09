"""TaskRun recorder (P3-1).

``docs/SERVICE_CONTRACT.md`` §3 freezes the operational record of one unit of
work: :class:`TaskRun`. It is durable execution metadata, never a copy of the
conversation. This module is the only writer of that record.

Rules enforced here (not by the caller):

* ``run_id`` is the external ``run_…`` id and ``turn_id`` the internal
  ``turn_…`` id. A run with no chat turn (scheduled, plugin, or system work)
  simply omits ``turn_id``.
* A run has exactly one terminal record. :meth:`RunRecorder.transition` refuses
  to move a terminal row, and the store repeats that guard inside its UPDATE so
  two processes racing a completion cannot both win.
* A replayed idempotency key returns the run that already exists instead of
  creating a second one.
* The terminal hook is the seam a later slice uses to emit a billing event. P3-1
  deliberately contains no billing code.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Mapping

from .store import (
    OWNER_TENANT_ID,
    RUN_TERMINAL_STATUSES,
    RUN_STATUSES,
    iso_now,
)
from .tenancy import validate_tenant_id

__all__ = [
    "RUN_STATUSES",
    "RUN_TERMINAL_STATUSES",
    "RunError",
    "RunNotFoundError",
    "RunRecorder",
    "RunStatus",
    "RunStatusError",
    "RunTerminalError",
    "TaskRun",
]


class RunStatus(str, Enum):
    """The eight frozen statuses, in the contract's order."""

    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    STOPPING = "stopping"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class RunError(Exception):
    """Base class for a refused run write."""


class RunStatusError(RunError, ValueError):
    """The requested status is not one of the frozen eight."""


class RunNotFoundError(RunError, LookupError):
    """No run with that id exists inside the caller's tenant."""


class RunTerminalError(RunError):
    """A terminal run was asked to change again (exactly-one-terminal rule)."""


def _coerce_status(value: RunStatus | str) -> RunStatus:
    if isinstance(value, RunStatus):
        return value
    try:
        return RunStatus(str(value))
    except ValueError:
        raise RunStatusError(
            f"status must be one of {', '.join(RUN_STATUSES)}."
        ) from None


def _iso(moment: datetime | None = None) -> str:
    value = moment or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _new_run_id() -> str:
    return f"run_{uuid.uuid4().hex}"


@dataclass(frozen=True)
class TaskRun:
    """One frozen ``TaskRun`` row, in contract field order.

    ``status`` is a :class:`RunStatus`; ``to_dict`` renders the plain JSON
    shape. ``plan`` is intentionally absent: the frozen P3 schema has no plan
    column, so the entitlement snapshot is a caller-side placeholder that is
    never persisted.
    """

    run_id: str
    tenant_id: str = OWNER_TENANT_ID
    thread_id: str | None = None
    turn_id: str | None = None
    surface: str = "web"
    task_type: str = "chat"
    status: RunStatus = RunStatus.QUEUED
    created_at: str = ""
    updated_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    model: str = ""
    provider: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    steps: int = 0
    tool_calls: int = 0
    approvals: int = 0
    error_code: str | None = None
    retry_of: str | None = None
    idempotency_key: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status in RUN_TERMINAL_STATUSES

    def to_dict(self) -> dict[str, Any]:
        """The plain, JSON-safe record in the contract's field order."""

        payload: dict[str, Any] = {}
        for field in fields(self):
            value = getattr(self, field.name)
            payload[field.name] = value.value if isinstance(value, RunStatus) else value
        return payload

    #: The admin read model is the same field set; keep one obvious spelling.
    to_record = to_dict

    def to_admin_row(self) -> dict[str, Any]:
        """The admin projection: identical, but no invented ``turn_id``.

        Contract §6: a run with no chat turn omits ``turn_id`` in an admin row
        instead of serializing a placeholder.
        """

        row = self.to_dict()
        if row.get("turn_id") is None:
            row.pop("turn_id", None)
        return row

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "TaskRun":
        values: dict[str, Any] = {}
        for field in fields(cls):
            if field.name in record:
                values[field.name] = record[field.name]
        if "status" in values:
            values["status"] = _coerce_status(values["status"])
        return cls(**values)


class RunRecorder:
    """Create and settle ``TaskRun`` rows through a :class:`ChatStore`.

    ``now`` is injectable so a test can pin ``created_at``/``updated_at``.
    ``on_terminal`` is invoked exactly once after a terminal transition wins;
    it is the future billing seam and its failure never reopens the run.
    """

    def __init__(
        self,
        store: Any,
        *,
        now: Callable[[], datetime] | None = None,
        on_terminal: Callable[[TaskRun], Any] | None = None,
    ):
        self.store = store
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._on_terminal = on_terminal

    def _now_iso(self) -> str:
        return _iso(self._now())

    # -- create ------------------------------------------------------------

    def create(
        self,
        *,
        tenant_id: str = OWNER_TENANT_ID,
        thread_id: str | None = None,
        turn_id: str | None = None,
        surface: str = "web",
        task_type: str = "chat",
        status: RunStatus | str = RunStatus.QUEUED,
        model: str = "",
        provider: str = "",
        retry_of: str | None = None,
        idempotency_key: str | None = None,
        plan: Mapping[str, Any] | None = None,
        run_id: str | None = None,
        created_at: str | None = None,
    ) -> TaskRun:
        """Insert one run, or return the run its idempotency key already owns.

        ``plan`` is the P5 entitlement-snapshot seam. The frozen P3 schema has
        no plan column, so it is accepted and deliberately not stored.
        """

        del plan  # reserved for P5; never persisted in P3
        tenant = validate_tenant_id(tenant_id)
        target = _coerce_status(status)
        if idempotency_key is not None:
            existing = self.store.find_run_by_idempotency(
                str(idempotency_key), tenant_id=tenant, thread_id=thread_id
            )
            if existing is not None:
                return TaskRun.from_record(existing)

        now = created_at or self._now_iso()
        record: dict[str, Any] = {
            "run_id": run_id or _new_run_id(),
            "tenant_id": tenant,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "surface": surface,
            "task_type": task_type,
            "status": target.value,
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "finished_at": None,
            "model": model,
            "provider": provider,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "steps": 0,
            "tool_calls": 0,
            "approvals": 0,
            "error_code": None,
            "retry_of": retry_of,
            "idempotency_key": idempotency_key,
        }
        if target in RUN_TERMINAL_STATUSES:
            record["finished_at"] = now
        try:
            stored = self.store.create_run(record)
        except Exception:
            # A concurrent submission can win the unique idempotency index
            # between the lookup above and the insert. The row that won is the
            # correct answer; anything else is a real failure.
            if idempotency_key is not None:
                existing = self.store.find_run_by_idempotency(
                    str(idempotency_key), tenant_id=tenant, thread_id=thread_id
                )
                if existing is not None:
                    return TaskRun.from_record(existing)
            raise
        return TaskRun.from_record(stored)

    # -- transition --------------------------------------------------------

    def transition(
        self,
        run_id: str,
        status: RunStatus | str,
        *,
        tenant_id: str = OWNER_TENANT_ID,
        **fields: Any,
    ) -> TaskRun:
        """Move a non-terminal run and return its new state.

        A terminal run is immutable: asking again raises
        :class:`RunTerminalError`. ``finished_at`` is stamped automatically for
        a terminal target when the caller does not supply it, and
        ``started_at`` for ``running``.
        """

        tenant = validate_tenant_id(tenant_id)
        target = _coerce_status(status)
        changes = dict(fields)
        if target in RUN_TERMINAL_STATUSES and "finished_at" not in changes:
            changes["finished_at"] = self._now_iso()
        if target is RunStatus.RUNNING and "started_at" not in changes:
            changes["started_at"] = self._now_iso()
        updated = self.store.update_run(
            run_id, tenant_id=tenant, status=target.value, **changes
        )
        if updated is None:
            current = self.store.get_run(run_id, tenant_id=tenant)
            if current is None:
                raise RunNotFoundError(run_id)
            if current["status"] in RUN_TERMINAL_STATUSES:
                raise RunTerminalError(
                    f"run {run_id} is already terminal ({current['status']})."
                )
            # A non-terminal row was visible but no longer matched: another
            # writer moved it in the same instant. Treat it as a lost race.
            raise RunError(f"run {run_id} changed before the transition landed.")
        run = TaskRun.from_record(updated)
        if target in RUN_TERMINAL_STATUSES and self._on_terminal is not None:
            try:
                self._on_terminal(run)
            except Exception:  # noqa: BLE001 - billing must not undo the record
                pass
        return run

    #: ``close`` reads better at a call site that is settling a finished turn.
    close = transition

    # -- reads -------------------------------------------------------------

    def get(self, run_id: str, *, tenant_id: str = OWNER_TENANT_ID) -> TaskRun | None:
        record = self.store.get_run(run_id, tenant_id=tenant_id)
        return TaskRun.from_record(record) if record is not None else None

    def for_turn(
        self, turn_id: str, *, tenant_id: str = OWNER_TENANT_ID
    ) -> TaskRun | None:
        record = self.store.get_run_by_turn(turn_id, tenant_id=tenant_id)
        return TaskRun.from_record(record) if record is not None else None

    def find_by_idempotency(
        self,
        idempotency_key: str,
        *,
        tenant_id: str = OWNER_TENANT_ID,
        thread_id: str | None = None,
    ) -> TaskRun | None:
        record = self.store.find_run_by_idempotency(
            idempotency_key, tenant_id=tenant_id, thread_id=thread_id
        )
        return TaskRun.from_record(record) if record is not None else None

    def list(
        self,
        *,
        tenant_id: str = OWNER_TENANT_ID,
        thread_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[TaskRun]:
        return [
            TaskRun.from_record(record)
            for record in self.store.list_runs(
                tenant_id=tenant_id,
                thread_id=thread_id,
                status=status,
                limit=limit,
            )
        ]

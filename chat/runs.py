"""TaskRun recorder and runner (P3-1, P3-2).

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

P3-2 adds :class:`TaskRunner`, the submission boundary for ``POST /api/runs``.
It creates a thread when the caller names none, hands the prompt to
:meth:`ChatService.send`, and returns the :class:`TaskRun`. The prompt is passed
through and dropped: neither the recorder, the run row, nor any read method here
can carry it back out.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Mapping

from .errors import ChatError
from .store import (
    OWNER_TENANT_ID,
    RUN_TERMINAL_STATUSES,
    RUN_STATUSES,
    iso_now,
)
from .tenancy import SURFACES, tenant_for, validate_tenant_id

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
    "TaskRunner",
]

#: ``chat.service._MAX_TEXT_CHARS``. Duplicated rather than imported because the
#: service imports this module; the two must stay in step for a prompt to reach
#: the provider with the same bound the interactive send applies.
_PROMPT_MAX_CHARS = 8000

#: A caller-supplied idempotency key becomes ``client_turn_id`` on the send, so
#: it shares that field's alphabet (see ``chat.routers._util._CLIENT_TURN_RE``).
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")

#: A controlled task label: lowercase, short, and free of path or log syntax.
_TASK_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

#: The store's ``list_runs`` caps one query at 500 rows. A replay lookup that
#: has no thread to scope it reads the same newest-first window.
_RUN_WINDOW = 500


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


def _prompt_text(text: str | None, prompt: str | None) -> str:
    """The one prompt value a submission carries, validated before any write.

    ``text`` and ``prompt`` are two spellings of the same field, so a caller may
    send either but not two different bodies. Validation happens here - before
    a thread is minted - so a malformed submission cannot leave an empty thread
    behind.
    """

    if text is not None and prompt is not None and text != prompt:
        raise ChatError(
            "invalid_request",
            status=400,
            message="Give either text or prompt, not both.",
            field="/text",
            user_fixable=True,
        )
    value = text if text is not None else prompt
    if not isinstance(value, str):
        raise ChatError(
            "invalid_message",
            status=400,
            message="A run needs text.",
            field="/text",
            user_fixable=True,
        )
    message = value.strip()
    if not 1 <= len(message) <= _PROMPT_MAX_CHARS:
        raise ChatError(
            "invalid_message",
            status=400,
            message=(
                f"A message is 1-{_PROMPT_MAX_CHARS} characters after trimming."
            ),
            field="/text",
            user_fixable=True,
        )
    return message


def _idempotency_key(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _IDEMPOTENCY_KEY_RE.match(value):
        raise ChatError(
            "invalid_request",
            status=400,
            message=(
                "idempotency_key must be 1-100 letters, digits, underscores, "
                "or hyphens."
            ),
            field="/idempotency_key",
            user_fixable=True,
        )
    return value


def _surface(value: Any, default: str) -> str:
    surface = default if value is None else value
    if not isinstance(surface, str) or surface not in SURFACES:
        raise ChatError(
            "invalid_request",
            status=400,
            message=f"surface must be one of {', '.join(SURFACES)}.",
            field="/surface",
            user_fixable=True,
        )
    return surface


def _task_type(value: Any) -> str:
    label = "chat" if value is None else value
    if not isinstance(label, str) or not _TASK_TYPE_RE.match(label):
        raise ChatError(
            "invalid_request",
            status=400,
            message=(
                "task_type must be lowercase letters, digits, or underscores "
                "(max 32 characters)."
            ),
            field="/task_type",
            user_fixable=True,
        )
    return label


class TaskRunner:
    """Submit one task run and read runs back (P3-2).

    ``start`` is the submission path behind ``POST /api/runs``: it reuses the
    thread the caller named or mints one, then calls :meth:`ChatService.send`,
    which owns the turn, the spend gate, and the provider call. Everything this
    class returns is run state; the prompt is an argument and is never stored on
    or read back from a :class:`TaskRun`.

    Idempotency keeps the P3-1 scope, ``tenant + thread``. When the caller names
    no thread, the replay lookup first asks the store's indexed lookup for a run
    without a thread and then scans the tenant's newest :data:`_RUN_WINDOW`
    rows; that is what lets a replayed ``POST /api/runs`` return the original
    run and thread instead of minting a second one. Two *concurrent* first
    submissions with the same key can still mint two threads, because each
    thread is a distinct idempotency scope - the same rule a repeated client
    turn id on a new thread already follows.
    """

    def __init__(self, service: Any, *, recorder: RunRecorder | None = None):
        self.service = service
        # ``ChatService`` builds the recorder its own sends write through, so a
        # terminal hook installed there is shared with runs started here.
        self.recorder = recorder or getattr(service, "_runs", None) or RunRecorder(
            getattr(service, "store")
        )
        self.store = getattr(service, "store", None) or self.recorder.store

    # -- submission --------------------------------------------------------

    async def start(
        self,
        *,
        text: str | None = None,
        prompt: str | None = None,
        thread_id: str | None = None,
        idempotency_key: str | None = None,
        surface: str | None = None,
        task_type: str | None = None,
        tenant: Any = None,
    ) -> TaskRun:
        """Start one run, or return the run a replayed key already owns."""

        context = tenant_for(tenant)
        tenant_id = context.tenant_id
        message = _prompt_text(text, prompt)
        key = _idempotency_key(idempotency_key)
        label = _task_type(task_type)
        run_surface = _surface(surface, context.surface)

        if thread_id is not None and (
            not isinstance(thread_id, str) or not thread_id.strip()
        ):
            raise ChatError(
                "invalid_request",
                status=400,
                message="thread_id must be a non-empty string.",
                field="/thread_id",
                user_fixable=True,
            )

        if key is not None:
            replay = self._find_replay(key, tenant_id=tenant_id, thread_id=thread_id)
            if replay is not None:
                return replay

        target_thread = thread_id or str(
            self.service.create_thread(tenant=context)["id"]
        )
        payload, _created = await self.service.send(
            target_thread,
            text=message,
            client_turn_id=key,
            tenant=context,
            surface=run_surface,
            task_type=label,
        )
        run_id = payload.get("run_id")
        run = (
            self.recorder.get(str(run_id), tenant_id=tenant_id)
            if run_id is not None
            else None
        )
        if run is None:
            run = self.recorder.for_turn(
                str(payload.get("turn_id", "")), tenant_id=tenant_id
            )
        if run is None:
            # ``send`` created a turn but its run row is missing: an invariant
            # break, reported as a generic failure and never retried blindly.
            raise RunError("the turn started but its run row is missing")
        return run

    def _find_replay(
        self, key: str, *, tenant_id: str, thread_id: str | None
    ) -> TaskRun | None:
        if thread_id is not None:
            return self.recorder.find_by_idempotency(
                key, tenant_id=tenant_id, thread_id=thread_id
            )
        exact = self.recorder.find_by_idempotency(key, tenant_id=tenant_id)
        if exact is not None:
            return exact
        for record in self.store.list_runs(tenant_id=tenant_id, limit=_RUN_WINDOW):
            if str(record.get("idempotency_key") or "") == key:
                return TaskRun.from_record(record)
        return None

    # -- reads -------------------------------------------------------------

    def get(self, run_id: str, *, tenant_id: str = OWNER_TENANT_ID) -> TaskRun | None:
        """The run, only when it belongs to the caller's tenant."""

        return self.recorder.get(run_id, tenant_id=validate_tenant_id(tenant_id))

    def list(
        self,
        *,
        tenant_id: str = OWNER_TENANT_ID,
        thread_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TaskRun]:
        """One page of a tenant's runs, newest first.

        ``offset`` pages through the store's bounded newest-first window rather
        than a cursor: ``runs`` has a created-at index but no pagination key, and
        the window is the same 500 rows the replay lookup reads.
        """

        tenant = validate_tenant_id(tenant_id)
        target: RunStatus | None = None
        if status is not None:
            try:
                target = _coerce_status(status)
            except RunStatusError:
                raise ChatError(
                    "invalid_request",
                    status=400,
                    message=f"status must be one of {', '.join(RUN_STATUSES)}.",
                    field="/status",
                    user_fixable=True,
                ) from None
        try:
            window = max(1, int(limit))
        except (TypeError, ValueError):
            window = 100
        window = min(window, _RUN_WINDOW)
        try:
            start = max(0, int(offset))
        except (TypeError, ValueError):
            start = 0
        fetch = min(_RUN_WINDOW, start + window)
        records = self.store.list_runs(
            tenant_id=tenant,
            thread_id=thread_id,
            status=target.value if target is not None else None,
            limit=fetch,
        )
        return [
            TaskRun.from_record(record)
            for record in records[start : start + window]
        ]

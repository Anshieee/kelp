"""Chat orchestration around the kernel loop (KEL-8).

One turn: validate and persist the user message, run ``AgentLoop``, translate
its typed events into the chat wire log, derive cards from envelopes, and close
the turn exactly once. Approvals come from two places and share one card:
``skills.context.ApprovalRequest`` (skill hook) and KEL-7
``openshell.ApprovalStore`` objects (exec gate). Budgets are the kernel's
``TurnBudget``; this layer never invents its own limits.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping, Sequence

from kernel import (
    AgentLoop,
    CancelToken,
    KernelConfig,
    RegistryToolExecutor,
    TurnBudget,
)
from kernel import events as kernel_events
from kernel.usage import SpendGuard, SpendState, UsageStore, state_payload
from openshell.approvals import ApprovalStore
from openshell.audit import AuditLog
from openshell.errors import (
    ApprovalAlreadyUsed,
    ApprovalDenied,
    ApprovalExpired,
    ApprovalNotGranted,
    ApprovalNotFound,
)
from skills.context import ApprovalRequest as SkillApprovalRequest
from skills.loader import SkillRegistry

from . import cards
from .artifacts import HANDLE_RE, ArtifactError, ArtifactRegistry
from .errors import ChatError
from .logging import current_request_id, log
from .metrics import Metrics, get_metrics
from .store import (
    APPROVAL_TRANSITIONS,
    LIVE_TURN_STATES,
    ChatStore,
    ThreadBusyError,
    iso_now,
    new_id,
)
from .stream import EventBus

__all__ = ["ChatService", "MESSAGE_STATUS_BY_REASON", "TurnBudget"]

MESSAGE_STATUS_BY_REASON = {
    "stop": "complete",
    "length": "complete",
    "budget": "error",
    "error": "error",
    "cancelled": "stopped",
    "interrupted": "interrupted",
}

_TERMINAL_RETRY_REASONS = frozenset({"error", "stopped", "interrupted"})
_MAX_TEXT_CHARS = 8000
_MAX_ATTACHMENTS = 5
_MAX_DECISION_NOTE = 200

# Stop: the cooperative token is enough for a normal turn, but a provider
# stream that does not yield cannot see it, so a hard cancel is armed. While a
# human decision is in flight the hammer is deferred: cancelling there could
# leave an approved card with no waiter left to consume it.
_HARD_CANCEL_DELAY = 0.05
_HARD_CANCEL_RETRY = 0.25
_HARD_CANCEL_ATTEMPTS = 40
_APPROVAL_POLL_SECONDS = 0.05

#: A tool name and an error code reach logs, audit entries, and metric labels.
#: Both are bounded so a model-authored string cannot create unbounded label
#: cardinality or a multi-kilobyte log line; a name outside the offered set
#: collapses to one fixed bucket instead of becoming its own series.
_MAX_TOOL_NAME_CHARS = 64
_MAX_ERROR_CODE_CHARS = 64
_UNKNOWN_TOOL = "unknown"
#: Error codes are internal tokens (``provider_error``, ``RuntimeError``). A
#: provider-authored value that does not look like one is replaced by the fixed
#: ``unknown`` bucket: the code reaches audit and log sinks, and only a
#: verified shape belongs there. A leading underscore is allowed so a private
#: exception class still logs as its class name.
_ERROR_CODE_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._:-]*")


def _declared_tool_name(spec: Any) -> str | None:
    """Read the tool name from a model-facing spec or a plain declaration."""

    if not isinstance(spec, Mapping):
        return None
    name = spec.get("name")
    if not isinstance(name, str):
        function = spec.get("function")
        name = function.get("name") if isinstance(function, Mapping) else None
    if not isinstance(name, str):
        return None
    name = name.strip()
    return name or None


def _shorten(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit]


def _error_code(value: Any) -> str | None:
    """A bounded, inert error code, or ``None`` when the value is unusable."""

    if not isinstance(value, str):
        return None
    code = value.strip()
    if not code:
        return None
    if not _ERROR_CODE_RE.fullmatch(code):
        return "unknown"
    return _shorten(code, _MAX_ERROR_CODE_CHARS)


@dataclass
class _TurnState:
    turn: dict[str, Any]
    thread_id: str
    user_id: str
    assistant_id: str
    model: str
    blocks: list[dict[str, Any]] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    sources_total: int = 0
    text_open: bool = False
    steps: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float | None = None
    usage_persisted: bool = False
    finished: bool = False
    started_mono: float = 0.0
    #: Last kernel or executor error code seen for this turn, used only for
    #: logs and audit (never for a metric label).
    error_code: str | None = None


class ChatService:
    def __init__(
        self,
        *,
        store: ChatStore,
        provider: Any | None = None,
        registry: SkillRegistry | None = None,
        artifacts: ArtifactRegistry | None = None,
        approvals: ApprovalStore | None = None,
        usage_store: UsageStore | None = None,
        budget: TurnBudget | None = None,
        model: str | None = None,
        config: KernelConfig | None = None,
        secrets: Mapping[str, str] | None = None,
        workspace_root: str | None = None,
        tool_executor_factory: Callable[[Any], Any] | None = None,
        now: Callable[[], datetime] | None = None,
        approval_ttl_seconds: float = 600.0,
        tools: Sequence[Mapping[str, Any]] = (),
        audit: AuditLog | None = None,
        metrics: Metrics | None = None,
        spend_guard: SpendGuard | None = None,
    ):
        self.store = store
        self.bus = EventBus()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.approvals = approvals or ApprovalStore()
        self.artifacts = artifacts or ArtifactRegistry(
            store,
            store.path.parent / "artifacts",
            now=lambda: iso_now(self._now()),
            workspace_root=workspace_root,
        )
        self._provider = provider
        self._registry = registry
        self._usage_store = usage_store
        self._audit = audit
        self._metrics = metrics or get_metrics()
        self._spend = spend_guard
        self._budget = budget or TurnBudget()
        self._model = model or (config or KernelConfig.from_env()).primary_model
        self._secrets = dict(secrets or {})
        self._secret_values = tuple(
            value for value in self._secrets.values() if isinstance(value, str) and len(value) >= 4
        )
        self._workspace_root = workspace_root
        self._tool_executor_factory = tool_executor_factory
        self._approval_ttl = float(approval_ttl_seconds)
        self._tools = list(tools)
        #: Names the model was actually offered. Anything else in a tool-call
        #: event is model-authored and is bucketed as ``unknown`` for every
        #: observability sink.
        self._offered_tools = frozenset(
            name for name in (_declared_tool_name(spec) for spec in self._tools) if name
        )
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._cancel: dict[str, CancelToken] = {}
        self._done: dict[str, asyncio.Event] = {}
        self._waiters: dict[str, asyncio.Event] = {}
        self._waiter_loops: dict[str, asyncio.AbstractEventLoop] = {}
        self._waiting_approval: dict[str, str] = {}
        self._live_blocks: dict[str, list[dict[str, Any]]] = {}
        self._closing = False

    # -- audit -------------------------------------------------------------

    def _record_audit(self, event: str, **fields: Any) -> None:
        """Audit without ever failing a turn: accounting is not traffic."""

        if self._audit is None:
            return
        try:
            self._audit.record(event, **fields)
        except Exception:  # noqa: BLE001 - a full disk must not end a turn
            return

    # -- spend guardrail ---------------------------------------------------

    def refresh_spend_metrics(self, now: datetime | None = None) -> SpendState | None:
        """Publish the persisted spend totals so a scrape sees them at boot."""

        if self._spend is None:
            return None
        state = self._spend.evaluate(now)
        for window in state.windows:
            self._metrics.observe_spend(
                window.name,
                used_usd=window.used_usd,
                limit_usd=window.limit_usd,
                ratio=window.ratio,
                unknown_price_records=window.unknown_price_records,
            )
        return state

    def publish_spend_metrics(self, now: datetime | None = None) -> SpendState | None:
        """Republish the spend gauges without ever failing the caller's work.

        A settling turn must finish even if its accounting cannot be read; the
        next gate and the next scrape retry from the persisted records.
        """

        try:
            return self.refresh_spend_metrics(now)
        except Exception:  # noqa: BLE001 - accounting is not traffic
            return None

    def _refuse_if_over_spend(self, *, action: str) -> None:
        """Gate a new turn on the persisted day/month spend.

        Runs before a turn row or message exists, so a refused request leaves
        the thread exactly as it was and no provider call is made. The 80%
        warning is emitted once per window: the marker is persisted by the
        guard, so a restart inside the same window does not repeat it.
        """

        if self._spend is None:
            return
        state = self.refresh_spend_metrics()
        if state is None:  # pragma: no cover - only when no guard is wired
            return
        request_id = current_request_id()
        if state.warnings:
            for name in state.warnings:
                window = state.day if name == "day" else state.month
                self._metrics.inc_spend_warning(name)
                log(
                    "spend_cap_warning",
                    request_id=request_id,
                    window=name,
                    used_usd=round(window.used_usd, 8),
                    limit_usd=window.limit_usd,
                    ratio=None if window.ratio is None else round(window.ratio, 6),
                    status="warning",
                )
                self._record_audit(
                    "spend_cap_warning",
                    request_id=request_id,
                    outcome="warning",
                    extra={
                        "window": name,
                        "used_usd": round(window.used_usd, 8),
                        "limit_usd": window.limit_usd,
                    },
                )
            self._spend.mark_warned(state)
        if not state.blocked:
            return
        window_name = state.block_window or "month"
        payload = state_payload(state)
        self._metrics.inc_spend_blocked(window_name)
        log(
            "spend_cap_blocked",
            request_id=request_id,
            window=window_name,
            reason=state.block_reason,
            used_usd=round(
                (state.day if window_name == "day" else state.month).used_usd, 8
            ),
            limit_usd=(
                state.day if window_name == "day" else state.month
            ).limit_usd,
            status="blocked",
        )
        self._record_audit(
            "spend_cap_blocked",
            request_id=request_id,
            outcome="blocked",
            extra={
                "action": action,
                "window": window_name,
                "reason": state.block_reason,
                "unknown_price_mode": state.unknown_price_mode,
                "windows": payload["windows"],
            },
        )
        if state.block_reason == "unknown_price":
            message = (
                "Spending can't be measured for this period, so new messages "
                "are paused."
            )
        elif state.block_reason == "usage_unavailable":
            message = (
                "Spending can't be checked right now, so new messages are paused."
            )
        else:
            message = (
                "The spending limit for this period is reached. New messages "
                "resume when the limit resets."
            )
        raise ChatError(
            "spend_cap_reached",
            status=402,
            message=message,
            retryable=False,
            details={
                "window": window_name,
                "reason": state.block_reason,
                "unknown_price_mode": state.unknown_price_mode,
                "unknown_price_usd": state.unknown_price_usd,
                "windows": payload["windows"],
            },
        )

    # -- observability labels ---------------------------------------------

    def _observable_tool(self, name: Any) -> str:
        """The tool name as logs, audit, and metric labels may carry it.

        Tool-call names come from the model, not from the operator. A name that
        was not offered collapses to one fixed bucket so a crafted or mistyped
        name cannot mint a new metric series, and every value is length-capped
        before it is written anywhere. A service built without model-facing
        specs therefore labels every call ``unknown``: it cannot verify one.
        """

        if isinstance(name, str) and name in self._offered_tools:
            return _shorten(name, _MAX_TOOL_NAME_CHARS)
        return _UNKNOWN_TOOL

    # -- threads -----------------------------------------------------------

    def create_thread(self, title: str | None = None) -> dict[str, Any]:
        source = "user" if title and title.strip() else "auto"
        return self.store.create_thread(
            title=(title or "").strip() or "New chat", title_source=source
        )

    def list_threads(
        self, *, limit: int = 50, cursor: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        return self.store.list_threads(limit=limit, cursor=cursor)

    def get_thread(
        self, thread_id: str, *, limit: int = 50, before: str | None = None
    ) -> dict[str, Any]:
        thread, messages, last_seq, has_more = self.store.snapshot(
            thread_id, limit=limit, before=before
        )
        if thread is None:
            raise ChatError("unknown_thread", status=404)
        return {
            "thread": thread,
            "messages": messages,
            "last_seq": last_seq,
            "has_more": has_more,
        }

    def rename_thread(self, thread_id: str, title: str) -> dict[str, Any]:
        clean = title.strip() if isinstance(title, str) else ""
        if not 1 <= len(clean) <= 80:
            raise ChatError(
                "invalid_title",
                status=400,
                message="A thread title is 1-80 characters.",
                user_fixable=True,
                field="/title",
            )
        thread = self.store.rename_thread(thread_id, clean)
        if thread is None:
            raise ChatError("unknown_thread", status=404)
        self._emit(
            thread_id,
            "thread_updated",
            {
                "title": thread["title"],
                "title_source": thread["title_source"],
                "updated_at": thread["updated_at"],
            },
        )
        return thread

    async def delete_thread(self, thread_id: str) -> dict[str, Any]:
        thread = self.store.get_thread(thread_id)
        if thread is None:
            raise ChatError("unknown_thread", status=404)
        active = thread.get("active_turn_id")
        if active:
            task = self._tasks.get(active)
            try:
                await self.stop(active)
                if task is not None:
                    await self.wait_for_turn(active, timeout=2.0)
            except (ChatError, asyncio.TimeoutError):
                pass
            # Never purge under a live turn: its finalize step appends events
            # and updates rows that no longer exist, which would leave orphans
            # behind every successful delete.
            current = self.store.get_turn(active)
            if current is not None and current["state"] in LIVE_TURN_STATES:
                if task is not None and not task.done():
                    raise ChatError(
                        "turn_in_progress",
                        status=409,
                        message="The turn is still settling; retry the delete.",
                        details={"active_turn_id": active},
                    )
                # No live task: the turn is a leftover from a crash or a Stop
                # that never resumed. Settle it the way boot recovery does, so
                # a dead turn cannot make its thread undeletable.
                self.recover_interrupted()
        try:
            result = self.store.purge_thread(
                thread_id,
                on_artifacts=self.artifacts.purge,
            )
        except ThreadBusyError as exc:
            # The store re-checks inside the delete transaction. Reaching this
            # means a turn became live after the settle above (a retry or a
            # concurrent send); nothing was deleted.
            raise ChatError(
                "turn_in_progress",
                status=409,
                message="The turn is still settling; retry the delete.",
                details={"active_turn_id": exc.turn_ids[0]},
            ) from None
        self.bus.close(thread_id)
        return {"ok": True, **result}

    # -- turns -------------------------------------------------------------

    async def send(
        self,
        thread_id: str,
        *,
        text: str,
        client_turn_id: str | None = None,
        attachment_handles: Sequence[str] = (),
    ) -> tuple[dict[str, Any], bool]:
        """Accept a user message and start a turn. Returns ``(payload, created)``."""

        thread = self.store.get_thread(thread_id)
        if thread is None:
            raise ChatError("unknown_thread", status=404)

        if client_turn_id:
            existing = self.store.find_turn_by_client_id(thread_id, client_turn_id)
            if existing is not None:
                stored = self.store.get_message(existing["user_message_id"])
                accepted = _message_text(stored) if stored is not None else text
                return self._send_payload(existing, accepted, idempotent=True), False

        message = text.strip() if isinstance(text, str) else ""
        if not 1 <= len(message) <= _MAX_TEXT_CHARS:
            raise ChatError(
                "invalid_message",
                status=400,
                message=f"A message is 1-{_MAX_TEXT_CHARS} characters after trimming.",
                user_fixable=True,
                field="/text",
            )
        handles = list(attachment_handles or ())
        if len(handles) > _MAX_ATTACHMENTS:
            raise ChatError(
                "invalid_message",
                status=400,
                message=f"At most {_MAX_ATTACHMENTS} attachments per message.",
                user_fixable=True,
                field="/attachment_handles",
            )
        attachment_cards = []
        for handle in handles:
            if not isinstance(handle, str) or not HANDLE_RE.match(handle):
                raise ChatError(
                    "invalid_message",
                    status=400,
                    message="An attachment handle is not valid.",
                    user_fixable=True,
                    field="/attachment_handles",
                )
            try:
                meta = self.artifacts.get(handle, thread_id=thread_id)
            except ArtifactError:
                raise ChatError(
                    "invalid_message",
                    status=400,
                    message="An attachment is no longer available.",
                    user_fixable=True,
                    field="/attachment_handles",
                ) from None
            attachment_cards.append(meta)

        if thread.get("active_turn_id"):
            raise ChatError(
                "turn_in_progress",
                status=409,
                details={"active_turn_id": thread["active_turn_id"]},
            )

        # Before a turn or a message exists: a capped request leaves the
        # thread untouched and never reaches the provider.
        self._refuse_if_over_spend(action="send")

        turn_id = new_id("turn")
        user_id = new_id("msg")
        assistant_id = new_id("msg")
        user_blocks: list[dict[str, Any]] = [
            _attachment_block(meta, thread_id) for meta in attachment_cards
        ]
        user_blocks.append({"type": "text", "text": message})

        with self.store.transaction():
            for meta in attachment_cards:
                if meta.get("thread_id") is None:
                    self.artifacts.claim(meta["handle"], thread_id)
            self.store.create_message(
                thread_id=thread_id,
                turn_id=turn_id,
                role="user",
                status="pending",
                blocks=user_blocks,
                message_id=user_id,
            )
            self.store.create_message(
                thread_id=thread_id,
                turn_id=turn_id,
                role="assistant",
                status="streaming",
                blocks=[],
                message_id=assistant_id,
            )
            turn = self.store.create_turn(
                thread_id=thread_id,
                user_message_id=user_id,
                assistant_message_id=assistant_id,
                client_turn_id=client_turn_id,
                budget=self._budget_dict(),
                turn_id=turn_id,
            )
            self.store.update_turn(turn_id, state="queued")
            self._emit(
                thread_id,
                "message_created",
                {"turn_id": turn_id, "message": self.store.get_message(user_id)},
            )
            self._emit(
                thread_id,
                "message_created",
                {"turn_id": turn_id, "message": self.store.get_message(assistant_id)},
            )
            thread_record = self.store.get_thread(thread_id)
            if thread_record and thread_record["title_source"] == "auto":
                title = " ".join(message.split())[:60]
                if thread_record["message_count"] <= 2 and title:
                    if self.store.set_auto_title(thread_id, title):
                        updated = self.store.get_thread(thread_id)
                        self._emit(
                            thread_id,
                            "thread_updated",
                            {
                                "title": updated["title"],
                                "title_source": updated["title_source"],
                                "updated_at": updated["updated_at"],
                            },
                        )
            self.store.touch_thread(thread_id)

        # Publish only after the transaction commits: a tailing stream that
        # wakes early re-reads the log and waits for the next signal.
        self.bus.publish(thread_id)
        self._done[turn_id] = asyncio.Event()
        self._cancel[turn_id] = CancelToken()
        task = asyncio.create_task(self._run_turn(turn_id, message))
        self._tasks[turn_id] = task
        task.add_done_callback(lambda finished: self._consume_task_error(finished))
        return self._send_payload(turn, message, idempotent=False), True

    def _send_payload(
        self, turn: Mapping[str, Any], text: str, *, idempotent: bool
    ) -> dict[str, Any]:
        return {
            "turn_id": turn["id"],
            "user_message_id": turn["user_message_id"],
            "assistant_message_id": turn["assistant_message_id"],
            "stream_started": True,
            "text": text.strip() if isinstance(text, str) else text,
            "idempotent": idempotent,
        }

    async def stop(self, turn_id: str) -> dict[str, Any]:
        turn = self.store.get_turn(turn_id)
        if turn is None:
            raise ChatError("unknown_turn", status=404)
        if turn["state"] not in {"queued", "running", "stopping"}:
            raise ChatError("turn_not_running", status=409)
        token = self._cancel.get(turn_id)
        if token is not None:
            token.cancel()
        task = self._tasks.get(turn_id)
        if task is not None and not task.done():
            self._arm_hard_cancel(turn_id, task)
        self.store.update_turn(turn_id, state="stopping")
        return {"turn_id": turn_id, "status": "stopping"}

    def _arm_hard_cancel(
        self,
        turn_id: str,
        task: "asyncio.Task[Any]",
        attempt: int = 0,
    ) -> None:
        """Cancel a turn that ignores the cooperative token — but never mid-approval.

        A hard cancel while the approval hook awaits a human can lose the
        decision: the turn unwinds and the card stays ``approved`` with no
        waiter left to consume it. The approval wait watches the token itself,
        so the hammer is deferred for as long as one is in flight.
        """

        loop = asyncio.get_running_loop()

        def fire() -> None:
            if task.done():
                return
            if self._waiting_approval.get(turn_id):
                if attempt < _HARD_CANCEL_ATTEMPTS:
                    loop.call_later(
                        _HARD_CANCEL_RETRY,
                        lambda: self._arm_hard_cancel(turn_id, task, attempt + 1),
                    )
                return
            task.cancel()

        loop.call_later(_HARD_CANCEL_DELAY, fire)

    async def retry(self, turn_id: str) -> dict[str, Any]:
        turn = self.store.get_turn(turn_id)
        if turn is None:
            raise ChatError("unknown_turn", status=404)
        thread_id = turn["thread_id"]
        latest = self.store.latest_turn(thread_id)
        if latest is None or latest["id"] != turn_id:
            raise ChatError("not_latest_turn", status=409)
        if turn["state"] != "finished" or turn["reason"] not in _TERMINAL_RETRY_REASONS:
            raise ChatError("turn_not_retryable", status=409)

        user_message = self.store.get_message(turn["user_message_id"])
        if user_message is None:  # pragma: no cover - internal invariant
            raise ChatError("unknown_turn", status=404)
        text = _message_text(user_message)
        # A retry opens a new turn, so it is a new spend decision too.
        self._refuse_if_over_spend(action="retry")
        # A retry never duplicates the user message: it opens a new assistant
        # message and a new turn over the same question (§5.4).
        thread_id = turn["thread_id"]
        new_turn_id = new_id("turn")
        assistant_id = new_id("msg")
        with self.store.transaction():
            self.store.create_message(
                thread_id=thread_id,
                turn_id=new_turn_id,
                role="assistant",
                status="streaming",
                blocks=[],
                message_id=assistant_id,
            )
            self.store.create_turn(
                thread_id=thread_id,
                user_message_id=turn["user_message_id"],
                assistant_message_id=assistant_id,
                client_turn_id=None,
                budget=self._budget_dict(),
                retry_of=turn_id,
                turn_id=new_turn_id,
            )
            self.store.update_turn(new_turn_id, state="queued")
            self.store.update_message(
                turn["assistant_message_id"], superseded_by=new_turn_id
            )
            self._emit(
                thread_id,
                "message_created",
                {
                    "turn_id": new_turn_id,
                    "message": self.store.get_message(assistant_id),
                },
            )
            self.store.touch_thread(thread_id)

        self.bus.publish(thread_id)
        self._done[new_turn_id] = asyncio.Event()
        self._cancel[new_turn_id] = CancelToken()
        task = asyncio.create_task(self._run_turn(new_turn_id, text))
        self._tasks[new_turn_id] = task
        task.add_done_callback(lambda finished: self._consume_task_error(finished))
        return {"turn_id": new_turn_id, "retry_of": turn_id}

    async def wait_for_turn(self, turn_id: str, timeout: float = 10.0) -> None:
        event = self._done.get(turn_id)
        if event is None:
            return
        await asyncio.wait_for(event.wait(), timeout=timeout)

    # -- shutdown ----------------------------------------------------------

    async def aclose(self, *, timeout: float = 10.0) -> None:
        """Cancel in-flight turns and settle them as ``interrupted`` (§10.4).

        The server calls this on shutdown *before* the API loop and the store
        are closed, so a turn that was streaming when ``SIGTERM`` arrived ends
        with ``reason: "interrupted"`` — the same outcome boot recovery
        produces — instead of being destroyed mid-flight or reported as a
        client-requested ``cancelled``. The flag is set first because a hard
        cancel only lands in :meth:`_run_turn` once it fires.

        Cancellation is awaited, never fire-and-forget: a task left pending
        when the loop closes is exactly the "Task was destroyed" warning this
        exists to prevent.
        """

        self._closing = True
        tasks = [task for task in self._tasks.values() if not task.done()]
        for token in self._cancel.values():
            token.cancel()
        for task in tasks:
            task.cancel()
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=timeout)
            for task in pending:  # pragma: no cover - finalize is synchronous
                task.cancel()
        # Safety net for a turn marked ``running`` with no live task: close it
        # exactly as boot recovery would. Live tasks are skipped by the method
        # itself, so this never double-settles a turn.
        self.recover_interrupted()

    # -- turn runner -------------------------------------------------------

    async def _run_turn(self, turn_id: str, text: str) -> None:
        turn = self.store.get_turn(turn_id)
        if turn is None:  # pragma: no cover - internal invariant
            return
        state = _TurnState(
            turn=turn,
            thread_id=turn["thread_id"],
            user_id=turn["user_message_id"],
            assistant_id=turn["assistant_message_id"],
            model=self._model,
        )
        state.started_mono = time.monotonic()
        self._live_blocks[state.assistant_id] = state.blocks
        self.store.update_turn(turn_id, state="running", started_at=iso_now(self._now()))
        self.store.update_message(state.user_id, status="complete")
        try:
            if self._provider is None:
                self._emit_error(
                    state,
                    "missing_api_key",
                    retryable=False,
                    details=None,
                    step=0,
                )
                self._finalize(state, "error", duration_ms=0)
                return

            approval_hook = self._approval_hook(state)
            executor = self._make_executor(approval_hook)
            loop = AgentLoop(
                provider=self._provider,
                config=KernelConfig(),
                tool_executor=executor,
                budget=self._budget,
                usage_store=self._usage_store,
                model=self._model,
                now_fn=self._now,
            )
            token = self._cancel.get(turn_id)
            async for event in loop.run_turn(
                [{"role": "user", "content": text}],
                tools=self._tools,
                turn_id=turn_id,
                session_id=state.thread_id,
                model=self._model,
                cancel=token,
            ):
                if self.store.get_thread(state.thread_id) is None:
                    break
                self._handle_kernel_event(state, event)
            if not state.finished:
                self._finalize(
                    state,
                    "error",
                    duration_ms=self._elapsed(state),
                )
        except asyncio.CancelledError:
            if not state.finished:
                self._abandon_turn_approvals(state)
                # A process shutdown is not a client "stop": §10.4 says the
                # turn ends ``interrupted`` so the client offers Resume.
                reason = "interrupted" if self._closing else "cancelled"
                self._mark_running_tools(
                    state, "interrupted" if self._closing else "turn_cancelled"
                )
                self._finalize(
                    state, reason, duration_ms=self._elapsed(state)
                )
        except Exception as exc:  # noqa: BLE001 - a turn never escapes to the caller
            if not state.finished:
                self._abandon_turn_approvals(state)
                # Only the exception's class name is logged. ``str(exc)`` can
                # carry a prompt, a URL with a token in it, or a provider body,
                # and the logging contract forbids free text.
                log(
                    "turn_crashed",
                    request_id=current_request_id(),
                    thread_id=state.thread_id,
                    turn=state.turn["id"],
                    error_code=_error_code(type(exc).__name__),
                    status="error",
                    step=state.steps,
                )
                self._emit_error(
                    state, "provider_error", retryable=True, details=None, step=state.steps
                )
                self._finalize(
                    state,
                    "error",
                    duration_ms=self._elapsed(state),
                    error_code="provider_error",
                )
        finally:
            self._live_blocks.pop(state.assistant_id, None)
            self._waiting_approval.pop(turn_id, None)
            self._cancel.pop(turn_id, None)
            self._tasks.pop(turn_id, None)
            done = self._done.get(turn_id)
            if done is not None:
                done.set()
            self.store.touch_thread(state.thread_id)
            self.bus.publish(state.thread_id)

    def _make_executor(self, approval_hook: Any) -> Any:
        if self._tool_executor_factory is not None:
            return self._tool_executor_factory(approval_hook)
        if self._registry is not None:
            return RegistryToolExecutor(
                self._registry,
                approval=approval_hook,
                secrets=self._secrets,
                workspace_root=self._workspace_root,
            )
        return None

    def _handle_kernel_event(self, state: _TurnState, event: Any) -> None:
        if isinstance(event, kernel_events.TurnStarted):
            state.model = event.model
            self.store.update_message(state.user_id, status="complete")
            self._emit(
                state.thread_id,
                "turn_started",
                {
                    "turn_id": state.turn["id"],
                    "message_id": state.assistant_id,
                    "model": event.model,
                    "limits": {
                        "max_steps": event.max_steps,
                        "max_output_tokens": event.max_output_tokens,
                        "max_tool_calls": event.max_tool_calls,
                        "max_wall_seconds": event.max_wall_seconds,
                    },
                },
            )
            return

        if isinstance(event, kernel_events.TokenDelta):
            state.steps = max(state.steps, event.step)
            self._append_text(state, event.text)
            self._emit(
                state.thread_id,
                "token_delta",
                {
                    "turn_id": state.turn["id"],
                    "message_id": state.assistant_id,
                    "text": event.text,
                    "step": event.step,
                },
            )
            # Persist the read model with the event so a mid-stream reload can
            # never skip a token between the snapshot and the resumed stream.
            self._flush(state, force=True)
            return

        if isinstance(event, kernel_events.ToolCallStarted):
            state.steps = max(state.steps, event.step)
            state.text_open = False
            tool_name = self._observable_tool(event.name)
            log(
                "tool_call_started",
                thread_id=state.thread_id,
                turn=state.turn["id"],
                tool=tool_name,
                status="started",
                step=event.step,
            )
            effects = self._effects_for(event.name)
            redacted = cards.redact_arguments(
                event.arguments, secrets=self._secret_values
            )
            block = {
                "type": "tool_call",
                "call_id": event.call_id,
                "tool": event.name,
                "label": cards.tool_label(event.name),
                "summary": cards.summarize_arguments(event.name, redacted),
                "status": "running",
                "duration_ms": None,
                "effects": list(effects),
                "approval_id": None,
                "detail": {
                    "arguments": redacted,
                    "result_summary": None,
                    "result": None,
                    "error": None,
                },
            }
            state.blocks.append(block)
            self._flush(state, force=True)
            self._emit(
                state.thread_id,
                "tool_call_started",
                {
                    "turn_id": state.turn["id"],
                    "message_id": state.assistant_id,
                    "call_id": event.call_id,
                    "tool": event.name,
                    "label": block["label"],
                    "summary": block["summary"],
                    "arguments": block["detail"]["arguments"],
                    "effects": block["effects"],
                    "step": event.step,
                },
            )
            return

        if isinstance(event, kernel_events.ToolCallFinished):
            state.steps = max(state.steps, event.step)
            state.tool_calls += 1
            tool_name = self._observable_tool(event.name)
            block = self._tool_block(state, event.call_id)
            if block is None:  # pragma: no cover - started always precedes
                block = {
                    "type": "tool_call",
                    "call_id": event.call_id,
                    "tool": event.name,
                    "label": cards.tool_label(event.name),
                    "summary": "",
                    "status": "running",
                    "duration_ms": None,
                    "effects": self._effects_for(event.name),
                    "approval_id": None,
                    "detail": {"arguments": {}, "result": None, "error": None},
                }
                state.blocks.append(block)
            block["status"] = "done" if event.ok else "failed"
            error_code: str | None = None
            if not event.ok:
                code = (
                    event.error.get("code")
                    if isinstance(event.error, Mapping)
                    else None
                )
                error_code = _error_code(code)
                if code == "approval_denied":
                    block["status"] = "blocked"
                elif code in {"expired", "approval_expired"}:
                    block["status"] = "expired"
                approval_id = block.get("approval_id")
                if approval_id:
                    approval = self.store.get_approval(str(approval_id))
                    if approval is not None and approval["status"] == "expired":
                        block["status"] = "expired"
                    elif approval is not None and approval["status"] == "denied":
                        block["status"] = "blocked"
            block["duration_ms"] = event.duration_ms
            detail = block.setdefault("detail", {})
            result = (
                self._scrub(event.result)
                if isinstance(event.result, Mapping)
                else event.result
            )
            detail["result"] = result
            detail["error"] = self._scrub(event.error)

            artifact_meta = None
            if event.ok and isinstance(result, Mapping):
                output = result.get("output")
                if isinstance(output, Mapping) and event.name == "docs.answer":
                    artifact_meta = self._resolve_docs_artifact(state, output)
            detail["result_summary"] = _result_summary(event.name, result, event.ok)

            new_sources = cards.sources_from_envelope(
                event.name, result, artifact=artifact_meta
            )
            merged, total = cards.merge_sources(state.sources, new_sources)
            sources_changed = len(merged) != len(state.sources) or total != state.sources_total
            state.sources, state.sources_total = merged, total
            if sources_changed:
                self.store.update_message(
                    state.assistant_id, sources=state.sources, sources_total=total
                )

            cards.apply_sweeps(state.blocks)
            if block.get("approval_id"):
                self._settle_approval(state, block["approval_id"], executed=True)
            tool_status = str(
                block.get("status") or ("done" if event.ok else "failed")
            )
            # Error codes stay out of metric labels: they are request-scoped
            # diagnostics and would multiply the series without bound.
            self._metrics.observe_tool(tool_name, tool_status)
            if event.waited_ms > 0:
                self._metrics.observe_approval_wait(event.waited_ms / 1000.0)
            request_id = current_request_id()
            log(
                "tool_call",
                request_id=request_id,
                thread_id=state.thread_id,
                turn=state.turn["id"],
                tool=tool_name,
                status=tool_status,
                error_code=error_code,
                duration_ms=event.duration_ms,
                step=event.step,
            )
            audit_extra: dict[str, Any] = {}
            if error_code is not None:
                audit_extra["error_code"] = error_code
            self._record_audit(
                "tool_call",
                turn_id=state.turn["id"],
                request_id=request_id,
                tool=tool_name,
                outcome=tool_status,
                duration_ms=event.duration_ms,
                extra=audit_extra,
            )
            self._flush(state, force=True)
            self._emit(
                state.thread_id,
                "tool_call_finished",
                {
                    "turn_id": state.turn["id"],
                    "message_id": state.assistant_id,
                    "call_id": event.call_id,
                    "tool": event.name,
                    "ok": event.ok,
                    "status": block["status"],
                    "duration_ms": event.duration_ms,
                    "result_summary": detail.get("result_summary"),
                    "error": detail.get("error"),
                    "detail": detail,
                    "step": event.step,
                },
            )
            if sources_changed:
                self._emit_sources(state)
            if artifact_meta is not None:
                self._emit_artifact(state, artifact_meta)
            return

        if isinstance(event, kernel_events.UsageUpdated):
            state.steps = max(state.steps, event.step)
            state.input_tokens = event.total_input_tokens
            state.output_tokens = event.total_output_tokens
            state.estimated_cost_usd = event.estimated_cost_usd
            self._emit(
                state.thread_id,
                "usage_updated",
                {
                    "turn_id": state.turn["id"],
                    "message_id": state.assistant_id,
                    "model": event.model,
                    "input_tokens": event.input_tokens,
                    "output_tokens": event.output_tokens,
                    "estimated_cost_usd": event.estimated_cost_usd,
                    "step": event.step,
                },
            )
            return

        if isinstance(event, kernel_events.Error):
            state.steps = max(state.steps, event.step)
            self._emit_error(
                state,
                event.code,
                retryable=event.retryable,
                details=self._scrub(event.details),
                step=event.step,
            )
            return

        if isinstance(event, kernel_events.TurnFinished):
            state.steps = max(state.steps, event.steps)
            state.tool_calls = max(state.tool_calls, event.tool_calls)
            state.input_tokens = event.input_tokens
            state.output_tokens = event.output_tokens
            state.estimated_cost_usd = event.estimated_cost_usd
            state.usage_persisted = event.usage_persisted
            self._finalize(
                state,
                event.reason,
                duration_ms=event.duration_ms,
            )
            return

    # -- blocks ------------------------------------------------------------

    def _append_text(self, state: _TurnState, text: str) -> None:
        if text == "":
            return
        if state.text_open and state.blocks and state.blocks[-1].get("type") == "text":
            state.blocks[-1]["text"] = str(state.blocks[-1].get("text") or "") + text
        else:
            state.blocks.append({"type": "text", "text": text})
            state.text_open = True

    def _tool_block(self, state: _TurnState, call_id: str) -> dict[str, Any] | None:
        for block in state.blocks:
            if block.get("type") == "tool_call" and block.get("call_id") == call_id:
                return block
        return None

    def _flush(self, state: _TurnState, *, force: bool = False) -> None:
        # The read model is written with the event that changed it. ``force`` is
        # kept for callers that flush at a block boundary for clarity.
        self.store.update_message(state.assistant_id, blocks=state.blocks)

    def _mark_running_tools(self, state: _TurnState, code: str) -> None:
        for block in state.blocks:
            if block.get("type") == "tool_call" and block.get("status") in {
                "running",
                "waiting_approval",
            }:
                block["status"] = "failed"
                detail = block.setdefault("detail", {})
                if isinstance(detail, dict):
                    detail["error"] = {"code": code}

    # -- cards -------------------------------------------------------------

    def _resolve_docs_artifact(
        self, state: _TurnState, output: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        artifact = output.get("artifact")
        handle = artifact.get("handle") if isinstance(artifact, Mapping) else None
        if not isinstance(handle, str) or not HANDLE_RE.match(handle):
            self._emit_error(
                state, "artifact_not_found", retryable=False, details=None, step=state.steps
            )
            return None
        try:
            return self.artifacts.get(handle, thread_id=state.thread_id)
        except ArtifactError:
            self._emit_error(
                state, "artifact_not_found", retryable=False, details=None, step=state.steps
            )
            return None

    def _emit_sources(self, state: _TurnState) -> None:
        self._flush(state, force=True)
        self._emit(
            state.thread_id,
            "sources_ready",
            {
                "turn_id": state.turn["id"],
                "message_id": state.assistant_id,
                "sources": state.sources,
                "sources_total": state.sources_total,
            },
        )

    def _emit_artifact(self, state: _TurnState, meta: Mapping[str, Any]) -> None:
        try:
            card = self.artifacts.card(str(meta["handle"]), thread_id=state.thread_id)
        except ArtifactError:  # pragma: no cover - meta was just resolved
            return
        state.blocks.append({"type": "artifact", "artifact": card})
        state.text_open = False
        self._flush(state, force=True)
        self._emit(
            state.thread_id,
            "artifact_ready",
            {
                "turn_id": state.turn["id"],
                "message_id": state.assistant_id,
                "artifact": card,
            },
        )

    def _emit_error(
        self,
        state: _TurnState,
        code: str,
        *,
        retryable: bool,
        details: Mapping[str, Any] | None,
        step: int,
    ) -> None:
        # The code is a stable, internal token; it is safe (and useful) to keep
        # in logs and audit. It never becomes a metric label, and the message
        # text is generated copy, not provider output.
        error_code = _error_code(code)
        if error_code is not None:
            # Last error wins, so a fatal failure after a recovered one is the
            # code the finished turn reports.
            state.error_code = error_code
        request_id = current_request_id()
        log(
            "turn_error",
            request_id=request_id,
            thread_id=state.thread_id,
            turn=state.turn["id"],
            error_code=error_code,
            status="error",
            step=step,
        )
        audit_extra: dict[str, Any] = {}
        if error_code is not None:
            audit_extra["error_code"] = error_code
        self._record_audit(
            "turn_error",
            turn_id=state.turn["id"],
            request_id=request_id,
            outcome="error",
            extra=audit_extra,
        )
        self._emit(
            state.thread_id,
            "error",
            {
                "turn_id": state.turn["id"],
                "message_id": state.assistant_id,
                "code": code,
                "message": cards.error_copy(code),
                "retryable": retryable,
                "user_fixable": False,
                "details": dict(details) if details else None,
                "step": step,
            },
        )

    def _finalize(
        self,
        state: _TurnState,
        reason: str,
        *,
        duration_ms: int,
        error_code: str | None = None,
    ) -> None:
        if state.finished:
            return
        state.finished = True
        # An explicit code wins. Otherwise only an ``error`` end inherits the
        # turn's last code: a recovered mid-turn error must not label a turn
        # that went on to finish normally.
        code = _error_code(error_code)
        if code is None and reason == "error":
            code = state.error_code
        status = MESSAGE_STATUS_BY_REASON.get(reason, "error")
        self.store.update_message(state.user_id, status="complete")
        self._mark_running_tools(
            state, "turn_cancelled" if reason == "cancelled" else "tool_error"
        )
        self.store.update_message(
            state.assistant_id, status=status, blocks=state.blocks
        )
        usage = {
            "model": state.model,
            "input_tokens": state.input_tokens,
            "output_tokens": state.output_tokens,
            "estimated_cost_usd": state.estimated_cost_usd,
            "usage_persisted": state.usage_persisted,
        }
        self.store.update_turn(
            state.turn["id"],
            state="finished",
            reason=reason,
            finished_at=iso_now(self._now()),
            usage=usage,
        )
        request_id = current_request_id()
        self._metrics.observe_turn(reason, max(0.0, duration_ms / 1000.0))
        self._metrics.add_tokens("input", state.model, state.input_tokens)
        self._metrics.add_tokens("output", state.model, state.output_tokens)
        if state.estimated_cost_usd is not None:
            self._metrics.add_cost(state.estimated_cost_usd)
        # The turn's usage record is already on disk (the kernel persists it
        # before ``TurnFinished``), so republish the day/month totals here: a
        # scrape after the turn must show the spend it just added, not the
        # total from before it (KEL-18 review M1).
        self.publish_spend_metrics()
        log(
            "turn_finished",
            request_id=request_id,
            thread_id=state.thread_id,
            turn=state.turn["id"],
            status=reason,
            error_code=code,
            duration_ms=duration_ms,
            model=state.model,
        )
        audit_extra: dict[str, Any] = {"thread_id": state.thread_id}
        if code is not None:
            audit_extra["error_code"] = code
        self._record_audit(
            "turn_finished",
            turn_id=state.turn["id"],
            request_id=request_id,
            outcome=reason,
            duration_ms=duration_ms,
            extra=audit_extra,
        )
        self._emit(
            state.thread_id,
            "turn_finished",
            {
                "turn_id": state.turn["id"],
                "message_id": state.assistant_id,
                "message_status": status,
                "reason": reason,
                "steps": state.steps,
                "tool_calls": state.tool_calls,
                "input_tokens": state.input_tokens,
                "output_tokens": state.output_tokens,
                "estimated_cost_usd": state.estimated_cost_usd,
                "duration_ms": duration_ms,
                "usage_persisted": state.usage_persisted,
            },
        )

    # -- approvals ---------------------------------------------------------

    def _approval_hook(self, state: _TurnState) -> Callable[[SkillApprovalRequest], Awaitable[bool]]:
        async def hook(request: SkillApprovalRequest) -> bool:
            return await self._request_skill_approval(state, request)

        return hook

    async def _request_skill_approval(
        self, state: _TurnState, request: SkillApprovalRequest
    ) -> bool:
        approval_id = new_id("apr")
        now = self._now()
        requested_at = iso_now(now)
        expires_at = iso_now(now + timedelta(seconds=self._approval_ttl))
        call_id = self._current_call_id(state)
        card = cards.approval_card(
            approval_id=approval_id,
            source="skill",
            thread_id=state.thread_id,
            turn_id=state.turn["id"],
            call_id=call_id,
            tool=request.tool,
            skill=request.skill,
            effects=request.effects,
            arguments=request.arguments,
            scopes=request.scopes,
            arguments_digest=request.argument_hash,
            requested_at=requested_at,
            expires_at=expires_at,
            title=_approval_title(request.tool),
            secrets=self._secret_values,
        )
        self.store.save_approval(card)
        self._attach_approval(state, card)
        request_id = current_request_id()
        log(
            "approval_requested",
            request_id=request_id,
            thread_id=state.thread_id,
            turn=state.turn["id"],
            tool=request.tool,
            approval_id=approval_id,
            status="pending",
        )
        self._record_audit(
            "approval_requested",
            turn_id=state.turn["id"],
            request_id=request_id,
            tool=request.tool,
            decision="pending",
            argument_hash=request.argument_hash,
        )
        waiter = asyncio.Event()
        loop = asyncio.get_running_loop()
        self._waiters[approval_id] = waiter
        self._waiter_loops[approval_id] = loop
        self._waiting_approval[state.turn["id"]] = approval_id
        timeout = max(
            0.0,
            (
                datetime.fromisoformat(expires_at.replace("Z", "+00:00")) - now
            ).total_seconds(),
        )
        cancelled = False
        try:
            cancelled = await self._wait_for_decision(state, waiter, timeout)
        except asyncio.CancelledError:
            # A hard cancel can still land here; never leave the decision
            # dangling (see ``_abandon_approval``).
            self._abandon_approval(state, approval_id)
            raise
        finally:
            self._waiters.pop(approval_id, None)
            self._waiter_loops.pop(approval_id, None)
            self._waiting_approval.pop(state.turn["id"], None)
        if cancelled:
            self._abandon_approval(state, approval_id)
            return False
        current = self.store.get_approval(approval_id)
        if current is not None and current["status"] == "approved":
            return True
        if current is not None and current["status"] == "pending":
            self._record_resolution(state, approval_id, "expired", executed=False)
        return False

    async def _wait_for_decision(
        self,
        state: _TurnState,
        waiter: asyncio.Event,
        timeout: float,
    ) -> bool:
        """Wait for a decision or Stop, whichever comes first.

        Polling in small slices keeps Stop cooperative: the turn ends promptly
        without a hard cancel that could strand the approval (§5.3, §14.1).
        Returns ``True`` when Stop won and the approval must be abandoned.
        """

        token = self._cancel.get(state.turn["id"])
        deadline = time.monotonic() + timeout
        while True:
            if token is not None and token.cancelled:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(
                    waiter.wait(), timeout=min(_APPROVAL_POLL_SECONDS, remaining)
                )
                break
            except asyncio.TimeoutError:
                continue
        return token is not None and token.cancelled

    def _abandon_turn_approvals(self, state: _TurnState) -> None:
        """Close approvals a stopped turn can no longer honour."""

        for card in self.store.list_approvals(thread_id=state.thread_id, limit=200):
            if card.get("turn_id") != state.turn["id"]:
                continue
            self._abandon_approval(state, str(card["id"]))

    def _abandon_approval(self, state: _TurnState, approval_id: str) -> None:
        """Settle one approval whose turn is over.

        A card left ``pending`` stays decidable, and a later Approve would have
        no waiter to consume it; an ``approved`` card left behind promises a run
        that never happens. Both are closed here, without executing anything.
        """

        card = self.store.get_approval(approval_id)
        if card is None or card.get("source") == "openshell":
            return
        if card.get("status") == "pending":
            self._record_resolution(state, approval_id, "expired", executed=False)
        elif card.get("status") == "approved":
            self._record_resolution(
                state,
                approval_id,
                "consumed",
                executed=False,
                allowed_from=("approved",),
            )

    def _current_call_id(self, state: _TurnState) -> str | None:
        for block in reversed(state.blocks):
            if block.get("type") == "tool_call":
                return str(block.get("call_id"))
        return None

    def _attach_approval(self, state: _TurnState, card: dict[str, Any]) -> None:
        call_id = card.get("call_id")
        for block in state.blocks:
            if block.get("type") == "tool_call" and block.get("call_id") == call_id:
                block["approval_id"] = card["id"]
                block["status"] = "waiting_approval"
        state.blocks.append({"type": "approval", "approval": card})
        state.text_open = False
        self._flush(state, force=True)
        self._emit(
            state.thread_id,
            "approval_requested",
            {
                "turn_id": state.turn["id"],
                "message_id": state.assistant_id,
                "approval": card,
            },
        )

    def get_approval(self, approval_id: str) -> dict[str, Any]:
        card = self.store.get_approval(approval_id)
        if card is None:
            raise ChatError("unknown_approval", status=404)
        if self.store.get_thread(str(card.get("thread_id") or "")) is None:
            raise ChatError("unknown_approval", status=404)
        return card

    def list_approvals(
        self, *, status: str | None = None, thread_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        cards = self.store.list_approvals(
            status=status, thread_id=thread_id, limit=limit
        )
        return [
            card
            for card in cards
            if self.store.get_thread(str(card.get("thread_id") or "")) is not None
        ]

    def decide(self, approval_id: str, decision: str, note: str | None = None) -> dict[str, Any]:
        if decision not in {"approve", "deny"}:
            raise ChatError(
                "invalid_request",
                status=400,
                message="Decision must be 'approve' or 'deny'.",
                user_fixable=True,
                field="/decision",
            )
        if note is not None and len(note) > _MAX_DECISION_NOTE:
            raise ChatError(
                "invalid_request",
                status=400,
                message=f"A note is at most {_MAX_DECISION_NOTE} characters.",
                user_fixable=True,
                field="/note",
            )
        card = self.store.get_approval(approval_id)
        if card is None:
            raise ChatError("unknown_approval", status=404)
        if self.store.get_thread(str(card.get("thread_id") or "")) is None:
            raise ChatError("unknown_approval", status=404)
        status = "approved" if decision == "approve" else "denied"
        if card.get("source") == "openshell":
            return self._decide_exec(card, decision, status)
        replay = self._replay_decision(card, status)
        if replay is not None:
            return replay
        return self._decide_card(card, status)

    def _replay_decision(
        self, card: Mapping[str, Any], status: str
    ) -> dict[str, Any] | None:
        """Answer for a card that is not pending; ``None`` means it still is."""

        current = str(card.get("status"))
        if current == "consumed":
            raise ChatError("already_used", status=409)
        if current == "expired":
            raise ChatError("expired", status=409)
        if current == status:
            return {
                "ok": True,
                "approval": dict(card),
                "executed": False,
                "idempotent": True,
            }
        if current != "pending":
            raise ChatError("not_approved", status=409)
        return None

    def _decide_card(self, card: Mapping[str, Any], status: str) -> dict[str, Any]:
        """Decide a skill-runtime approval: the guarded UPDATE is the authority."""

        approval_id = str(card["id"])
        now = self._now()
        expires = _as_aware(
            datetime.fromisoformat(str(card["expires_at"]).replace("Z", "+00:00"))
        )
        if now >= expires:
            self._record_resolution(None, approval_id, "expired", executed=False)
            raise ChatError("expired", status=409)
        updated, applied = self._record_resolution(
            None,
            approval_id,
            status,
            executed=False,
            decided_at=iso_now(now),
            allowed_from=("pending",),
        )
        if not applied:
            replay = self._replay_decision(updated, status)
            if replay is not None:
                return replay
            raise ChatError("not_approved", status=409)  # pragma: no cover
        return {
            "ok": True,
            "approval": updated,
            "executed": False,
            "idempotent": False,
        }

    def _decide_exec(
        self, card: Mapping[str, Any], decision: str, status: str
    ) -> dict[str, Any]:
        """Decide an exec approval through KEL-7, whose store owns single use."""

        approval_id = str(card["id"])
        try:
            if decision == "approve":
                self.approvals.approve(approval_id)
            else:
                self.approvals.deny(approval_id)
        except ApprovalExpired:
            self._record_resolution(None, approval_id, "expired", executed=False)
            raise ChatError("expired", status=409) from None
        except ApprovalAlreadyUsed:
            raise ChatError("already_used", status=409) from None
        except (ApprovalNotGranted, ApprovalDenied):
            raise ChatError("not_approved", status=409) from None
        except ApprovalNotFound:
            raise ChatError("unknown_approval", status=404) from None

        updated, applied = self._record_resolution(
            None,
            approval_id,
            status,
            executed=False,
            decided_at=iso_now(self._now()),
            allowed_from=("pending",),
        )
        if not applied:
            # A concurrent decision mirrored first: report as a replay so both
            # callers agree on the same final state.
            replay = self._replay_decision(updated, status)
            if replay is not None:
                return replay
            raise ChatError("not_approved", status=409)  # pragma: no cover
        return {
            "ok": True,
            "approval": updated,
            "executed": False,
            "idempotent": False,
        }

    def _record_resolution(
        self,
        state: _TurnState | None,
        approval_id: str,
        status: str,
        *,
        executed: bool,
        decided_at: str | None = None,
        allowed_from: Sequence[str] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Record a status change atomically; announce it only when we won.

        Losing the guarded UPDATE means another caller already moved the row,
        so writing a second event (or a stale one) is exactly what two tabs
        pressing Approve must not produce.
        """

        card = self.store.get_approval(approval_id)
        if card is None:  # pragma: no cover - callers check first
            raise ChatError("unknown_approval", status=404)
        decided = decided_at or iso_now(self._now())
        source = (
            allowed_from
            if allowed_from is not None
            else APPROVAL_TRANSITIONS.get(status, ("pending",))
        )
        updated = self.store.transition_approval(
            approval_id,
            status,
            allowed_from=source,
            decided_at=decided if status in {"approved", "denied", "expired"} else None,
            consumed_at=decided if status == "consumed" else None,
        )
        if updated is None:
            current = self.store.get_approval(approval_id) or card
            return current, False

        tool_status = {
            "approved": "running",
            "denied": "blocked",
            "expired": "expired",
        }.get(status)
        blocks = self._live_blocks.get(_message_for_card(self.store, card))
        if blocks is not None:
            self._apply_resolution_to_blocks(blocks, approval_id, updated, tool_status)
            message_id = _message_for_card(self.store, card)
            if message_id:
                self.store.update_message(message_id, blocks=blocks)
        else:
            message_id = _message_for_card(self.store, card)
            if message_id:
                blocks = self.store.get_blocks(message_id)
                if self._apply_resolution_to_blocks(blocks, approval_id, updated, tool_status):
                    self.store.update_message(message_id, blocks=blocks)

        self._emit(
            updated["thread_id"],
            "approval_resolved",
            {
                "approval_id": approval_id,
                "thread_id": updated["thread_id"],
                "turn_id": updated.get("turn_id"),
                "status": status,
                "decided_at": (
                    updated.get("decided_at") if status == "consumed" else decided
                ),
                "actor": "owner",
                "executed": executed,
            },
        )
        waiter = self._waiters.get(approval_id)
        loop = self._waiter_loops.get(approval_id)
        if waiter is not None:
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(waiter.set)
            else:  # pragma: no cover - defensive
                waiter.set()
        request_id = current_request_id()
        log(
            "approval_resolved",
            request_id=request_id,
            thread_id=updated.get("thread_id"),
            turn=updated.get("turn_id"),
            tool=updated.get("tool"),
            approval_id=approval_id,
            status=status,
        )
        self._record_audit(
            "approval_resolved",
            turn_id=(
                str(updated["turn_id"])
                if updated.get("turn_id") is not None
                else None
            ),
            request_id=request_id,
            tool=(
                str(updated["tool"])
                if updated.get("tool") is not None
                else None
            ),
            decision=status,
            outcome=status,
        )
        return updated, True

    def _apply_resolution_to_blocks(
        self,
        blocks: Sequence[dict[str, Any]],
        approval_id: str,
        card: Mapping[str, Any],
        tool_status: str | None,
    ) -> bool:
        changed = False
        for block in blocks:
            if block.get("type") == "approval":
                approval = block.get("approval")
                if isinstance(approval, dict) and approval.get("id") == approval_id:
                    approval["status"] = card.get("status")
                    approval["decided_at"] = card.get("decided_at")
                    approval["consumed_at"] = card.get("consumed_at")
                    changed = True
            if (
                tool_status
                and block.get("type") == "tool_call"
                and block.get("approval_id") == approval_id
            ):
                block["status"] = tool_status
                changed = True
        return changed

    def _settle_approval(
        self, state: _TurnState, approval_id: str, *, executed: bool
    ) -> None:
        card = self.store.get_approval(approval_id)
        if card is None or card.get("source") == "openshell":
            return
        if card.get("status") != "approved":
            return
        self._record_resolution(
            state,
            approval_id,
            "consumed",
            executed=executed,
            allowed_from=("approved",),
        )

    def open_exec_approval(
        self,
        *,
        thread_id: str,
        turn_id: str | None,
        call_id: str | None,
        tool: str,
        command: Sequence[str],
        effects: Sequence[str] = ("execute",),
        blast_radius: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a card for a KEL-7 gate request, preserving its id/expiry."""

        if self.store.get_thread(thread_id) is None:
            raise ChatError("unknown_thread", status=404)
        request = self.approvals.create(
            tool=tool,
            command=command,
            effects=effects,
            blast_radius=blast_radius,
            metadata=dict(metadata or {}),
        )
        card = cards.approval_card(
            approval_id=request.id,
            source="openshell",
            thread_id=thread_id,
            turn_id=turn_id,
            call_id=call_id,
            tool=tool,
            effects=request.effects,
            command=request.command,
            blast_radius=request.blast_radius,
            arguments={"command": list(request.command)},
            arguments_digest=request.argument_hash,
            requested_at=request.requested_at,
            expires_at=request.expires_at,
            title="Run a command",
            secrets=self._secret_values,
        )
        self.store.save_approval(card)
        self._emit(
            thread_id,
            "approval_requested",
            {"turn_id": turn_id, "message_id": None, "approval": card},
        )
        return card

    # -- recovery ----------------------------------------------------------

    def recover_interrupted(self) -> list[str]:
        """Close turns left running by a previous process (§10.4)."""

        closed: list[str] = []
        for turn in self.store.running_turns():
            if turn["id"] in self._tasks:
                continue
            if self.store.get_thread(turn["thread_id"]) is None:
                continue
            message_id = turn["assistant_message_id"]
            self.store.update_message(message_id, status="interrupted")
            usage = {"usage_persisted": False}
            self.store.update_turn(
                turn["id"],
                state="finished",
                reason="interrupted",
                finished_at=iso_now(self._now()),
                usage=usage,
            )
            self._emit(
                turn["thread_id"],
                "turn_finished",
                {
                    "turn_id": turn["id"],
                    "message_id": message_id,
                    "message_status": "interrupted",
                    "reason": "interrupted",
                    "steps": 0,
                    "tool_calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "estimated_cost_usd": None,
                    "duration_ms": 0,
                    "usage_persisted": False,
                },
            )
            closed.append(turn["id"])
        return closed

    # -- helpers -----------------------------------------------------------

    def _budget_dict(self) -> dict[str, Any]:
        return {
            "max_steps": self._budget.max_steps,
            "max_output_tokens": self._budget.max_output_tokens,
            "max_tool_calls": self._budget.max_tool_calls,
            "max_wall_seconds": self._budget.max_wall_seconds,
        }

    def _effects_for(self, tool: str) -> tuple[str, ...]:
        if tool == "shell.exec":
            return ("execute",)
        if self._registry is not None and tool in self._registry:
            try:
                return self._registry.get(tool).effects
            except KeyError:  # pragma: no cover - guarded by membership
                return ()
        return ()

    def _emit(self, thread_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
        self.store.append_event(thread_id, event_type, payload)
        self.bus.publish(thread_id)

    def _elapsed(self, state: _TurnState) -> int:
        if not state.started_mono:
            return 0
        return max(0, int((time.monotonic() - state.started_mono) * 1000))

    def _scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            for secret in self._secret_values:
                value = value.replace(secret, "[redacted]")
            return value
        if isinstance(value, Mapping):
            return {key: self._scrub(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._scrub(item) for item in value]
        return value

    def _consume_task_error(self, task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except Exception:  # pragma: no cover - never leak out of a callback
            pass


def _result_summary(tool: str, result: Mapping[str, Any] | None, ok: bool) -> str:
    if not ok:
        return "failed"
    output = result.get("output") if isinstance(result, Mapping) else None
    if not isinstance(output, Mapping):
        return "done"
    if tool == "web.search":
        results = output.get("results")
        return f"{len(results)} results" if isinstance(results, list) else "done"
    if tool == "github.read":
        kind = output.get("kind")
        if isinstance(output.get("repo"), Mapping):
            count = output["repo"].get("open_issues_count")
            return f"{count} open issues" if isinstance(count, int) else "repository"
        return f"{kind} found" if kind else "done"
    if tool == "docs.answer":
        return "answer ready"
    return "done"


def _attachment_block(meta: Mapping[str, Any], thread_id: str) -> dict[str, Any]:
    handle = str(meta["handle"])
    # The upload is bound to this thread in the same transaction, so its URL
    # must carry the scope or the message's own link would 404 (§8).
    return {
        "type": "attachment",
        "attachment": {
            "handle": handle,
            "name": meta.get("name"),
            "kind": meta.get("kind"),
            "mime": meta.get("mime"),
            "size_bytes": meta.get("size_bytes"),
            "status": "attached",
            "open_url": f"/api/artifacts/{handle}/content?thread_id={thread_id}",
        },
    }


def _message_text(message: Mapping[str, Any]) -> str:
    for block in reversed(message.get("blocks") or []):
        if block.get("type") == "text":
            return str(block.get("text") or "")
    return ""


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _approval_title(tool: str) -> str:
    return {
        "schedule.create": "Create a recurring task",
        "shell.exec": "Run a command",
    }.get(tool, f"Use {cards.tool_label(tool)}")


def _message_for_card(store: ChatStore, card: Mapping[str, Any]) -> str | None:
    turn_id = card.get("turn_id")
    if not turn_id:
        return None
    turn = store.get_turn(str(turn_id))
    return str(turn["assistant_message_id"]) if turn is not None else None

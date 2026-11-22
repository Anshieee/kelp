"""Streaming agent loop: model step -> tool calls -> observations -> finish.

Contract highlights (KEL-6 / P0-1):

- One async generator per user turn; it never raises for provider or tool errors.
- Hard budgets: model steps, output tokens, tool calls, wall-clock seconds.
- Tool calls are executed through an injected executor; the loop does not
  special-case skill names.
- Every turn writes one usage record (tokens in/out, estimated cost, outcome).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, Mapping, Protocol, Sequence

from skills import cli as skill_cli
from skills.context import ApprovalRequest
from skills.loader import find_pinned_plugin, plugin_review_warnings

from . import events
from .config import KernelConfig
from .provider import (
    ChatRequest,
    ProviderFailure,
    StreamEnd,
    TextDelta,
    ToolCall,
    ToolCallReady,
    UsageReport,
)
from .usage import UsageRecord, UsageStore

if TYPE_CHECKING:  # pragma: no cover - typing only
    from skills import SkillRegistry

__all__ = [
    "AgentLoop",
    "CallableToolExecutor",
    "CancelToken",
    "RegistryToolExecutor",
    "ToolExecutor",
    "TurnBudget",
]

MAX_TOOL_CONTENT_CHARS = 20_000

# Longest the loop waits on a provider stream before it re-checks the wall
# budget and the cancel token. A provider that only sends keepalives, or
# trickles tokens, would otherwise hold a step open indefinitely.
_STREAM_POLL_SECONDS = 1.0

# Codes of failures a provider raises before it makes any request: missing
# configuration, and the defensive tenant check in kernel.providers.base.
_UNBILLED_FAILURE_CODES = frozenset(
    {"missing_base_url", "missing_api_key", "tenant_mismatch"}
)


class CancelToken:
    """Cooperative stop signal for one turn.

    The dashboard sets it from the Stop endpoint; the loop checks it between
    provider chunks and between steps, so partial text and settled cards stay
    exactly as streamed. Cancellation is never an error.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()



@dataclass(frozen=True)
class TurnBudget:
    """Hard per-turn limits. Exceeding any of them ends the turn cleanly."""

    max_steps: int = 6
    max_output_tokens: int = 4096
    max_tokens_per_step: int = 1024
    max_tool_calls: int = 8
    max_wall_seconds: float = 120.0

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be at least 1")
        if self.max_tokens_per_step < 1:
            raise ValueError("max_tokens_per_step must be at least 1")
        if self.max_tool_calls < 0:
            raise ValueError("max_tool_calls must be non-negative")
        if self.max_wall_seconds <= 0:
            raise ValueError("max_wall_seconds must be positive")


class ToolExecutor(Protocol):
    async def execute(self, call: ToolCall) -> Mapping[str, Any]: ...


class CallableToolExecutor:
    """Adapter for tests and simple in-process tools."""

    def __init__(self, fn: Callable[[ToolCall], Awaitable[Mapping[str, Any]]]):
        self._fn = fn

    async def execute(self, call: ToolCall) -> Mapping[str, Any]:
        return await self._fn(call)


class RegistryToolExecutor:
    """Runs tool calls through the KEL-5 skill registry, including approvals.

    The approval hook is wrapped so the executor can report how long each call
    spent waiting on a human. The loop subtracts that from the turn's wall
    budget (§14.2); the call's own ``duration_ms`` still includes it.
    """

    def __init__(
        self,
        registry: "SkillRegistry",
        *,
        approval: Callable[[Any], Awaitable[bool]] | None = None,
        secrets: Mapping[str, str] | None = None,
        workspace_root: str | None = None,
        policy: Any | None = None,
        audit: Any | None = None,
        turn_id: str | None = None,
        time_fn: Callable[[], float] | None = None,
    ):
        self._registry = registry
        self._approval = approval
        self._secrets = secrets
        self._workspace_root = workspace_root
        self._policy = policy
        self._audit = audit
        self._turn_id = turn_id
        self._time = time_fn or time.monotonic
        self._waits: dict[str, int] = {}
        self._active_call: str | None = None

    async def execute(self, call: ToolCall) -> Mapping[str, Any]:
        self._waits.pop(call.call_id, None)
        self._active_call = call.call_id
        try:
            has_skill_lookup = callable(getattr(self._registry, "__contains__", None))
            if (
                has_skill_lookup
                and call.name not in self._registry
                and self._approval is not None
            ):
                installed = await self._install_requested_skill(call)
                if installed is False:
                    return {
                        "ok": False,
                        "skill": call.name,
                        "tool": call.name,
                        "error": {
                            "code": "approval_denied",
                            "message": "Skill installation was not approved.",
                            "retryable": False,
                            "user_fixable": False,
                        },
                    }
                if installed:
                    self._registry = self._registry.reload()
            return await self._registry.invoke(
                call.name,
                dict(call.arguments),
                approval=self._gate if self._approval is not None else None,
                secrets=self._secrets,
                workspace_root=self._workspace_root,
                policy=self._policy,
                audit=self._audit,
                turn_id=self._turn_id,
            )
        finally:
            self._active_call = None

    async def _install_requested_skill(self, call: ToolCall) -> bool | None:
        """Install a locally pinned candidate only after explicit approval."""

        package = find_pinned_plugin(call.name)
        if package is None:
            return None
        plugin_root = self._registry.plugin_root
        if plugin_root is None:
            return None
        while True:
            warnings = plugin_review_warnings(package)
            flagged_imports = [
                warning for warning in warnings if warning.startswith("flagged import:")
            ]
            args = {
                "skill": package.name,
                "version": package.version,
                "digest": package.digest,
                "source": str(package.source),
                "warnings": list(warnings),
                "enable_flagged_imports": bool(flagged_imports),
            }
            canonical = json.dumps(args, sort_keys=True, separators=(",", ":"))
            now = datetime.now(timezone.utc)
            request = ApprovalRequest(
                skill="skill.install",
                tool="skill.install",
                arguments=args,
                argument_hash="sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                effects=("write",),
                scopes={"paths": (str(plugin_root),), "hosts": (), "repos": ()},
                manifest_version="1.0",
                requested_at=now.isoformat().replace("+00:00", "Z"),
                expires_at=(now + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
            )
            started = self._time()
            try:
                allowed = await self._approval(request)  # type: ignore[misc]
            except Exception:  # fail closed, matching the skill approval gate
                allowed = False
            call_id = self._active_call
            if call_id is not None:
                self._waits[call_id] = self._waits.get(call_id, 0) + _elapsed_ms(
                    started, self._time()
                )
            if not allowed:
                return False

            # Approval is tied to the reviewed digest. If the store now contains
            # a different pinned image, discard this approval and show a fresh
            # request rather than silently approving the replacement.
            current = find_pinned_plugin(call.name)
            if current is None:
                return False
            if (
                current.digest != package.digest
                or current.version != package.version
                or current.source != package.source
            ):
                package = current
                continue
            await asyncio.to_thread(
                skill_cli.install_registered,
                package.name,
                root=plugin_root,
                expected_digest=package.digest,
                expected_version=package.version,
                # Approving this reviewed request is the explicit enablement.
                allow_flagged_imports=bool(flagged_imports),
            )
            return True

    async def _gate(self, request: Any) -> bool:
        assert self._approval is not None  # only wired when an approval exists
        started = self._time()
        try:
            return await self._approval(request)
        finally:
            waited = _elapsed_ms(started, self._time())
            call_id = self._active_call
            if call_id is not None:
                self._waits[call_id] = self._waits.get(call_id, 0) + waited

    def approval_wait_ms(self, call_id: str) -> int:
        """Milliseconds spent awaiting approval for one call; consumed once."""

        return self._waits.pop(call_id, 0)


class AgentLoop:
    def __init__(
        self,
        *,
        provider: Any,
        config: KernelConfig | None = None,
        tool_executor: ToolExecutor | None = None,
        budget: TurnBudget | None = None,
        usage_store: UsageStore | None = None,
        model: str | None = None,
        time_fn: Callable[[], float] = time.monotonic,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self._provider = provider
        self._config = config or KernelConfig.from_env()
        self._tool_executor = tool_executor
        self._budget = budget or TurnBudget()
        self._usage_store = usage_store
        self._model = model or self._config.primary_model
        self._time = time_fn
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

    @property
    def budget(self) -> TurnBudget:
        return self._budget

    async def run_turn(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]] = (),
        turn_id: str | None = None,
        session_id: str | None = None,
        model: str | None = None,
        cancel: CancelToken | None = None,
    ) -> AsyncIterator[events._Event]:
        budget = self._budget
        model = model or self._model
        turn_id = turn_id or f"turn_{uuid.uuid4().hex}"
        started_mono = self._time()
        started_at = self._now()

        steps = 0
        tool_calls_made = 0
        input_tokens = 0
        output_tokens = 0
        usage_reported = False
        # Upper bounds for steps that reached the provider without a
        # UsageReport. Kept apart from input_tokens/output_tokens on purpose:
        # ``remaining`` below must keep measuring reported output only.
        estimated_input_tokens = 0
        estimated_output_tokens = 0
        usage_estimated = False
        # State of the step whose stream is open (or just ended). The
        # finally-block reads it to bound a step that a cancel or an
        # exception cut short.
        step_open = False
        step_metered = False
        step_unbilled_failure = False
        step_events = 0
        step_input_bound = 0
        step_output_bound = 0
        reason = "stop"
        final_message: str | None = None
        # Approval waiting is paused time for the wall budget, not for the
        # duration we report: the turn really did take that long (§14.2).
        approval_paused_ms = 0
        transcript: list[dict[str, Any]] = [dict(message) for message in messages]

        recorded = False
        abort_outcome = "cancelled"
        try:
            yield events.TurnStarted(
                turn_id=turn_id,
                session_id=session_id,
                model=model,
                max_steps=budget.max_steps,
                max_output_tokens=budget.max_output_tokens,
                max_tool_calls=budget.max_tool_calls,
                max_wall_seconds=budget.max_wall_seconds,
            )

            while True:
                if cancel is not None and cancel.cancelled:
                    reason = "cancelled"
                    break

                if steps >= budget.max_steps:
                    reason = "budget"
                    yield _budget_error(
                        "max_steps",
                        f"Turn stopped after {steps} model step(s); limit is {budget.max_steps}.",
                        steps,
                    )
                    break

                elapsed = (self._time() - started_mono) - (approval_paused_ms / 1000.0)
                if elapsed >= budget.max_wall_seconds:
                    reason = "budget"
                    yield _budget_error(
                        "max_wall_seconds",
                        f"Turn stopped after {elapsed:.1f}s; limit is {budget.max_wall_seconds:.1f}s.",
                        steps,
                    )
                    break

                remaining = budget.max_output_tokens - output_tokens
                if remaining <= 0:
                    reason = "budget"
                    yield _budget_error(
                        "max_output_tokens",
                        f"Turn stopped after {output_tokens} output tokens; limit is {budget.max_output_tokens}.",
                        steps,
                    )
                    break

                steps += 1
                step_max_tokens = min(budget.max_tokens_per_step, remaining)
                request = ChatRequest(
                    model=model,
                    messages=transcript,
                    tools=tools,
                    max_tokens=step_max_tokens,
                )
                # The transcript is mutated after the step, so the bound is
                # taken from the request as sent, now.
                step_input_bound = _request_input_bound(transcript, tools)
                step_output_bound = step_max_tokens
                step_open = True
                step_metered = False
                step_unbilled_failure = False
                step_events = 0

                content_parts: list[str] = []
                pending: dict[int, ToolCall] = {}
                failure: ProviderFailure | None = None
                finish_reason = "stop"
                cancelled = False

                # Close the provider stream deterministically so its own
                # cleanup (egress ledger row, HTTP stream release) runs when
                # this turn is closed or cancelled, not at GC.
                wall_exceeded = False
                wall_elapsed = 0.0
                async with _closing(self._provider.stream(request)) as stream:
                    polled = _PolledStream(stream)
                    try:
                        while True:
                            wall_elapsed = (self._time() - started_mono) - (approval_paused_ms / 1000.0)
                            wall_left = budget.max_wall_seconds - wall_elapsed
                            if wall_left <= 0 and step_events > 0:
                                # Also catches a provider that trickles events
                                # faster than the poll interval. The first read
                                # of a step follows the top-of-step check.
                                wall_exceeded = True
                                break
                            event = await polled.next(min(_STREAM_POLL_SECONDS, max(wall_left, 0.0)))
                            if event is _IDLE:
                                # No event within the poll interval: the stream is
                                # silent (keepalives only, or a stall). Re-check
                                # Stop and the wall budget without touching the
                                # pending read.
                                if cancel is not None and cancel.cancelled:
                                    cancelled = True
                                    break
                                wall_elapsed = (self._time() - started_mono) - (approval_paused_ms / 1000.0)
                                if wall_elapsed >= budget.max_wall_seconds:
                                    wall_exceeded = True
                                    break
                                continue
                            if event is _STREAM_DONE:
                                break
                            step_events += 1
                            if cancel is not None and cancel.cancelled:
                                cancelled = True
                                break
                            if isinstance(event, TextDelta):
                                content_parts.append(event.text)
                                yield events.TokenDelta(text=event.text, step=steps)
                            elif isinstance(event, ToolCallReady):
                                pending[event.index] = ToolCall(
                                    call_id=event.call_id,
                                    name=event.name,
                                    arguments=dict(event.arguments),
                                )
                            elif isinstance(event, UsageReport):
                                usage_reported = True
                                step_metered = True
                                input_tokens += event.input_tokens
                                output_tokens += event.output_tokens
                                yield events.UsageUpdated(
                                    model=model,
                                    input_tokens=event.input_tokens,
                                    output_tokens=event.output_tokens,
                                    total_input_tokens=input_tokens,
                                    total_output_tokens=output_tokens,
                                    estimated_cost_usd=self._config.estimate_cost(model, input_tokens, output_tokens),
                                    step=steps,
                                )
                            elif isinstance(event, ProviderFailure):
                                failure = event
                                # Exempt from the bound only when no request was
                                # accepted for billing: the step's first and only
                                # event is an HTTP status failure (the provider
                                # answered with an error status) or a failure in
                                # _UNBILLED_FAILURE_CODES (no request was made). Timeouts,
                                # transport errors and protocol errors carry no
                                # status and can follow an HTTP 200, so they are
                                # bounded.
                                step_unbilled_failure = step_events == 1 and (
                                    event.status is not None
                                    or event.code in _UNBILLED_FAILURE_CODES
                                )
                                break
                            elif isinstance(event, StreamEnd):
                                finish_reason = event.finish_reason
                    finally:
                        await polled.discard()

                if step_open and not step_metered and not step_unbilled_failure:
                    estimated_input_tokens += step_input_bound
                    estimated_output_tokens += step_output_bound
                    usage_estimated = True
                step_open = False

                if cancelled:
                    reason = "cancelled"
                    break

                if wall_exceeded:
                    reason = "budget"
                    yield _budget_error(
                        "max_wall_seconds",
                        f"Turn stopped after {wall_elapsed:.1f}s; limit is {budget.max_wall_seconds:.1f}s.",
                        steps,
                    )
                    break

                if failure is not None:
                    reason = "error"
                    details = {"status": failure.status} if failure.status is not None else None
                    yield events.Error(
                        code=failure.code,
                        message=failure.message,
                        retryable=failure.retryable,
                        step=steps,
                        details=details,
                    )
                    break

                if not pending:
                    final_message = "".join(content_parts)
                    reason = "length" if finish_reason == "length" else "stop"
                    break

                calls = [pending[index] for index in sorted(pending)]
                transcript.append(
                    {
                        "role": "assistant",
                        "content": "".join(content_parts) or None,
                        "tool_calls": [
                            {
                                "id": call.call_id,
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": json.dumps(
                                        dict(call.arguments),
                                        sort_keys=True,
                                        separators=(",", ":"),
                                        default=str,
                                    ),
                                },
                            }
                            for call in calls
                        ],
                    }
                )

                budget_exceeded = False
                for call in calls:
                    if cancel is not None and cancel.cancelled:
                        cancelled = True
                        break
                    if tool_calls_made >= budget.max_tool_calls:
                        budget_exceeded = True
                        break

                    yield events.ToolCallStarted(
                        call_id=call.call_id,
                        name=call.name,
                        arguments=dict(call.arguments),
                        step=steps,
                    )
                    call_started = self._time()
                    result, error = await self._execute(call)
                    ok = bool(result.get("ok", True)) if result is not None else False
                    if error is None and result is not None:
                        raw_error = result.get("error")
                        if isinstance(raw_error, Mapping):
                            error = dict(raw_error)
                    duration_ms = _elapsed_ms(call_started, self._time())
                    waited_ms = _approval_wait_ms(self._tool_executor, call.call_id, duration_ms)
                    approval_paused_ms += waited_ms
                    tool_calls_made += 1
                    yield events.ToolCallFinished(
                        call_id=call.call_id,
                        name=call.name,
                        ok=ok,
                        duration_ms=duration_ms,
                        step=steps,
                        result=_bounded(result),
                        error=error,
                        waited_ms=waited_ms,
                    )
                    transcript.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.call_id,
                            "name": call.name,
                            "content": _tool_content(result),
                        }
                    )

                if cancelled:
                    reason = "cancelled"
                    break

                if budget_exceeded:
                    reason = "budget"
                    yield _budget_error(
                        "max_tool_calls",
                        f"Turn stopped at {tool_calls_made} tool call(s); limit is {budget.max_tool_calls}.",
                        steps,
                    )
                    break

            recorded = True
            cost, duration_ms, persisted = self._record_usage(
                turn_id=turn_id,
                session_id=session_id,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_input_tokens=estimated_input_tokens,
                estimated_output_tokens=estimated_output_tokens,
                usage_reported=usage_reported,
                usage_estimated=usage_estimated,
                steps=steps,
                tool_calls=tool_calls_made,
                started_mono=started_mono,
                started_at=started_at,
                outcome=reason,
            )
        except (GeneratorExit, asyncio.CancelledError):
            # The consumer closed the generator or the task was cancelled.
            abort_outcome = "cancelled"
            raise
        except BaseException:
            # Any other escape (e.g. the provider stream raised) is a failed
            # turn, not a cancelled one.
            abort_outcome = "error"
            raise
        finally:
            if not recorded:
                # The turn ended before the normal accounting path.
                # Provider-billed steps must still be accounted, and nothing
                # may be yielded or awaited from here. A step whose stream was
                # open and unmetered at exit may already have been billed.
                if step_open and not step_metered and not step_unbilled_failure:
                    estimated_input_tokens += step_input_bound
                    estimated_output_tokens += step_output_bound
                    usage_estimated = True
                self._record_usage(
                    turn_id=turn_id,
                    session_id=session_id,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    estimated_input_tokens=estimated_input_tokens,
                    estimated_output_tokens=estimated_output_tokens,
                    usage_reported=usage_reported,
                    usage_estimated=usage_estimated,
                    steps=steps,
                    tool_calls=tool_calls_made,
                    started_mono=started_mono,
                    started_at=started_at,
                    outcome=abort_outcome,
                )

        yield events.TurnFinished(
            turn_id=turn_id,
            reason=reason,
            steps=steps,
            tool_calls=tool_calls_made,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=cost,
            duration_ms=duration_ms,
            message=final_message,
            usage_persisted=persisted,
        )

    def _record_usage(
        self,
        *,
        turn_id: str,
        session_id: str | None,
        model: str,
        input_tokens: int,
        output_tokens: int,
        estimated_input_tokens: int,
        estimated_output_tokens: int,
        usage_reported: bool,
        usage_estimated: bool,
        steps: int,
        tool_calls: int,
        started_mono: float,
        started_at: datetime,
        outcome: str,
    ) -> tuple[float | None, int, bool]:
        """Build and persist the turn's one ``UsageRecord``.

        ``input_tokens``/``output_tokens`` are provider-reported counts; the
        ``estimated_*`` bounds cover steps that reached the provider without a
        ``UsageReport``. The record and its cost use the sum, so billed work is
        never recorded as ``$0``; ``None`` stays ``None`` for an unpriced model.

        Returns:
            ``(estimated_cost_usd, duration_ms, persisted)``. A store failure
            never raises.
        """

        duration_ms = _elapsed_ms(started_mono, self._time())
        input_tokens += estimated_input_tokens
        output_tokens += estimated_output_tokens
        cost = self._config.estimate_cost(model, input_tokens, output_tokens)
        record = UsageRecord(
            turn_id=turn_id,
            session_id=session_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            estimated_cost_usd=cost,
            cost_known=self._config.pricing_for(model) is not None,
            usage_reported=usage_reported,
            steps=steps,
            tool_calls=tool_calls,
            started_at=started_at.isoformat(),
            finished_at=self._now().isoformat(),
            duration_ms=duration_ms,
            outcome=outcome,
            usage_estimated=usage_estimated,
        )
        persisted = self._usage_store is not None
        if self._usage_store is not None:
            try:
                self._usage_store.record(record)
            except Exception:  # pragma: no cover - accounting must not kill a turn
                persisted = False
        return cost, duration_ms, persisted

    async def _execute(
        self, call: ToolCall
    ) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
        if self._tool_executor is None:
            return (
                {"ok": False, "error": {"code": "no_tool_executor"}},
                {
                    "code": "no_tool_executor",
                    "message": f"No executor is configured for {call.name!r}.",
                },
            )
        try:
            result = await self._tool_executor.execute(call)
        except Exception as exc:  # noqa: BLE001 - tool failures become observations
            return (
                {"ok": False, "error": {"code": "tool_error"}},
                {
                    "code": "tool_error",
                    "message": f"Tool {call.name!r} failed ({type(exc).__name__}).",
                },
            )
        if not isinstance(result, Mapping):
            return (
                {"ok": False, "error": {"code": "invalid_tool_output"}},
                {
                    "code": "invalid_tool_output",
                    "message": f"Tool {call.name!r} returned a non-object result.",
                },
            )
        return result, None


def _budget_error(limit: str, message: str, step: int) -> events.Error:
    return events.Error(
        code="turn_budget_exceeded",
        message=message,
        retryable=False,
        step=step,
        details={"limit": limit},
    )


_IDLE = object()
_STREAM_DONE = object()


def _start_read(coro: Any) -> asyncio.Task[Any]:
    """Start ``coro`` as a task, running it to its first suspension at once.

    On Python 3.12+ an eager task whose read needs no suspension (a provider
    that already has its next event buffered) finishes before this returns, so
    the common path costs no event-loop round trip and yields no scheduling
    point beyond what iterating the stream directly had. Python 3.11 has no
    eager start and falls back to a regular task.
    """

    if sys.version_info >= (3, 12):
        return asyncio.Task(coro, loop=asyncio.get_running_loop(), eager_start=True)
    return asyncio.ensure_future(coro)  # type: ignore[no-any-return]


class _PolledStream:
    """Reads a provider stream so the caller can wake while it is silent.

    Each read is an ``asyncio`` task over ``__anext__``. ``next`` waits on it
    for at most ``timeout`` seconds and returns ``_IDLE`` if nothing arrived;
    the same task is waited on again by the next call, so a slow read is never
    cancelled just to poll. ``discard`` cancels a read still in flight, for an
    early exit, so the provider's own ``finally`` can run.
    """

    def __init__(self, stream: Any) -> None:
        self._iterator = stream.__aiter__()
        self._task: asyncio.Task[Any] | None = None

    async def _read(self) -> Any:
        return await self._iterator.__anext__()

    async def next(self, timeout: float) -> Any:
        """Return the next event, ``_IDLE`` on timeout, or ``_STREAM_DONE``.

        Any exception the provider raises from ``__anext__`` propagates.
        """

        if self._task is None:
            self._task = _start_read(self._read())
        if not self._task.done():
            done, _ = await asyncio.wait({self._task}, timeout=timeout)
            if not done:
                return _IDLE
        task, self._task = self._task, None
        try:
            return task.result()
        except StopAsyncIteration:
            return _STREAM_DONE

    async def discard(self) -> None:
        """Cancel and reap a read still in flight; never raises its outcome."""

        task, self._task = self._task, None
        if task is None:
            return
        if not task.done():
            task.cancel()
            # asyncio.wait does not re-raise the task's outcome, and still
            # propagates a cancellation aimed at the caller.
            await asyncio.wait({task})
        if not task.cancelled():
            # Mark any late provider exception as retrieved.
            task.exception()


def _closing(stream: Any) -> Any:
    """Async context manager that ``aclose()``s ``stream`` on exit when it can.

    Provider streams are normally async generators, whose ``finally`` blocks
    (egress ledger row, HTTP stream release) must run when a turn is closed or
    cancelled. Plain async iterators without ``aclose`` are left alone.
    """

    if callable(getattr(stream, "aclose", None)):
        return contextlib.aclosing(stream)
    return contextlib.nullcontext(stream)


def _request_input_bound(
    transcript: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]
) -> int:
    """Upper bound on a request's input tokens: its serialized UTF-8 size.

    A token covers at least one byte, and the JSON keys and quotes outweigh
    chat-template tokens, so the byte count of the messages and tools is above
    the billed input for what the loop sends (an adapter's own system prompt
    is not counted).
    """

    payload = {"messages": transcript, "tools": tools}
    try:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        # Non-serializable keys or a cycle: still count something finite
        # rather than let accounting fail the turn.
        text = repr(payload)
    return len(text.encode("utf-8", errors="replace"))


def _elapsed_ms(started: float, now: float) -> int:
    return max(0, int(round((now - started) * 1000)))


def _approval_wait_ms(executor: Any, call_id: str, duration_ms: int) -> int:
    """Ask a capable executor how much of a call was spent awaiting approval."""

    getter = getattr(executor, "approval_wait_ms", None)
    if not callable(getter):
        return 0
    try:
        waited = int(getter(call_id) or 0)
    except Exception:  # pragma: no cover - accounting must not kill a turn
        return 0
    return max(0, min(waited, duration_ms))


def _bounded(value: Mapping[str, Any] | None, limit: int = MAX_TOOL_CONTENT_CHARS) -> Mapping[str, Any] | None:
    if value is None:
        return None
    text = json.dumps(value, default=str, sort_keys=True)
    if len(text) <= limit:
        return json.loads(text)
    return {"truncated": True, "preview": text[:limit]}


def _tool_content(result: Mapping[str, Any] | None) -> str:
    text = json.dumps(result if result is not None else {}, default=str, sort_keys=True)
    if len(text) <= MAX_TOOL_CONTENT_CHARS:
        return text
    return text[:MAX_TOOL_CONTENT_CHARS] + "…[truncated]"

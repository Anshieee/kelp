"""Streaming agent loop: model step -> tool calls -> observations -> finish.

Contract highlights (KEL-6 / P0-1):

- One async generator per user turn; it never raises for provider or tool errors.
- Hard budgets: model steps, output tokens, tool calls, wall-clock seconds.
- Tool calls are executed through an injected executor; the loop does not
  special-case skill names.
- Every turn writes one usage record (tokens in/out, estimated cost, outcome).
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, Mapping, Protocol, Sequence

from . import events
from .config import KernelConfig, estimate_cost, pricing_for
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
    "RegistryToolExecutor",
    "ToolExecutor",
    "TurnBudget",
]

MAX_TOOL_CONTENT_CHARS = 20_000


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
    """Runs tool calls through the KEL-5 skill registry, including approvals."""

    def __init__(
        self,
        registry: "SkillRegistry",
        *,
        approval: Callable[[Any], Awaitable[bool]] | None = None,
        secrets: Mapping[str, str] | None = None,
        workspace_root: str | None = None,
    ):
        self._registry = registry
        self._approval = approval
        self._secrets = secrets
        self._workspace_root = workspace_root

    async def execute(self, call: ToolCall) -> Mapping[str, Any]:
        return await self._registry.invoke(
            call.name,
            dict(call.arguments),
            approval=self._approval,
            secrets=self._secrets,
            workspace_root=self._workspace_root,
        )


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
        reason = "stop"
        final_message: str | None = None
        transcript: list[dict[str, Any]] = [dict(message) for message in messages]

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
            if steps >= budget.max_steps:
                reason = "budget"
                yield _budget_error(
                    "max_steps",
                    f"Turn stopped after {steps} model step(s); limit is {budget.max_steps}.",
                    steps,
                )
                break

            elapsed = self._time() - started_mono
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

            content_parts: list[str] = []
            pending: dict[int, ToolCall] = {}
            failure: ProviderFailure | None = None
            finish_reason = "stop"

            async for event in self._provider.stream(request):
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
                    input_tokens += event.input_tokens
                    output_tokens += event.output_tokens
                    yield events.UsageUpdated(
                        model=model,
                        input_tokens=event.input_tokens,
                        output_tokens=event.output_tokens,
                        total_input_tokens=input_tokens,
                        total_output_tokens=output_tokens,
                        estimated_cost_usd=estimate_cost(model, input_tokens, output_tokens),
                        step=steps,
                    )
                elif isinstance(event, ProviderFailure):
                    failure = event
                    break
                elif isinstance(event, StreamEnd):
                    finish_reason = event.finish_reason

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
                tool_calls_made += 1
                yield events.ToolCallFinished(
                    call_id=call.call_id,
                    name=call.name,
                    ok=ok,
                    duration_ms=duration_ms,
                    step=steps,
                    result=_bounded(result),
                    error=error,
                )
                transcript.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "name": call.name,
                        "content": _tool_content(result),
                    }
                )

            if budget_exceeded:
                reason = "budget"
                yield _budget_error(
                    "max_tool_calls",
                    f"Turn stopped at {tool_calls_made} tool call(s); limit is {budget.max_tool_calls}.",
                    steps,
                )
                break

        duration_ms = _elapsed_ms(started_mono, self._time())
        cost = estimate_cost(model, input_tokens, output_tokens)
        record = UsageRecord(
            turn_id=turn_id,
            session_id=session_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            estimated_cost_usd=cost,
            cost_known=pricing_for(model) is not None,
            usage_reported=usage_reported,
            steps=steps,
            tool_calls=tool_calls_made,
            started_at=started_at.isoformat(),
            finished_at=self._now().isoformat(),
            duration_ms=duration_ms,
            outcome=reason,
        )
        persisted = self._usage_store is not None
        if self._usage_store is not None:
            try:
                self._usage_store.record(record)
            except Exception:  # pragma: no cover - accounting must not kill a turn
                persisted = False

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


def _elapsed_ms(started: float, now: float) -> int:
    return max(0, int(round((now - started) * 1000)))


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

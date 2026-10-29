"""Typed events yielded by the kernel agent loop.

The loop is an async generator of these events. Surfaces (dashboard, Telegram,
CLI) translate them; none of them are required here. Every event is JSON-safe
via ``to_dict()``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, ClassVar, Mapping

__all__ = [
    "Error",
    "TokenDelta",
    "ToolCallFinished",
    "ToolCallStarted",
    "TurnFinished",
    "TurnStarted",
    "UsageUpdated",
]


class _Event:
    kind: ClassVar[str]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)  # type: ignore[call-overload]
        payload["kind"] = self.kind
        return payload


@dataclass(frozen=True)
class TurnStarted(_Event):
    kind: ClassVar[str] = "turn_started"

    turn_id: str
    session_id: str | None
    model: str
    max_steps: int
    max_output_tokens: int
    max_tool_calls: int
    max_wall_seconds: float


@dataclass(frozen=True)
class TokenDelta(_Event):
    kind: ClassVar[str] = "token_delta"

    text: str
    step: int


@dataclass(frozen=True)
class ToolCallStarted(_Event):
    kind: ClassVar[str] = "tool_call_started"

    call_id: str
    name: str
    arguments: Mapping[str, Any]
    step: int


@dataclass(frozen=True)
class ToolCallFinished(_Event):
    kind: ClassVar[str] = "tool_call_finished"

    call_id: str
    name: str
    ok: bool
    duration_ms: int
    step: int
    result: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class UsageUpdated(_Event):
    kind: ClassVar[str] = "usage_updated"

    model: str
    input_tokens: int
    output_tokens: int
    total_input_tokens: int
    total_output_tokens: int
    estimated_cost_usd: float | None
    step: int


@dataclass(frozen=True)
class Error(_Event):
    kind: ClassVar[str] = "error"

    code: str
    message: str
    retryable: bool
    step: int
    details: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class TurnFinished(_Event):
    kind: ClassVar[str] = "turn_finished"

    turn_id: str
    reason: str  # stop | length | budget | error | cancelled
    steps: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float | None
    duration_ms: int
    message: str | None
    usage_persisted: bool

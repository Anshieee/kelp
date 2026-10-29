"""Result objects shared by scenarios, metrics, and report writing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["Check", "Observation", "TaskResult"]


@dataclass
class Check:
    name: str
    ok: bool
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": self.name, "ok": bool(self.ok)}
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass
class Observation:
    checks: list[Check] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    approval: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def add(self, name: str, ok: bool, detail: str | None = None) -> None:
        self.checks.append(Check(name=name, ok=bool(ok), detail=detail))


@dataclass
class TaskResult:
    task_id: str
    category: str
    name: str
    passed: bool
    checks: list[dict[str, Any]]
    duration_ms: int
    durations_ms: list[int]
    citations: list[str]
    expected_citations: list[str]
    tool_calls: list[dict[str, Any]]
    expected_tool_calls: list[dict[str, Any]]
    tool_score: float
    approval: dict[str, Any]
    input_tokens: int
    output_tokens: int
    cost_usd: float
    cost_known: bool
    error: str | None
    deterministic: bool
    repeat_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.task_id,
            "category": self.category,
            "name": self.name,
            "passed": bool(self.passed),
            "checks": self.checks,
            "duration_ms": int(self.duration_ms),
            "durations_ms": list(self.durations_ms),
            "citations": list(self.citations),
            "expected_citations": list(self.expected_citations),
            "tool_calls": list(self.tool_calls),
            "expected_tool_calls": list(self.expected_tool_calls),
            "tool_score": round(float(self.tool_score), 6),
            "approval": dict(self.approval),
            "input_tokens": int(self.input_tokens),
            "output_tokens": int(self.output_tokens),
            "cost_usd": round(float(self.cost_usd), 8),
            "cost_known": bool(self.cost_known),
            "error": self.error,
            "deterministic": bool(self.deterministic),
            "repeat_count": int(self.repeat_count),
        }

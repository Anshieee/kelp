"""Recorded providers: deterministic stand-ins for the model service.

A script is a sequence of *turns*; each turn is a sequence of model *steps*;
each step is a sequence of provider events. The provider pops one turn when the
transcript ends with a user message and advances one step whenever the loop
comes back with a tool result. Tool arguments may contain ``{{name}}``
placeholders, resolved from the demo's substitution table — that is how a
scripted tool call can name an artifact handle that only exists at run time.
"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from typing import Any, Mapping, Sequence

from kernel.provider import StreamEnd, TextDelta, ToolCallReady, UsageReport

__all__ = [
    "FixtureProvider",
    "SlowTextProvider",
    "text_script",
    "tool_script",
]

_PLACEHOLDER_RE = re.compile(r"\{\{([a-z0-9_]+)\}\}")

Turn = Sequence[Sequence[Any]]


def _substitute(value: Any, table: Mapping[str, str]) -> Any:
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in table:
                raise KeyError(f"script placeholder {{{{{key}}}}} has no value")
            return table[key]

        return _PLACEHOLDER_RE.sub(replace, value)
    if isinstance(value, Mapping):
        return {key: _substitute(item, table) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_substitute(item, table) for item in value]
    return value


def tool_script(
    name: str,
    arguments: Mapping[str, Any],
    *,
    call_id: str,
    usage: tuple[int, int] = (180, 24),
) -> list[Any]:
    return [
        ToolCallReady(index=0, call_id=call_id, name=name, arguments=dict(arguments)),
        UsageReport(usage[0], usage[1], usage[0] + usage[1]),
        StreamEnd("tool_calls"),
    ]


def text_script(text: str, *, usage: tuple[int, int] = (60, 40)) -> list[Any]:
    return [
        TextDelta(text),
        UsageReport(usage[0], usage[1], usage[0] + usage[1]),
        StreamEnd("stop"),
    ]


class FixtureProvider:
    def __init__(
        self,
        turns: Sequence[Turn],
        *,
        substitutions: Mapping[str, str] | None = None,
    ):
        self._turns = deque(turns)
        # Held by reference: the demo adds artifact handles after start-up.
        self._table = substitutions if substitutions is not None else {}
        self._current: list[Sequence[Any]] | None = None
        self._step = 0
        self.requests = 0

    async def stream(self, request: Any) -> Any:
        self.requests += 1
        messages = list(request.messages)
        if not messages or messages[-1].get("role") != "user":
            if self._current is None:
                raise RuntimeError("fixture provider got a step with no open turn")
            self._step += 1
        else:
            if not self._turns:
                raise RuntimeError("fixture provider ran out of scripted turns")
            self._current = [list(step) for step in self._turns.popleft()]
            self._step = 0
        if self._step >= len(self._current):
            raise RuntimeError(
                f"fixture script has {len(self._current)} step(s); the loop asked for step {self._step + 1}"
            )
        for event in self._current[self._step]:
            if isinstance(event, ToolCallReady):
                event = ToolCallReady(
                    index=event.index,
                    call_id=event.call_id,
                    name=event.name,
                    arguments=_substitute(dict(event.arguments), self._table),
                )
            yield event


class SlowTextProvider:
    """Streams word by word so a turn is reliably mid-flight when it is killed."""

    def __init__(self, words: Sequence[str], *, delay: float = 0.03):
        self._words = list(words)
        self._delay = delay
        self.requests = 0

    async def stream(self, request: Any) -> Any:
        self.requests += 1
        for word in self._words:
            await asyncio.sleep(self._delay)
            yield TextDelta(word + " ")
        yield UsageReport(120, len(self._words), 120 + len(self._words))
        yield StreamEnd("stop")

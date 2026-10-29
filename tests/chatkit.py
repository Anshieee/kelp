"""Shared helpers for the KEL-8 chat tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from chat import ChatService, ChatStore
from kernel import CallableToolExecutor
from kernel.provider import (
    ChatRequest,
    StreamEnd,
    TextDelta,
    ToolCallReady,
    UsageReport,
)

MODEL = "nvidia/Nemotron-3_5-Super-VL"


class StubProvider:
    def __init__(self, scripts: Sequence[Sequence[Any]]):
        self.scripts = list(scripts)
        self.requests: list[ChatRequest] = []

    async def stream(self, request: ChatRequest) -> Any:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.scripts) - 1)
        for event in self.scripts[index]:
            yield event


def text_script(text: str, *, usage: tuple[int, int] = (10, 5)) -> list[Any]:
    return [
        TextDelta(text),
        UsageReport(usage[0], usage[1], usage[0] + usage[1]),
        StreamEnd("stop"),
    ]


def tool_script(
    name: str,
    arguments: Mapping[str, Any],
    *,
    call_id: str = "call_1",
    index: int = 0,
    usage: tuple[int, int] = (10, 5),
) -> list[Any]:
    return [
        ToolCallReady(index=index, call_id=call_id, name=name, arguments=dict(arguments)),
        UsageReport(usage[0], usage[1], usage[0] + usage[1]),
        StreamEnd("tool_calls"),
    ]


def executor_factory(
    handler: Callable[[Any], Awaitable[Mapping[str, Any]]]
) -> Callable[[Any], Any]:
    return lambda _hook: CallableToolExecutor(handler)


def make_service(
    tmp_path: Path,
    scripts: Sequence[Sequence[Any]],
    **kwargs: Any,
) -> tuple[ChatService, ChatStore, StubProvider]:
    store = ChatStore(tmp_path / "chat.db")
    provider = StubProvider(scripts)
    service = ChatService(
        store=store,
        provider=provider,
        model=MODEL,
        **kwargs,
    )
    return service, store, provider


async def send_and_wait(
    service: ChatService,
    thread_id: str,
    text: str = "hi",
    *,
    client_turn_id: str = "ct-1234abcd",
    timeout: float = 5.0,
) -> dict[str, Any]:
    payload, _created = await service.send(
        thread_id, text=text, client_turn_id=client_turn_id
    )
    await service.wait_for_turn(payload["turn_id"], timeout=timeout)
    return payload


async def wait_until(
    predicate: Callable[[], Any], *, timeout: float = 3.0, interval: float = 0.01
) -> Any:
    elapsed = 0.0
    while elapsed < timeout:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(interval)
        elapsed += interval
    raise AssertionError("condition was not met before the timeout")

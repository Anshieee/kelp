"""Tests for the kernel agent loop, budgets, and usage accounting (KEL-6)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from kernel import (
    AgentLoop,
    CallableToolExecutor,
    InMemoryUsageStore,
    KernelConfig,
    JsonlUsageStore,
    RegistryToolExecutor,
    TurnBudget,
)
from kernel import events
from kernel.provider import (
    ChatRequest,
    ProviderFailure,
    StreamEnd,
    TextDelta,
    ToolCall,
    ToolCallReady,
    UsageReport,
)

MODEL = "nvidia/Nemotron-3_5-Super-VL"


def text_script(text: str, *, usage: tuple[int, int] = (10, 5), finish: str = "stop") -> list[Any]:
    return [
        TextDelta(text),
        UsageReport(usage[0], usage[1], usage[0] + usage[1]),
        StreamEnd(finish),
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
        ToolCallReady(index=index, call_id=call_id, name=name, arguments=arguments),
        UsageReport(usage[0], usage[1], usage[0] + usage[1]),
        StreamEnd("tool_calls"),
    ]


class StubProvider:
    def __init__(self, scripts: Sequence[Sequence[Any]]):
        self._scripts = list(scripts)
        self.requests: list[ChatRequest] = []

    async def stream(self, request: ChatRequest) -> Any:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self._scripts) - 1)
        for event in self._scripts[index]:
            yield event


def collect(loop: AgentLoop, messages: Sequence[Mapping[str, Any]], **kwargs: Any) -> list[Any]:
    async def run() -> list[Any]:
        return [event async for event in loop.run_turn(messages, **kwargs)]

    return asyncio.run(run())


def build(
    scripts: Sequence[Sequence[Any]],
    *,
    budget: TurnBudget | None = None,
    executor: Any = None,
    store: Any = None,
    config: KernelConfig | None = None,
    **kwargs: Any,
) -> tuple[AgentLoop, StubProvider]:
    provider = StubProvider(scripts)
    loop = AgentLoop(
        provider=provider,
        config=config or KernelConfig(api_key="test-key"),
        tool_executor=executor,
        budget=budget,
        usage_store=store,
        model=kwargs.pop("model", MODEL),
        **kwargs,
    )
    return loop, provider


def kinds(events_list: Sequence[Any]) -> list[str]:
    return [event.kind for event in events_list]


def test_zero_tool_calls_completes_and_accounts_usage() -> None:
    store = InMemoryUsageStore()
    loop, provider = build([text_script("Hello there")], store=store)
    result = collect(loop, [{"role": "user", "content": "hi"}], session_id="s1", turn_id="t1")

    assert kinds(result) == ["turn_started", "token_delta", "usage_updated", "turn_finished"]
    finished = result[-1]
    assert finished.reason == "stop"
    assert finished.message == "Hello there"
    assert finished.steps == 1
    assert finished.input_tokens == 10
    assert finished.output_tokens == 5
    assert finished.estimated_cost_usd == pytest.approx(7.5e-06)
    assert finished.usage_persisted is True
    assert len(store.records) == 1
    record = store.records[0]
    assert record.turn_id == "t1"
    assert record.session_id == "s1"
    assert record.model == MODEL
    assert record.total_tokens == 15
    assert record.cost_known is True
    assert record.usage_reported is True
    assert record.outcome == "stop"
    assert provider.requests[0].max_tokens == 1024


def test_one_tool_call_completes_end_to_end() -> None:
    calls: list[ToolCall] = []

    async def executor(call: ToolCall) -> Mapping[str, Any]:
        calls.append(call)
        return {"ok": True, "results": [{"title": "Nebius"}]}

    loop, provider = build(
        [tool_script("web.search", {"query": "nebius"}), text_script("Found one.")],
        executor=CallableToolExecutor(executor),
    )
    result = collect(loop, [{"role": "user", "content": "search"}])
    event_kinds = kinds(result)

    assert event_kinds == [
        "turn_started",
        "usage_updated",
        "tool_call_started",
        "tool_call_finished",
        "token_delta",
        "usage_updated",
        "turn_finished",
    ]
    assert calls == [ToolCall(call_id="call_1", name="web.search", arguments={"query": "nebius"})]
    started = [e for e in result if e.kind == "tool_call_started"][0]
    assert started.arguments == {"query": "nebius"}
    finished = [e for e in result if e.kind == "tool_call_finished"][0]
    assert finished.ok is True
    assert result[-1].message == "Found one."
    assert result[-1].tool_calls == 1
    assert result[-1].steps == 2
    assert len(provider.requests) == 2

    # The second model call sees the assistant tool call and the observation.
    second = provider.requests[1].messages
    assert second[1]["role"] == "assistant"
    assert second[1]["tool_calls"][0]["function"]["name"] == "web.search"
    assert second[2]["role"] == "tool"
    assert json.loads(second[2]["content"])["ok"] is True


def test_three_sequential_tool_calls_complete() -> None:
    async def executor(call: ToolCall) -> Mapping[str, Any]:
        return {"ok": True, "step": call.arguments.get("n")}

    scripts = [
        tool_script("stub.tool", {"n": 1}, call_id="call_1"),
        tool_script("stub.tool", {"n": 2}, call_id="call_2"),
        tool_script("stub.tool", {"n": 3}, call_id="call_3"),
        text_script("done"),
    ]
    loop, provider = build(scripts, executor=CallableToolExecutor(executor))
    result = collect(loop, [{"role": "user", "content": "go"}])

    assert len([e for e in result if e.kind == "tool_call_finished"]) == 3
    assert len(provider.requests) == 4
    assert result[-1].tool_calls == 3
    assert result[-1].steps == 4
    assert result[-1].message == "done"
    assert result[-1].reason == "stop"


def test_max_steps_budget_is_enforced() -> None:
    loop, provider = build(
        [tool_script("stub.tool", {"n": 1})],
        budget=TurnBudget(max_steps=2, max_tool_calls=10),
        executor=CallableToolExecutor(lambda call: _ok()),
    )
    result = collect(loop, [{"role": "user", "content": "loop"}])

    errors = [e for e in result if e.kind == "error"]
    assert len(errors) == 1
    assert errors[0].code == "turn_budget_exceeded"
    assert errors[0].details == {"limit": "max_steps"}
    assert len(provider.requests) == 2
    assert result[-1].reason == "budget"
    assert result[-1].steps == 2


def test_output_token_budget_shrinks_step_and_then_stops() -> None:
    loop, provider = build(
        [tool_script("stub.tool", {"n": 1}, usage=(10, 100))],
        budget=TurnBudget(max_steps=10, max_output_tokens=150, max_tokens_per_step=1024, max_tool_calls=10),
        executor=CallableToolExecutor(lambda call: _ok()),
    )
    result = collect(loop, [{"role": "user", "content": "spend"}])

    assert [request.max_tokens for request in provider.requests] == [150, 50]
    errors = [e for e in result if e.kind == "error"]
    assert errors[0].details == {"limit": "max_output_tokens"}
    assert result[-1].reason == "budget"
    assert result[-1].output_tokens == 200


def test_max_tool_calls_budget_is_enforced() -> None:
    script = [
        ToolCallReady(0, "call_1", "stub.tool", {"n": 1}),
        ToolCallReady(1, "call_2", "stub.tool", {"n": 2}),
        UsageReport(10, 5, 15),
        StreamEnd("tool_calls"),
    ]
    loop, _provider = build(
        [script],
        budget=TurnBudget(max_steps=5, max_tool_calls=1),
        executor=CallableToolExecutor(lambda call: _ok()),
    )
    result = collect(loop, [{"role": "user", "content": "two at once"}])

    finished = [e for e in result if e.kind == "tool_call_finished"]
    assert len(finished) == 1
    errors = [e for e in result if e.kind == "error"]
    assert errors[0].details == {"limit": "max_tool_calls"}
    assert result[-1].reason == "budget"
    assert result[-1].tool_calls == 1


def test_wall_clock_budget_is_enforced() -> None:
    class Clock:
        def __init__(self) -> None:
            self.calls = 0
            self.value = 0.0

        def __call__(self) -> float:
            self.calls += 1
            if self.calls > 2:
                self.value += 60.0
            return self.value

    loop, provider = build(
        [tool_script("stub.tool", {"n": 1})],
        budget=TurnBudget(max_steps=10, max_wall_seconds=50),
        executor=CallableToolExecutor(lambda call: _ok()),
        time_fn=Clock(),
    )
    result = collect(loop, [{"role": "user", "content": "slow"}])

    errors = [e for e in result if e.kind == "error"]
    assert errors[0].details == {"limit": "max_wall_seconds"}
    assert len(provider.requests) == 1
    assert result[-1].reason == "budget"


def test_provider_failure_mid_stream_is_visible_and_session_stays_usable() -> None:
    failing = [
        TextDelta("par"),
        ProviderFailure("provider_timeout", "Token Factory timed out.", retryable=True),
    ]
    loop, _provider = build([failing])
    first = collect(loop, [{"role": "user", "content": "hi"}])

    assert kinds(first) == ["turn_started", "token_delta", "error", "turn_finished"]
    error = first[2]
    assert error.retryable is True
    assert first[-1].reason == "error"
    assert first[-1].message is None

    # Same loop instance remains usable for the next turn.
    second = collect(loop, [{"role": "user", "content": "again"}])
    assert second[-1].reason == "error"  # stub still returns the failing script

    good_loop, _ = build([text_script("recovered")])
    assert collect(good_loop, [{"role": "user", "content": "hi"}])[-1].message == "recovered"


def test_tool_exception_becomes_an_observation() -> None:
    async def executor(call: ToolCall) -> Mapping[str, Any]:
        raise RuntimeError("boom")

    loop, provider = build(
        [tool_script("stub.tool", {}), text_script("after failure")],
        executor=CallableToolExecutor(executor),
    )
    result = collect(loop, [{"role": "user", "content": "go"}])

    finished = [e for e in result if e.kind == "tool_call_finished"][0]
    assert finished.ok is False
    assert finished.error["code"] == "tool_error"
    observation = json.loads(provider.requests[1].messages[-1]["content"])
    assert observation["ok"] is False
    assert result[-1].message == "after failure"


def test_missing_executor_is_a_typed_observation() -> None:
    loop, provider = build([tool_script("stub.tool", {}), text_script("ok")])
    result = collect(loop, [{"role": "user", "content": "go"}])

    finished = [e for e in result if e.kind == "tool_call_finished"][0]
    assert finished.ok is False
    assert finished.error["code"] == "no_tool_executor"
    assert result[-1].message == "ok"


def test_jsonl_usage_store_contains_no_secrets(tmp_path: Path) -> None:
    path = tmp_path / "usage" / "turns.jsonl"
    secret = "super-secret-api-key-value"
    loop, _ = build(
        [text_script("done")],
        store=JsonlUsageStore(path),
        config=KernelConfig(api_key=secret),
    )
    collect(loop, [{"role": "user", "content": "hi"}], turn_id="turn-1", session_id="sess-1")

    raw = path.read_text(encoding="utf-8")
    assert secret not in raw
    record = json.loads(raw.strip())
    assert record["turn_id"] == "turn-1"
    assert record["outcome"] == "stop"
    assert record["total_tokens"] == 15


def test_unknown_model_cost_is_unknown() -> None:
    store = InMemoryUsageStore()
    loop, _ = build([text_script("done")], store=store, model="unknown/model")
    result = collect(loop, [{"role": "user", "content": "hi"}])
    assert result[-1].estimated_cost_usd is None
    assert store.records[0].cost_known is False
    assert store.records[0].estimated_cost_usd is None


def test_usage_store_failure_does_not_break_the_turn() -> None:
    class BrokenStore:
        def record(self, record: Any) -> None:
            raise OSError("disk full")

    loop, _ = build([text_script("done")], store=BrokenStore())
    result = collect(loop, [{"role": "user", "content": "hi"}])
    assert result[-1].message == "done"
    assert result[-1].usage_persisted is False


def test_no_usage_store_is_reported_as_not_persisted() -> None:
    loop, _ = build([text_script("done")])
    result = collect(loop, [{"role": "user", "content": "hi"}])
    assert result[-1].usage_persisted is False


def test_events_are_json_serializable() -> None:
    async def executor(call: ToolCall) -> Mapping[str, Any]:
        return {"ok": True, "value": call.arguments.get("n")}

    loop, _ = build(
        [tool_script("stub.tool", {"n": 1}), text_script("done")],
        executor=CallableToolExecutor(executor),
    )
    result = collect(loop, [{"role": "user", "content": "hi"}])
    payload = json.dumps([event.to_dict() for event in result])
    assert '"kind": "turn_finished"' in payload


def test_registry_tool_executor_passes_contract_kwargs() -> None:
    class FakeRegistry:
        def __init__(self) -> None:
            self.seen: dict[str, Any] = {}

        async def invoke(self, name: str, arguments: Any, **kwargs: Any) -> dict[str, Any]:
            self.seen = {"name": name, "arguments": arguments, **kwargs}
            return {"ok": True}

    registry = FakeRegistry()
    executor = RegistryToolExecutor(
        registry,  # type: ignore[arg-type]
        approval=None,
        secrets={"TAVILY_API_KEY": "x"},
        workspace_root="/tmp/ws",
    )
    call = ToolCall("call_1", "web.search", {"query": "q"})

    async def run() -> Mapping[str, Any]:
        return await executor.execute(call)

    result = asyncio.run(run())
    assert result == {"ok": True}
    assert registry.seen["name"] == "web.search"
    assert registry.seen["arguments"] == {"query": "q"}
    assert registry.seen["secrets"] == {"TAVILY_API_KEY": "x"}
    assert registry.seen["workspace_root"] == "/tmp/ws"


def test_budget_validation() -> None:
    with pytest.raises(ValueError):
        TurnBudget(max_steps=0)
    with pytest.raises(ValueError):
        TurnBudget(max_output_tokens=0)
    with pytest.raises(ValueError):
        TurnBudget(max_tool_calls=-1)
    with pytest.raises(ValueError):
        TurnBudget(max_wall_seconds=0)


async def _ok() -> Mapping[str, Any]:
    return {"ok": True}

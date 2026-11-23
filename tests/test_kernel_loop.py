"""Tests for the kernel agent loop, budgets, and usage accounting (KEL-6)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import httpx
import pytest

from kernel import (
    AgentLoop,
    CallableToolExecutor,
    CancelToken,
    InMemoryUsageStore,
    KernelConfig,
    JsonlUsageStore,
    RegistryToolExecutor,
    TurnBudget,
)
from kernel.loop import _request_input_bound
from kernel.provider import (
    ChatRequest,
    OpenAICompatibleClient,
    ProviderFailure,
    StreamEnd,
    TextDelta,
    ToolCall,
    ToolCallReady,
    UsageReport,
)

MODEL = "nvidia/nemotron-3.5-super-vl"


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


def input_bound(messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]] = ()) -> int:
    """Independent restatement of the documented input upper bound (bytes)."""

    return len(
        json.dumps(
            {"messages": list(messages), "tools": list(tools)},
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    )


class SnapshotProvider(StubProvider):
    """``StubProvider`` that records the input bound as each request arrives.

    The loop mutates the transcript after a step, so the bound for a step can
    only be computed when that step's request is made.
    """

    def __init__(self, scripts: Sequence[Sequence[Any]]):
        super().__init__(scripts)
        self.bounds: list[int] = []

    async def stream(self, request: ChatRequest) -> Any:
        self.bounds.append(input_bound(request.messages, request.tools))
        async for event in super().stream(request):
            yield event


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
    assert finished.estimated_cost_usd == pytest.approx(4.0e-06)
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
        return {"ok": True, "results": [{"title": "GPU cloud"}]}

    loop, provider = build(
        [tool_script("web.search", {"query": "gpu"}), text_script("Found one.")],
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
    assert calls == [ToolCall(call_id="call_1", name="web.search", arguments={"query": "gpu"})]
    started = [e for e in result if e.kind == "tool_call_started"][0]
    assert started.arguments == {"query": "gpu"}
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


class _ManualClock:
    """Monotonic clock a test advances explicitly."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class _ApprovalWaitingExecutor:
    """Pretends each call spent ``wait_seconds`` blocked on an approval."""

    def __init__(self, clock: _ManualClock, wait_seconds: float) -> None:
        self._clock = clock
        self._wait_seconds = wait_seconds
        self.waited: dict[str, int] = {}

    async def execute(self, call: ToolCall) -> Mapping[str, Any]:
        self._clock.value += self._wait_seconds
        self.waited[call.call_id] = int(self._wait_seconds * 1000)
        return {"ok": True}

    def approval_wait_ms(self, call_id: str) -> int:
        return self.waited.pop(call_id, 0)


def test_approval_wait_does_not_consume_the_wall_budget() -> None:
    # §14.2: a 60 s approval wait inside a 120 s budget must not stop the turn,
    # and duration_ms must still report the real 60 s the person experienced.
    clock = _ManualClock()
    executor = _ApprovalWaitingExecutor(clock, wait_seconds=60.0)
    loop, _provider = build(
        [tool_script("shell.exec", {"cmd": "echo hi"}), text_script("all done")],
        budget=TurnBudget(max_steps=10, max_wall_seconds=120),
        executor=executor,
        time_fn=clock,
    )
    result = collect(loop, [{"role": "user", "content": "run"}])

    finished = [e for e in result if e.kind == "tool_call_finished"][0]
    assert finished.waited_ms == 60_000
    assert finished.duration_ms == 60_000
    assert result[-1].reason == "stop"
    assert result[-1].message == "all done"
    assert result[-1].duration_ms == 60_000
    assert not [e for e in result if e.kind == "error"]


def test_approval_wait_beyond_the_budget_still_completes() -> None:
    # The old behaviour stopped this turn at the next step boundary because the
    # 90 s wait was counted as 90 s of budget.
    clock = _ManualClock()
    executor = _ApprovalWaitingExecutor(clock, wait_seconds=90.0)
    loop, _provider = build(
        [tool_script("shell.exec", {}), text_script("after approval")],
        budget=TurnBudget(max_steps=10, max_wall_seconds=60),
        executor=executor,
        time_fn=clock,
    )
    result = collect(loop, [{"role": "user", "content": "run"}])

    assert result[-1].reason == "stop"
    assert result[-1].message == "after approval"
    assert result[-1].duration_ms == 90_000
    assert [e for e in result if e.kind == "tool_call_finished"][0].waited_ms == 90_000


def test_time_after_the_approval_still_counts_against_the_budget() -> None:
    # Pausing must not become an exemption: work that is slow for any other
    # reason still hits max_wall_seconds.
    clock = _ManualClock()

    class SlowTool:
        async def execute(self, call: ToolCall) -> Mapping[str, Any]:
            clock.value += 70.0
            return {"ok": True}

    loop, provider = build(
        [tool_script("stub.tool", {}), text_script("never")],
        budget=TurnBudget(max_steps=10, max_wall_seconds=50),
        executor=SlowTool(),
        time_fn=clock,
    )
    result = collect(loop, [{"role": "user", "content": "slow"}])

    errors = [e for e in result if e.kind == "error"]
    assert errors[0].details == {"limit": "max_wall_seconds"}
    assert result[-1].reason == "budget"
    assert len(provider.requests) == 1
    finished = [e for e in result if e.kind == "tool_call_finished"][0]
    assert finished.waited_ms == 0


def test_registry_executor_reports_and_consumes_approval_wait() -> None:
    clock = _ManualClock()

    class FakeRegistry:
        async def invoke(
            self, name: str, arguments: Any, *, approval: Any, **kwargs: Any
        ) -> dict[str, Any]:
            assert approval is not None
            return {"ok": await approval(object())}

    async def gate(_request: Any) -> bool:
        clock.value += 45.0
        return True

    executor = RegistryToolExecutor(
        FakeRegistry(),  # type: ignore[arg-type]
        approval=gate,
        time_fn=clock,
    )
    result = asyncio.run(executor.execute(ToolCall("call_7", "shell.exec", {})))
    assert result == {"ok": True}
    assert executor.approval_wait_ms("call_7") == 45_000
    # The wait is consumed so a later call with the same id cannot inherit it.
    assert executor.approval_wait_ms("call_7") == 0


def test_registry_executor_without_approval_reports_no_wait() -> None:
    class FakeRegistry:
        def __init__(self) -> None:
            self.approval: Any = "unset"

        async def invoke(self, name: str, arguments: Any, *, approval: Any, **kwargs: Any) -> dict[str, Any]:
            self.approval = approval
            return {"ok": True}

    registry = FakeRegistry()
    executor = RegistryToolExecutor(registry)  # type: ignore[arg-type]
    result = asyncio.run(executor.execute(ToolCall("call_8", "web.search", {})))
    assert result == {"ok": True}
    assert registry.approval is None
    assert executor.approval_wait_ms("call_8") == 0


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


def test_cancel_token_before_the_turn_ends_it_without_an_error() -> None:
    store = InMemoryUsageStore()
    token = CancelToken()
    token.cancel()
    loop, provider = build([text_script("never")], store=store)

    result = collect(loop, [{"role": "user", "content": "hi"}], cancel=token)

    assert kinds(result) == ["turn_started", "turn_finished"]
    assert result[-1].reason == "cancelled"
    assert result[-1].message is None
    assert provider.requests == []
    assert store.records[0].outcome == "cancelled"


def test_cancel_between_provider_chunks_keeps_the_partial_text() -> None:
    token = CancelToken()
    store = InMemoryUsageStore()

    class CancellingProvider:
        async def stream(self, request: ChatRequest) -> Any:
            yield TextDelta("partial")
            token.cancel()
            yield TextDelta("after cancel")
            yield UsageReport(1, 1, 2)
            yield StreamEnd("stop")

    loop = AgentLoop(
        provider=CancellingProvider(),
        config=KernelConfig(api_key="test-key"),
        usage_store=store,
        model=MODEL,
    )
    result = collect(loop, [{"role": "user", "content": "hi"}], cancel=token)

    assert kinds(result) == ["turn_started", "token_delta", "turn_finished"]
    assert result[1].text == "partial"
    assert result[-1].reason == "cancelled"
    assert store.records[0].outcome == "cancelled"


def test_cancel_during_a_tool_call_stops_before_the_next_model_step() -> None:
    token = CancelToken()

    async def executor(call: ToolCall) -> Mapping[str, Any]:
        token.cancel()
        return {"ok": True, "output": {}}

    loop, provider = build(
        [tool_script("stub.tool", {"n": 1}), text_script("never")],
        executor=CallableToolExecutor(executor),
    )
    result = collect(loop, [{"role": "user", "content": "go"}], cancel=token)

    assert kinds(result) == [
        "turn_started",
        "usage_updated",
        "tool_call_started",
        "tool_call_finished",
        "turn_finished",
    ]
    assert result[-1].reason == "cancelled"
    assert result[-1].tool_calls == 1
    assert len(provider.requests) == 1
    assert not [event for event in result if event.kind == "error"]


async def _ok() -> Mapping[str, Any]:
    return {"ok": True}


# --- review fixes: pricing source and cancellation accounting -----------------


def test_usage_record_uses_config_pricing_for_model_missing_from_defaults() -> None:
    from kernel.config import DEFAULT_PRICING, ModelPricing

    custom = "acme/private-model-1"
    assert custom not in DEFAULT_PRICING
    config = KernelConfig(
        api_key="test-key",
        pricing={custom: ModelPricing(input_per_million=2.0, output_per_million=4.0)},
    )
    store = InMemoryUsageStore()
    loop, _ = build([text_script("hi", usage=(1000, 500))], store=store, config=config, model=custom)

    result = collect(loop, [{"role": "user", "content": "hi"}], turn_id="t1")

    expected = (1000 / 1_000_000) * 2.0 + (500 / 1_000_000) * 4.0
    record = store.records[0]
    assert record.cost_known is True
    assert record.estimated_cost_usd == pytest.approx(expected)
    assert result[-1].estimated_cost_usd == pytest.approx(expected)
    updated = [event for event in result if event.kind == "usage_updated"][0]
    assert updated.estimated_cost_usd == pytest.approx(expected)


def test_usage_record_uses_config_override_for_default_model() -> None:
    from kernel.config import DEFAULT_PRICING, ModelPricing

    assert MODEL in DEFAULT_PRICING
    config = KernelConfig(
        api_key="test-key",
        pricing={MODEL: ModelPricing(input_per_million=100.0, output_per_million=200.0)},
    )
    store = InMemoryUsageStore()
    loop, _ = build([text_script("hi", usage=(1000, 500))], store=store, config=config)

    result = collect(loop, [{"role": "user", "content": "hi"}])

    expected = (1000 / 1_000_000) * 100.0 + (500 / 1_000_000) * 200.0
    default_cost = DEFAULT_PRICING[MODEL].estimate(1000, 500)
    assert expected != pytest.approx(default_cost)
    assert store.records[0].estimated_cost_usd == pytest.approx(expected)
    assert result[-1].estimated_cost_usd == pytest.approx(expected)
    updated = [event for event in result if event.kind == "usage_updated"][0]
    assert updated.estimated_cost_usd == pytest.approx(expected)


def test_aclose_mid_turn_writes_one_cancelled_usage_record() -> None:
    store = InMemoryUsageStore()
    loop, _ = build([text_script("Hello there")], store=store)

    async def run() -> None:
        gen = loop.run_turn([{"role": "user", "content": "hi"}], turn_id="t1")
        first = await gen.__anext__()
        second = await gen.__anext__()
        assert first.kind == "turn_started"
        assert second.kind == "token_delta"
        await gen.aclose()

    asyncio.run(run())

    assert len(store.records) == 1
    record = store.records[0]
    assert record.turn_id == "t1"
    assert record.outcome == "cancelled"
    assert record.steps == 1
    assert record.tool_calls == 0
    # The closed turn consumed only the TextDelta; the UsageReport had not
    # been read yet, so nothing is claimed as provider-reported. The step
    # reached the provider, so it is recorded at its upper bound, not as 0.
    bound_in = input_bound([{"role": "user", "content": "hi"}])
    assert record.input_tokens == bound_in
    assert record.output_tokens == 1024
    assert record.total_tokens == bound_in + 1024
    assert record.usage_reported is False
    assert record.usage_estimated is True
    assert record.estimated_cost_usd == pytest.approx(KernelConfig(api_key="k").estimate_cost(MODEL, bound_in, 1024))
    assert record.estimated_cost_usd is not None and record.estimated_cost_usd > 0


def test_task_cancellation_while_provider_awaits_writes_one_usage_record() -> None:
    store = InMemoryUsageStore()

    class HangingProvider:
        async def stream(self, request: ChatRequest) -> Any:
            yield UsageReport(7, 3, 10)
            await asyncio.Event().wait()
            yield StreamEnd("stop")  # pragma: no cover - never reached

    loop = AgentLoop(
        provider=HangingProvider(),
        config=KernelConfig(api_key="test-key"),
        usage_store=store,
        model=MODEL,
    )

    async def run() -> None:
        seen: list[str] = []

        async def consume() -> None:
            async for event in loop.run_turn([{"role": "user", "content": "hi"}], turn_id="t1"):
                seen.append(event.kind)

        async def wait_for_usage() -> None:
            while "usage_updated" not in seen:
                await asyncio.sleep(0)

        task = asyncio.create_task(consume())
        await asyncio.wait_for(wait_for_usage(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

    assert len(store.records) == 1
    record = store.records[0]
    assert record.outcome == "cancelled"
    assert record.steps == 1
    assert record.input_tokens == 7
    assert record.output_tokens == 3
    assert record.usage_reported is True


def test_completed_turn_writes_exactly_one_usage_record() -> None:
    store = InMemoryUsageStore()
    loop, _ = build([text_script("done")], store=store)

    result = collect(loop, [{"role": "user", "content": "hi"}], turn_id="t1")

    assert kinds(result)[-1] == "turn_finished"
    assert len(store.records) == 1
    assert store.records[0].outcome == "stop"


def test_cancelled_record_store_failure_does_not_raise() -> None:
    class BrokenStore:
        def record(self, record: Any) -> None:
            raise RuntimeError("disk full")

    loop, _ = build([text_script("Hello there")], store=BrokenStore())

    async def run() -> None:
        gen = loop.run_turn([{"role": "user", "content": "hi"}])
        await gen.__anext__()
        await gen.__anext__()
        await gen.aclose()

    asyncio.run(run())


def test_provider_exception_mid_stream_records_error_outcome_and_propagates() -> None:
    store = InMemoryUsageStore()

    class ExplodingProvider:
        async def stream(self, request: ChatRequest) -> Any:
            yield TextDelta("partial")
            yield UsageReport(7, 3, 10)
            raise RuntimeError("socket reset")

    loop = AgentLoop(
        provider=ExplodingProvider(),
        config=KernelConfig(api_key="test-key"),
        usage_store=store,
        model=MODEL,
    )
    seen: list[str] = []

    async def run() -> None:
        async for event in loop.run_turn([{"role": "user", "content": "hi"}], turn_id="t1"):
            seen.append(event.kind)

    with pytest.raises(RuntimeError, match="socket reset"):
        asyncio.run(run())

    assert seen == ["turn_started", "token_delta", "usage_updated"]
    assert len(store.records) == 1
    record = store.records[0]
    assert record.turn_id == "t1"
    assert record.outcome == "error"
    assert record.steps == 1
    assert record.input_tokens == 7
    assert record.output_tokens == 3
    assert record.usage_reported is True


def test_aclose_mid_turn_closes_provider_stream_immediately() -> None:
    state = {"closed": False}

    class TrackingProvider:
        async def stream(self, request: ChatRequest) -> Any:
            try:
                yield TextDelta("Hello")
                yield TextDelta(" there")
                yield StreamEnd("stop")
            finally:
                state["closed"] = True

    loop = AgentLoop(
        provider=TrackingProvider(),
        config=KernelConfig(api_key="test-key"),
        usage_store=InMemoryUsageStore(),
        model=MODEL,
    )
    closed_after_aclose: list[bool] = []

    async def run() -> None:
        gen = loop.run_turn([{"role": "user", "content": "hi"}], turn_id="t1")
        assert (await gen.__anext__()).kind == "turn_started"
        assert (await gen.__anext__()).kind == "token_delta"
        assert state["closed"] is False
        await gen.aclose()
        # Observed before the event loop gets a chance to run GC finalizers.
        closed_after_aclose.append(state["closed"])

    asyncio.run(run())

    assert closed_after_aclose == [True]


def test_plain_async_iterator_provider_stream_still_works() -> None:
    class PlainStream:
        """Async iterator with no ``aclose`` (not an async generator)."""

        def __init__(self, items: Sequence[Any]) -> None:
            self._items = iter(items)

        def __aiter__(self) -> PlainStream:
            return self

        async def __anext__(self) -> Any:
            try:
                return next(self._items)
            except StopIteration:
                raise StopAsyncIteration from None

    class PlainProvider:
        def stream(self, request: ChatRequest) -> Any:
            return PlainStream(text_script("plain"))

    store = InMemoryUsageStore()
    loop = AgentLoop(
        provider=PlainProvider(),
        config=KernelConfig(api_key="test-key"),
        usage_store=store,
        model=MODEL,
    )

    result = collect(loop, [{"role": "user", "content": "hi"}], turn_id="t1")

    assert result[-1].reason == "stop"
    assert result[-1].message == "plain"
    assert [record.outcome for record in store.records] == ["stop"]


# --- unreported usage: provider-billed steps are never recorded as $0 ---------

NO_USAGE_TEXT = [TextDelta("Hello"), StreamEnd("stop")]


def test_unreported_usage_is_recorded_as_the_upper_bound() -> None:
    store = InMemoryUsageStore()
    config = KernelConfig(api_key="test-key")
    messages = [{"role": "user", "content": "h\u00e9llo \u4f60\u597d"}]
    loop, provider = build([NO_USAGE_TEXT], store=store, config=config)

    result = collect(loop, messages, turn_id="t1")

    bound_in = input_bound(messages)
    assert bound_in > len(json.dumps(messages))  # bytes, not characters, not ASCII-escaped
    record = store.records[0]
    assert provider.requests[0].max_tokens == 1024
    assert record.input_tokens == bound_in
    assert record.output_tokens == 1024
    assert record.total_tokens == bound_in + 1024
    assert record.estimated_cost_usd == config.estimate_cost(MODEL, bound_in, 1024)
    assert record.estimated_cost_usd is not None and record.estimated_cost_usd > 0
    assert record.cost_known is True
    assert record.usage_reported is False
    assert record.usage_estimated is True
    assert record.to_dict()["usage_estimated"] is True
    finished = result[-1]
    assert finished.estimated_cost_usd == record.estimated_cost_usd
    # TurnFinished token fields stay the provider-reported counts.
    assert finished.input_tokens == 0
    assert finished.output_tokens == 0
    assert finished.reason == "stop"


def test_input_bound_covers_the_tools_sent_with_the_request() -> None:
    store = InMemoryUsageStore()
    tools = [{"type": "function", "function": {"name": "web.search", "parameters": {"type": "object"}}}]
    messages = [{"role": "user", "content": "hi"}]
    loop, _ = build([NO_USAGE_TEXT], store=store)

    collect(loop, messages, tools=tools)

    assert store.records[0].input_tokens == input_bound(messages, tools)
    assert input_bound(messages, tools) > input_bound(messages)


def test_unmetered_step_after_a_metered_step_adds_only_its_own_bound() -> None:
    store = InMemoryUsageStore()
    provider = SnapshotProvider([tool_script("stub.tool", {"n": 1}, usage=(10, 5)), NO_USAGE_TEXT])
    loop = AgentLoop(
        provider=provider,
        config=KernelConfig(api_key="test-key"),
        tool_executor=CallableToolExecutor(lambda call: _ok()),
        usage_store=store,
        model=MODEL,
    )

    result = collect(loop, [{"role": "user", "content": "go"}])

    second_bound = provider.bounds[1]
    assert provider.requests[1].max_tokens == 1024
    record = store.records[0]
    assert record.steps == 2
    assert record.input_tokens == 10 + second_bound
    assert record.output_tokens == 5 + 1024
    assert record.total_tokens == 10 + second_bound + 5 + 1024
    assert record.usage_reported is True
    assert record.usage_estimated is True
    # The reported counts are preserved exactly on the finished event.
    assert result[-1].input_tokens == 10
    assert result[-1].output_tokens == 5
    assert result[-1].estimated_cost_usd == record.estimated_cost_usd


def test_http_status_failure_as_the_only_event_adds_no_bound() -> None:
    # An HTTP status failure means the provider answered the request with an
    # error status, so no request was accepted for billing.
    store = InMemoryUsageStore()
    failing = [ProviderFailure("provider_http_error", "HTTP 503", retryable=True, status=503)]
    loop, _ = build([failing], store=store)

    result = collect(loop, [{"role": "user", "content": "hi"}])

    assert result[-1].reason == "error"
    record = store.records[0]
    assert record.input_tokens == 0
    assert record.output_tokens == 0
    assert record.estimated_cost_usd == 0.0
    assert record.cost_known is True
    assert record.usage_reported is False
    assert record.usage_estimated is False


@pytest.mark.parametrize("code", ["missing_api_key", "missing_base_url", "tenant_mismatch"])
def test_missing_config_failure_as_the_only_event_adds_no_bound(code: str) -> None:
    # The client refuses before any request is made (missing configuration,
    # or kernel.providers.base.tenant_failure), so nothing was billable.
    store = InMemoryUsageStore()
    failing = [ProviderFailure(code, "not configured", retryable=False)]
    loop, _ = build([failing], store=store)

    result = collect(loop, [{"role": "user", "content": "hi"}])

    assert result[-1].reason == "error"
    record = store.records[0]
    assert record.input_tokens == 0
    assert record.output_tokens == 0
    assert record.estimated_cost_usd == 0.0
    assert record.usage_estimated is False


@pytest.mark.parametrize(
    "code", ["provider_timeout", "provider_unreachable", "provider_protocol_error"]
)
def test_sole_failure_without_a_status_is_bounded(code: str) -> None:
    # No HTTP status: the failure can follow an HTTP 200 or a request the
    # provider already accepted, so the step may have been billed.
    store = InMemoryUsageStore()
    messages = [{"role": "user", "content": "hi"}]
    failing = [ProviderFailure(code, "failed", retryable=True)]
    loop, _ = build([failing], store=store)

    result = collect(loop, messages)

    assert result[-1].reason == "error"
    record = store.records[0]
    assert record.input_tokens == input_bound(messages)
    assert record.output_tokens == 1024
    assert record.usage_estimated is True
    assert record.estimated_cost_usd is not None and record.estimated_cost_usd > 0
    # The finished event keeps reported counts only.
    assert result[-1].input_tokens == 0
    assert result[-1].output_tokens == 0


def test_http_status_failure_after_a_metered_step_adds_no_bound() -> None:
    store = InMemoryUsageStore()
    failing = [ProviderFailure("provider_http_error", "HTTP 503", retryable=True, status=503)]
    loop, _ = build(
        [tool_script("stub.tool", {"n": 1}, usage=(10, 5)), failing],
        store=store,
        executor=CallableToolExecutor(lambda call: _ok()),
    )

    result = collect(loop, [{"role": "user", "content": "go"}])

    assert result[-1].reason == "error"
    record = store.records[0]
    assert record.steps == 2
    assert record.input_tokens == 10
    assert record.output_tokens == 5
    assert record.total_tokens == 15
    assert record.usage_reported is True
    assert record.usage_estimated is False


def _sse_body(*chunks: Any, done: bool = True) -> str:
    body = "\n\n".join(f"data: {json.dumps(chunk)}" for chunk in chunks)
    if done:
        body += "\n\ndata: [DONE]"
    return body + "\n\n"


class _DiesMidBody(httpx.AsyncByteStream):
    """A 200 response whose body raises a transport error after one chunk."""

    async def __aiter__(self) -> Any:
        yield b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        raise httpx.ReadError("connection reset")


ROLE_ONLY_CHUNK = {"choices": [{"delta": {"role": "assistant"}}]}


@pytest.mark.parametrize(
    ("make_response", "code"),
    [
        pytest.param(
            lambda: httpx.Response(200, text=_sse_body(ROLE_ONLY_CHUNK, done=False)),
            "provider_protocol_error",
            id="truncated-no-done-no-finish-reason",
        ),
        pytest.param(
            lambda: httpx.Response(200, text="data: {not json\n\n"),
            "provider_protocol_error",
            id="malformed-first-chunk",
        ),
        pytest.param(
            lambda: httpx.Response(200, stream=_DiesMidBody()),
            "provider_unreachable",
            id="transport-error-mid-body",
        ),
    ],
)
def test_failure_after_http_200_through_the_real_client_is_bounded(
    make_response: Any, code: str
) -> None:
    # The real client yields a lone first-event ProviderFailure (status None)
    # after an HTTP 200, when the provider may already have billed the step.
    def handler(request: httpx.Request) -> httpx.Response:
        return make_response()

    config = KernelConfig(api_key="test-key", base_url="https://provider.example.test/v1")
    client = OpenAICompatibleClient(
        config, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    store = InMemoryUsageStore()
    messages = [{"role": "user", "content": "hi"}]
    loop = AgentLoop(provider=client, config=config, usage_store=store, model=MODEL)

    async def run() -> list[Any]:
        async with client:
            return [event async for event in loop.run_turn(messages, turn_id="t1")]

    result = asyncio.run(run())

    errors = [event for event in result if event.kind == "error"]
    assert [event.code for event in errors] == [code]
    assert result[-1].reason == "error"
    record = store.records[0]
    assert record.usage_estimated is True
    assert record.usage_reported is False
    assert record.input_tokens == input_bound(messages)
    assert record.output_tokens == 1024
    assert record.estimated_cost_usd is not None and record.estimated_cost_usd > 0
    assert record.estimated_cost_usd == config.estimate_cost(MODEL, record.input_tokens, 1024)


def test_request_input_bound_falls_back_to_repr_for_unserializable_keys() -> None:
    # json.dumps cannot sort or encode a tuple key; accounting must not fail.
    messages = [{"role": "user", "content": "hi", ("a", 1): "tuple key"}]

    with pytest.raises(TypeError):
        json.dumps({"messages": messages, "tools": []})

    bound = _request_input_bound(messages, [])

    assert isinstance(bound, int)
    assert bound > 0


def test_provider_failure_after_text_is_estimated() -> None:
    store = InMemoryUsageStore()
    failing = [TextDelta("par"), ProviderFailure("provider_timeout", "timed out", retryable=True)]
    messages = [{"role": "user", "content": "hi"}]
    loop, _ = build([failing], store=store)

    result = collect(loop, messages)

    assert result[-1].reason == "error"
    record = store.records[0]
    assert record.input_tokens == input_bound(messages)
    assert record.output_tokens == 1024
    assert record.usage_estimated is True
    assert record.estimated_cost_usd is not None and record.estimated_cost_usd > 0


def test_provider_failure_after_a_tool_call_is_estimated() -> None:
    store = InMemoryUsageStore()
    failing = [
        ToolCallReady(index=0, call_id="c1", name="stub.tool", arguments={}),
        ProviderFailure("provider_timeout", "timed out", retryable=True),
    ]
    loop, _ = build([failing], store=store)

    collect(loop, [{"role": "user", "content": "hi"}])

    assert store.records[0].usage_estimated is True
    assert store.records[0].output_tokens == 1024


def test_aclose_before_usage_records_the_bound_on_the_cancelled_turn() -> None:
    store = InMemoryUsageStore()
    messages = [{"role": "user", "content": "hi"}]
    loop, _ = build([[TextDelta("a"), TextDelta("b"), UsageReport(3, 2, 5), StreamEnd("stop")]], store=store)

    async def run() -> None:
        gen = loop.run_turn(messages, turn_id="t1")
        assert (await gen.__anext__()).kind == "turn_started"
        assert (await gen.__anext__()).kind == "token_delta"
        await gen.aclose()

    asyncio.run(run())

    assert len(store.records) == 1
    record = store.records[0]
    assert record.outcome == "cancelled"
    assert record.input_tokens == input_bound(messages)
    assert record.output_tokens == 1024
    assert record.usage_reported is False
    assert record.usage_estimated is True


def test_aclose_after_usage_does_not_add_a_bound() -> None:
    store = InMemoryUsageStore()
    loop, _ = build([text_script("Hello there", usage=(7, 3))], store=store)

    async def run() -> None:
        gen = loop.run_turn([{"role": "user", "content": "hi"}])
        kinds_seen = [(await gen.__anext__()).kind for _ in range(3)]
        assert kinds_seen == ["turn_started", "token_delta", "usage_updated"]
        await gen.aclose()

    asyncio.run(run())

    record = store.records[0]
    assert (record.input_tokens, record.output_tokens) == (7, 3)
    assert record.usage_reported is True
    assert record.usage_estimated is False


def test_cancel_token_mid_stream_before_usage_includes_the_bound() -> None:
    token = CancelToken()
    store = InMemoryUsageStore()
    messages = [{"role": "user", "content": "hi"}]

    class CancellingProvider:
        async def stream(self, request: ChatRequest) -> Any:
            yield TextDelta("partial")
            token.cancel()
            yield TextDelta("after cancel")
            yield UsageReport(1, 1, 2)
            yield StreamEnd("stop")

    loop = AgentLoop(
        provider=CancellingProvider(),
        config=KernelConfig(api_key="test-key"),
        usage_store=store,
        model=MODEL,
    )
    result = collect(loop, messages, cancel=token)

    assert result[-1].reason == "cancelled"
    record = store.records[0]
    assert record.outcome == "cancelled"
    assert record.input_tokens == input_bound(messages)
    assert record.output_tokens == 1024
    assert record.usage_reported is False
    assert record.usage_estimated is True
    assert result[-1].estimated_cost_usd == record.estimated_cost_usd


def test_provider_exception_before_usage_records_the_bound_with_error_outcome() -> None:
    store = InMemoryUsageStore()
    messages = [{"role": "user", "content": "hi"}]

    class ExplodingProvider:
        async def stream(self, request: ChatRequest) -> Any:
            yield TextDelta("partial")
            raise RuntimeError("socket reset")

    loop = AgentLoop(
        provider=ExplodingProvider(),
        config=KernelConfig(api_key="test-key"),
        usage_store=store,
        model=MODEL,
    )

    with pytest.raises(RuntimeError, match="socket reset"):
        collect(loop, messages)

    assert len(store.records) == 1
    record = store.records[0]
    assert record.outcome == "error"
    assert record.input_tokens == input_bound(messages)
    assert record.output_tokens == 1024
    assert record.usage_estimated is True


def test_task_cancellation_before_usage_records_the_bound() -> None:
    store = InMemoryUsageStore()
    messages = [{"role": "user", "content": "hi"}]

    class HangingProvider:
        async def stream(self, request: ChatRequest) -> Any:
            yield TextDelta("partial")
            await asyncio.Event().wait()
            yield StreamEnd("stop")  # pragma: no cover - never reached

    loop = AgentLoop(
        provider=HangingProvider(),
        config=KernelConfig(api_key="test-key"),
        usage_store=store,
        model=MODEL,
    )

    async def run() -> None:
        seen: list[str] = []

        async def consume() -> None:
            async for event in loop.run_turn(messages, turn_id="t1"):
                seen.append(event.kind)

        async def wait_for_text() -> None:
            while "token_delta" not in seen:
                await asyncio.sleep(0)

        task = asyncio.create_task(consume())
        await asyncio.wait_for(wait_for_text(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

    assert len(store.records) == 1
    record = store.records[0]
    assert record.outcome == "cancelled"
    assert record.input_tokens == input_bound(messages)
    assert record.output_tokens == 1024
    assert record.usage_estimated is True


def test_unpriced_model_without_usage_stays_unknown_not_estimated_cost() -> None:
    store = InMemoryUsageStore()
    loop, _ = build([NO_USAGE_TEXT], store=store, model="unknown/model")

    result = collect(loop, [{"role": "user", "content": "hi"}])

    record = store.records[0]
    assert record.estimated_cost_usd is None
    assert record.cost_known is False
    assert result[-1].estimated_cost_usd is None
    # The token bound is still recorded; only the price is unknown.
    assert record.usage_estimated is True
    assert record.output_tokens == 1024


def test_metered_turn_is_unchanged_and_not_marked_estimated() -> None:
    store = InMemoryUsageStore()
    loop, _ = build([text_script("Hello there")], store=store)

    result = collect(loop, [{"role": "user", "content": "hi"}])

    record = store.records[0]
    assert (record.input_tokens, record.output_tokens, record.total_tokens) == (10, 5, 15)
    assert record.estimated_cost_usd == pytest.approx(4.0e-06)
    assert record.usage_reported is True
    assert record.usage_estimated is False
    assert result[-1].estimated_cost_usd == record.estimated_cost_usd


def test_unmetered_step_does_not_consume_the_output_token_budget() -> None:
    # Step 1 reports nothing; step 2 must still be offered the full remaining
    # budget, because TurnBudget is measured in reported usage only.
    unmetered_tool = [
        ToolCallReady(index=0, call_id="call_1", name="stub.tool", arguments={}),
        StreamEnd("tool_calls"),
    ]
    store = InMemoryUsageStore()
    loop, provider = build(
        [unmetered_tool, text_script("done", usage=(10, 20))],
        budget=TurnBudget(max_steps=5, max_output_tokens=300, max_tokens_per_step=1024, max_tool_calls=5),
        executor=CallableToolExecutor(lambda call: _ok()),
        store=store,
    )

    result = collect(loop, [{"role": "user", "content": "go"}])

    assert [request.max_tokens for request in provider.requests] == [300, 300]
    assert result[-1].reason == "stop"
    assert result[-1].output_tokens == 20
    record = store.records[0]
    assert record.output_tokens == 300 + 20
    assert record.usage_reported is True
    assert record.usage_estimated is True


def test_usage_log_line_carries_usage_estimated() -> None:
    import io

    from chat.logging import configure_logging
    from chat.metrics import LoggedUsageStore

    stream = io.StringIO()
    configure_logging(level="debug", stream=stream)
    store = LoggedUsageStore(InMemoryUsageStore())
    loop, _ = build([NO_USAGE_TEXT], store=store)

    collect(loop, [{"role": "user", "content": "hi"}])

    line = json.loads(stream.getvalue().splitlines()[-1])
    assert line["event"] == "usage"
    assert line["usage_reported"] is False
    assert line["usage_estimated"] is True

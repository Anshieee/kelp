"""P5: entitlement decisions, gate order, and terminal billing metadata.

The cases bind the control-layer contract to the runtime:

* the owner default is an explicit unlimited plan, not a missing decision;
* a configured ceiling denies with the frozen code before any provider call and
  leaves the thread byte-for-byte unchanged;
* the entitlement gate runs before the spend gate;
* the plan snapshot is captured at run creation and reaches the terminal billing
  hook, never the prompt;
* the static store parses only configuration and fails closed on malformed input.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from chat import ChatAPI, ChatService, ChatStore, Request
from chat.errors import ChatError
from chat.runs import RunStatus
from payments.billing import CountingBillingSink
from payments.entitlements import (
    DEFAULT_OWNER_PLAN_ID,
    ENTITLEMENT_PLAN_ENV,
    ENTITLEMENT_PLANS_ENV,
    ENTITLEMENT_TENANT_PLANS_ENV,
    EntitlementDecision,
    PlanLimits,
    StaticEntitlementStore,
)

from .chatkit import MODEL, StubProvider, text_script

PROMPT = "super-secret-entitlement-prompt"
AUTH = {"authorization": "Bearer tok"}
CSRF = {"x-csrf-token": "csrf"}


def _api_call(api: ChatAPI, method: str, path: str, body: dict[str, Any]) -> Any:
    headers = {**AUTH, "content-type": "application/json"}
    if method.upper() in {"POST", "PATCH", "DELETE"}:
        headers.update(CSRF)
    return api.handle(
        Request(method, path, {}, headers, json.dumps(body).encode())
    )


def _plan(**overrides: Any) -> PlanLimits:
    values = {
        "plan_id": "owner-test",
        "window": "day",
        "max_turns": None,
        "max_runs": None,
        "max_cost_usd": None,
        "allowed_task_types": None,
    }
    values.update(overrides)
    return PlanLimits(**values)


class _SpendThatMustNotRun:
    """A spend guard that records whether the entitlement gate let it run."""

    def __init__(self) -> None:
        self.evaluated = False

    def evaluate(self, now: Any = None) -> Any:
        del now
        self.evaluated = True
        raise AssertionError("the spend gate must not run after a denial")


# -- the default plan ------------------------------------------------------


def test_owner_default_plan_is_explicit_and_allows(tmp_path: Path) -> None:
    store = ChatStore(tmp_path / "chat.db")
    provider = StubProvider([text_script("hello")])
    service = ChatService(store=store, provider=provider, model=MODEL)
    try:
        decision = service.decide_entitlement("owner", task_type="chat")
        assert decision.allowed is True
        assert decision.decision == "allow"
        assert decision.code == "ok"
        assert decision.plan_id == DEFAULT_OWNER_PLAN_ID
        assert decision.limits == {
            "plan_id": DEFAULT_OWNER_PLAN_ID,
            "window": None,
            "max_turns": None,
            "max_runs": None,
            "max_cost_usd": None,
            "allowed_task_types": None,
        }
        # The frozen decision prefix keeps its field order.
        assert list(decision.to_dict()) == [
            "decision",
            "code",
            "limit",
            "window",
            "plan_id",
            "limits",
        ]

        thread = service.create_thread()

        async def scenario() -> dict[str, Any]:
            payload, _created = await service.send(
                thread["id"], text="hi", client_turn_id="ct-default-plan"
            )
            await service.wait_for_turn(payload["turn_id"], timeout=5.0)
            return payload

        payload = asyncio.run(scenario())
        assert len(provider.requests) == 1
        snapshot = service._runs.plan_snapshot(payload["run_id"])  # noqa: SLF001
        assert snapshot is not None
        assert snapshot["plan_id"] == DEFAULT_OWNER_PLAN_ID
        # The snapshot is read-only: a caller's copy cannot change the record.
        snapshot["plan_id"] = "tampered"
        assert service._runs.plan_snapshot(payload["run_id"])["plan_id"] == (  # noqa: SLF001
            DEFAULT_OWNER_PLAN_ID
        )
    finally:
        store.close()


# -- denials ---------------------------------------------------------------


def test_over_limit_denies_with_frozen_code_before_provider(tmp_path: Path) -> None:
    entitled = StaticEntitlementStore(
        {"owner-test": _plan(max_turns=1)},
        default_plan_id="owner-test",
        counter=lambda tenant, task, window: 1,
    )
    store = ChatStore(tmp_path / "chat.db")
    provider = StubProvider([text_script("never")])
    service = ChatService(
        store=store, provider=provider, model=MODEL, entitlements=entitled
    )
    try:
        thread = service.create_thread()
        before = service.get_thread(thread["id"])
        with pytest.raises(ChatError) as caught:
            asyncio.run(
                service.send(thread["id"], text="hi", client_turn_id="ct-over-limit")
            )
        error = caught.value
        assert error.code == "entitlement_exhausted"
        assert error.status == 429
        assert error.error["details"]["plan_id"] == "owner-test"
        assert error.error["details"]["limit"] == 1
        assert error.error["details"]["window"] == "day"

        assert provider.requests == []
        assert store.latest_turn(thread["id"]) is None
        assert service.get_thread(thread["id"])["messages"] == before["messages"] == []
        assert store.list_runs(tenant_id="owner") == []
    finally:
        store.close()


def test_denial_leaves_no_partial_message_and_makes_no_provider_call(
    tmp_path: Path,
) -> None:
    entitled = StaticEntitlementStore(
        {"owner-test": _plan(allowed_task_types=("chat",))},
        default_plan_id="owner-test",
    )
    store = ChatStore(tmp_path / "chat.db")
    provider = StubProvider([text_script("never")])
    service = ChatService(
        store=store, provider=provider, model=MODEL, entitlements=entitled
    )
    try:
        thread = service.create_thread()
        before = service.get_thread(thread["id"])
        with pytest.raises(ChatError) as caught:
            asyncio.run(
                service.send(
                    thread["id"],
                    text="hi",
                    client_turn_id="ct-denied",
                    task_type="plugin",
                )
            )
        assert caught.value.code == "entitlement_denied"
        assert caught.value.status == 403
        assert provider.requests == []
        assert store.latest_turn(thread["id"]) is None
        assert service.get_thread(thread["id"])["messages"] == before["messages"] == []
    finally:
        store.close()


def test_entitlement_gate_runs_before_spend(tmp_path: Path) -> None:
    entitled = StaticEntitlementStore(
        {"owner-test": _plan(allowed_task_types=("chat",))},
        default_plan_id="owner-test",
    )
    spend = _SpendThatMustNotRun()
    store = ChatStore(tmp_path / "chat.db")
    provider = StubProvider([text_script("never")])
    service = ChatService(
        store=store,
        provider=provider,
        model=MODEL,
        entitlements=entitled,
        spend_guard=spend,  # type: ignore[arg-type]
    )
    try:
        thread = service.create_thread()
        with pytest.raises(ChatError) as caught:
            asyncio.run(
                service.send(
                    thread["id"],
                    text="hi",
                    client_turn_id="ct-order",
                    task_type="scheduled",
                )
            )
        assert caught.value.code == "entitlement_denied"
        assert spend.evaluated is False
        assert provider.requests == []
    finally:
        store.close()


def test_retry_passes_the_entitlement_gate_before_new_work(tmp_path: Path) -> None:
    class FailingProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, request: Any) -> Any:
            del request
            self.calls += 1
            raise RuntimeError("provider down")
            yield  # pragma: no cover - makes this an async generator

    calls = {"count": 0}

    def counter(tenant: str, task: str, window: str) -> int:
        del tenant, task, window
        return calls["count"]

    entitled = StaticEntitlementStore(
        {"owner-test": _plan(max_turns=1)},
        default_plan_id="owner-test",
        counter=counter,
    )
    store = ChatStore(tmp_path / "chat.db")
    provider = FailingProvider()
    service = ChatService(
        store=store, provider=provider, model=MODEL, entitlements=entitled
    )
    try:
        thread = service.create_thread()

        async def first() -> dict[str, Any]:
            payload, _created = await service.send(
                thread["id"], text="hi", client_turn_id="ct-retry-gate"
            )
            await service.wait_for_turn(payload["turn_id"], timeout=5.0)
            return payload

        payload = asyncio.run(first())
        assert provider.calls == 1
        assert store.get_run_by_turn(payload["turn_id"])["status"] == "failed"

        # The window is now at its configured ceiling, so the retry is refused
        # before a new turn row, a new run, or another provider call exists.
        calls["count"] = 1
        with pytest.raises(ChatError) as caught:
            asyncio.run(service.retry(payload["turn_id"]))
        assert caught.value.code == "entitlement_exhausted"
        assert provider.calls == 1
        assert len(store.list_runs(tenant_id="owner")) == 1
        assert store.latest_turn(thread["id"])["id"] == payload["turn_id"]
    finally:
        store.close()


# -- API submission gate ---------------------------------------------------


def test_api_run_post_is_denied_before_a_thread_or_run_exists(
    tmp_path: Path,
) -> None:
    entitled = StaticEntitlementStore(
        {"owner-test": _plan(allowed_task_types=("chat",))},
        default_plan_id="owner-test",
    )
    store = ChatStore(tmp_path / "chat.db")
    provider = StubProvider([text_script("never")])
    service = ChatService(
        store=store, provider=provider, model=MODEL, entitlements=entitled
    )
    api = ChatAPI(service, auth_token="tok", csrf_token="csrf")
    try:
        response = _api_call(
            api, "POST", "/api/runs", {"text": "hi", "task_type": "plugin"}
        )
        assert response.status == 403
        assert json.loads(response.body)["error"]["code"] == "entitlement_denied"
        assert provider.requests == []
        assert store.list_threads(tenant_id="owner")[0] == []
        assert store.list_runs(tenant_id="owner") == []
    finally:
        api.close()
        store.close()


# -- billing ---------------------------------------------------------------


def test_billing_sink_gets_terminal_metadata_only(tmp_path: Path) -> None:
    sink = CountingBillingSink()
    store = ChatStore(tmp_path / "chat.db")
    provider = StubProvider([text_script("hello")])
    service = ChatService(
        store=store, provider=provider, model=MODEL, billing=sink
    )
    try:
        thread = service.create_thread()

        async def scenario() -> dict[str, Any]:
            payload, _created = await service.send(
                thread["id"], text=PROMPT, client_turn_id="ct-billing"
            )
            await service.wait_for_turn(payload["turn_id"], timeout=5.0)
            return payload

        payload = asyncio.run(scenario())
        assert sink.count == 1
        events = sink.records()
        event = events[0]
        assert set(event) == {
            "run_id",
            "tenant_id",
            "task_type",
            "status",
            "model",
            "provider",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cost_usd",
            "plan",
        }
        assert event["run_id"] == payload["run_id"]
        assert event["tenant_id"] == "owner"
        assert event["model"] == MODEL
        assert event["status"] == RunStatus.COMPLETED.value
        assert (event["input_tokens"], event["output_tokens"]) == (10, 5)
        assert event["total_tokens"] == 15
        assert event["plan"]["plan_id"] == DEFAULT_OWNER_PLAN_ID
        serialized = json.dumps(event)
        assert PROMPT not in serialized
        assert "prompt" not in serialized.lower()
        assert "message" not in serialized.lower()
    finally:
        store.close()


# -- static configuration --------------------------------------------------


def test_static_store_parses_plan_and_tenant_configuration() -> None:
    store = StaticEntitlementStore.from_env(
        {
            ENTITLEMENT_PLAN_ENV: "owner-unlimited",
            ENTITLEMENT_PLANS_ENV: json.dumps(
                {
                    "owner-trial": {
                        "window": "day",
                        "max_turns": 2,
                        "max_runs": 3,
                        "max_cost_usd": 1.5,
                        "allowed_task_types": ["chat", "scheduled"],
                    }
                }
            ),
            ENTITLEMENT_TENANT_PLANS_ENV: json.dumps({"owner": "owner-trial"}),
        }
    )
    plan = store.plan_for(tenant_id="owner")
    assert plan is not None
    assert plan.plan_id == "owner-trial"
    assert plan.window == "day"
    assert plan.max_turns == 2
    assert plan.max_runs == 3
    assert plan.max_cost_usd == 1.5
    assert plan.allowed_task_types == ("chat", "scheduled")

    allowed = store.decide(tenant_id="owner", task_type="chat")
    assert allowed.allowed is True
    denied = store.decide(tenant_id="owner", task_type="plugin")
    assert denied.code == "entitlement_denied"
    assert denied.plan_id == "owner-trial"

    # An unmapped tenant falls back to the configured default plan, which is
    # the explicit owner-unlimited plan.
    default = store.plan_for(tenant_id="another")
    assert default is not None and default.plan_id == "owner-unlimited"

    exact = StaticEntitlementStore(
        {"owner-test": _plan(max_turns=1)},
        default_plan_id="owner-test",
        counter=lambda tenant, task, window: 1,
    )
    assert exact.decide(tenant_id="owner", task_type="chat").code == (
        "entitlement_exhausted"
    )

    with pytest.raises(ValueError):
        StaticEntitlementStore.from_env({ENTITLEMENT_PLANS_ENV: "not json"})


def test_entitlement_decision_rejects_unknown_values() -> None:
    with pytest.raises(ValueError):
        EntitlementDecision(decision="maybe", code="ok")
    with pytest.raises(ValueError):
        EntitlementDecision(decision="allow", code="ok", window="fortnight")
    with pytest.raises(ValueError):
        PlanLimits(plan_id="x", window="year")

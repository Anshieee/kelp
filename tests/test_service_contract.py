"""Contract tests for the frozen bot control layer (P0-1).

These tests bind the documents to the code that exists today. P0-1 changes no
runtime module: the schema freezes ``ChatRequest.tenant_id`` as the P1 target
while asserting that the current dataclass still has exactly five fields.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import re
import typing
from pathlib import Path

import chat.server
import chat.service
import kernel.provider

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs" / "SERVICE_CONTRACT.md"
ADR = ROOT / "docs" / "adr" / "0003-service-control-layer.md"
PRIVACY = ROOT / "docs" / "PRIVACY.md"

START = "<!-- contract-schema:start -->"
END = "<!-- contract-schema:end -->"

EXPECTED_PROVIDER_SPEC = [
    "name",
    "base_url",
    "api_key_env",
    "default_model",
    "pricing",
]
EXPECTED_CHAT_REQUEST = [
    "model",
    "messages",
    "tools",
    "temperature",
    "max_tokens",
    "tenant_id",
]
EXPECTED_CHAT_REQUEST_P0 = [
    "model",
    "messages",
    "tools",
    "temperature",
    "max_tokens",
]
EXPECTED_TENANT_CONTEXT = ["tenant_id", "actor_id", "role", "surface"]
EXPECTED_TASK_RUN = [
    "run_id",
    "tenant_id",
    "thread_id",
    "turn_id",
    "surface",
    "task_type",
    "status",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "model",
    "provider",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cost_usd",
    "steps",
    "tool_calls",
    "approvals",
    "error_code",
    "retry_of",
    "idempotency_key",
]
EXPECTED_STATUSES = [
    "queued",
    "running",
    "awaiting_approval",
    "stopping",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
]
EXPECTED_TERMINAL = ["completed", "failed", "cancelled", "interrupted"]
EXPECTED_ENTITLEMENT = ["decision", "code", "limit", "window", "plan_id"]
EXPECTED_GATES = ["auth", "tenant", "entitlement", "spend", "provider"]
EXPECTED_ERROR_CODES = [
    "invalid_request",
    "unauthenticated",
    "forbidden",
    "tenant_missing",
    "tenant_unknown",
    "tenant_mismatch",
    "entitlement_denied",
    "entitlement_exhausted",
    "spend_cap_exceeded",
    "provider_auth",
    "provider_model_not_found",
    "provider_timeout",
    "provider_rate_limited",
    "provider_unavailable",
    "provider_rejected",
    "provider_unreachable",
    "provider_protocol_error",
    "provider_error",
    "internal_error",
]


def load_schema() -> dict:
    text = CONTRACT.read_text(encoding="utf-8")
    assert text.count(START) == 1, "the contract must have exactly one schema start marker"
    assert text.count(END) == 1, "the contract must have exactly one schema end marker"
    body = text.split(START, 1)[1].split(END, 1)[0]
    return json.loads(body)


def record(schema: dict, *path: str) -> dict:
    node: dict = schema
    for key in path:
        node = node[key]
    return node


def test_documents_exist_and_cross_reference() -> None:
    assert CONTRACT.is_file()
    assert ADR.is_file()
    privacy = PRIVACY.read_text(encoding="utf-8")
    assert "SERVICE_CONTRACT.md" in privacy
    assert "0003-service-control-layer.md" in privacy
    contract = CONTRACT.read_text(encoding="utf-8")
    assert "0003-service-control-layer.md" in contract


def test_schema_is_frozen_and_identifies_the_contract() -> None:
    schema = load_schema()
    assert schema["contract_version"] == "1.0.0"
    assert schema["status"] == "frozen"
    assert schema["frozen_on"] == "2026-11-09"
    assert schema["tenant_model"]["mode"] == "single_owner"
    assert schema["tenant_model"]["owner_tenant_id"] == "owner"
    assert schema["tenant_model"]["principal_roles"] == ["owner"]
    assert schema["tenant_model"]["future"]["tenant_id_format"] == "slug"
    assert schema["tenant_model"]["future"]["isolation_shipped"] is False


def test_provider_contract_and_name_agnostic_seam() -> None:
    schema = load_schema()
    provider = schema["provider"]
    assert provider["stream_signature"] == (
        "stream(ChatRequest) -> AsyncIterator[ProviderEvent]"
    )
    assert provider["request_type"] == "ChatRequest"
    assert provider["event_type"] == "ProviderEvent"

    seam = provider["seam"]
    assert seam["module"] == "chat.server"
    assert seam["injectable_keyword"] == "provider"
    assert seam["module_level_only"] is True
    assert "ProviderClient" in seam["accepted_symbols"]
    assert "TokenFactoryClient" in seam["accepted_symbols"]
    assert "one module-level patchable provider symbol" in seam["rule"]
    assert "injectable provider=" in seam["rule"]

    spec = provider["provider_spec"]
    assert spec["field_order"] == EXPECTED_PROVIDER_SPEC
    assert spec["required"] == EXPECTED_PROVIDER_SPEC
    assert set(spec["properties"]) == set(EXPECTED_PROVIDER_SPEC)
    assert spec["properties"]["pricing"]["field_order"] == [
        "input_per_million_usd",
        "output_per_million_usd",
    ]
    assert "value never appears" in spec["secret_rule"]


def test_chat_request_fields_are_ordered_and_tenant_id_is_the_p1_target() -> None:
    schema = load_schema()
    chat_request = schema["ChatRequest"]
    assert chat_request["field_order"] == EXPECTED_CHAT_REQUEST
    assert chat_request["p0_present_field_order"] == EXPECTED_CHAT_REQUEST_P0
    assert chat_request["p1_additions"] == ["tenant_id"]
    assert chat_request["required"] == ["model", "messages"]
    assert chat_request["properties"]["tenant_id"]["default"] == "owner"
    assert chat_request["properties"]["tenant_id"]["since"] == "P1"
    assert chat_request["field_order"] == (
        chat_request["p0_present_field_order"] + ["tenant_id"]
    )


def test_tenant_context_fields() -> None:
    schema = load_schema()
    tenant = schema["TenantContext"]
    assert tenant["field_order"] == EXPECTED_TENANT_CONTEXT
    assert tenant["required"] == EXPECTED_TENANT_CONTEXT
    assert tenant["properties"]["tenant_id"]["default"] == "owner"
    assert tenant["properties"]["role"]["enum"] == ["owner"]
    assert set(tenant["properties"]["surface"]["enum"]) == {
        "web",
        "desktop",
        "mobile",
        "telegram",
        "cli",
        "scheduler",
    }


def test_task_run_fields_and_terminal_statuses() -> None:
    schema = load_schema()
    run = schema["TaskRun"]
    assert run["field_order"] == EXPECTED_TASK_RUN
    assert set(run["properties"]) == set(EXPECTED_TASK_RUN)
    assert run["statuses"] == EXPECTED_STATUSES
    assert run["terminal_statuses"] == EXPECTED_TERMINAL
    assert set(run["terminal_statuses"]) < set(run["statuses"])
    assert "stopping" not in run["terminal_statuses"]
    assert run["properties"]["status"]["enum"] == EXPECTED_STATUSES
    assert run["run_id"]["format"].startswith("run_")
    assert run["run_id"]["visibility"] == "external"
    assert run["turn_id"]["format"].startswith("turn_")
    assert run["turn_id"]["visibility"] == "internal"
    assert "exactly one terminal record per run_id" in run["terminal_rule"].lower()
    assert run["properties"]["run_id"]["pattern"] == "^run_"
    assert run["properties"]["turn_id"]["pattern"] == "^turn_"


def test_entitlement_decision_shape() -> None:
    schema = load_schema()
    decision = schema["EntitlementDecision"]
    assert decision["field_order"] == EXPECTED_ENTITLEMENT
    assert decision["required"] == ["decision", "code"]
    assert decision["properties"]["decision"]["enum"] == ["allow", "deny"]
    assert decision["properties"]["window"]["enum"] == [
        "minute",
        "hour",
        "day",
        "month",
        None,
    ]


def test_gate_order_is_frozen_and_fail_closed() -> None:
    schema = load_schema()
    assert schema["gate_order"] == EXPECTED_GATES
    assert "fails closed" in schema["gate_rule"]
    assert "before any provider call" in schema["gate_rule"]


def test_error_codes_are_unique_snake_case_and_match_todays_provider_codes() -> None:
    schema = load_schema()
    codes = schema["error_codes"]
    assert codes == EXPECTED_ERROR_CODES
    assert len(codes) == len(set(codes))
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", code) for code in codes)

    provider_codes = {
        kernel.provider.classify_status(status)[0]
        for status in (400, 401, 403, 404, 408, 418, 429, 500, 503)
    }
    assert provider_codes <= set(codes)
    assert "internal_error" in codes


def test_admin_read_model_excludes_prompt_text_secrets_and_cross_tenant_data() -> None:
    schema = load_schema()
    admin = schema["admin_read_model"]
    fields = admin["fields"]
    excluded = admin["excluded"]

    assert not set(fields) & set(excluded)
    assert set(fields) <= set(EXPECTED_TASK_RUN)
    required_exclusions = {
        "prompt_text",
        "message_text",
        "api_key",
        "secret_values",
        "cross_tenant_data",
    }
    assert required_exclusions <= set(excluded)
    assert "provider_response_body" in excluded
    assert any("no prompt" in rule for rule in admin["rules"])
    assert any("no API-key" in rule for rule in admin["rules"])
    assert any("tenant-scoped" in rule for rule in admin["rules"])


def test_today_chat_request_has_exactly_five_fields() -> None:
    current = [field.name for field in dataclasses.fields(kernel.provider.ChatRequest)]
    schema = load_schema()
    assert current == EXPECTED_CHAT_REQUEST_P0
    assert current == schema["ChatRequest"]["p0_present_field_order"]
    assert "tenant_id" not in current
    assert kernel.provider.ChatRequest.__dataclass_params__.frozen is True


def test_today_provider_seam_is_one_patchable_symbol_and_provider_keyword() -> None:
    schema = load_schema()
    symbols = [
        name
        for name in schema["provider"]["seam"]["accepted_symbols"]
        if hasattr(chat.server, name)
    ]
    assert len(symbols) == 1, f"expected one module-level provider symbol, found {symbols}"
    symbol = getattr(chat.server, symbols[0])
    assert hasattr(symbol, "stream")
    assert inspect.isasyncgenfunction(symbol.stream)

    build_app = inspect.signature(chat.server.build_app)
    provider_param = build_app.parameters["provider"]
    assert provider_param.kind is inspect.Parameter.KEYWORD_ONLY
    assert provider_param.default is None

    service_init = inspect.signature(chat.service.ChatService.__init__)
    assert "provider" in service_init.parameters
    assert service_init.parameters["provider"].kind is inspect.Parameter.KEYWORD_ONLY
    assert service_init.parameters["provider"].default is None


def test_today_provider_events_are_the_frozen_union() -> None:
    events = set(typing.get_args(kernel.provider.ProviderEvent))
    assert events == {
        kernel.provider.TextDelta,
        kernel.provider.ToolCallReady,
        kernel.provider.UsageReport,
        kernel.provider.ProviderFailure,
        kernel.provider.StreamEnd,
    }

"""P6: the owner-only, tenant-scoped, content-free admin read surface."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

from chat import ChatAPI, ChatService, ChatStore, Request
from chat.api import RateLimiter
from chat.runs import RunRecorder
from kernel.usage import InMemoryUsageStore, SpendCaps, SpendGuard, UsageRecord
from openshell.audit import AuditLog
from payments.entitlements import PlanLimits, StaticEntitlementStore

from .chatkit import MODEL, StubProvider, text_script

AUTH = {"authorization": "Bearer tok"}

#: The frozen §6 field list, restated here so a test does not import a private
#: test constant from another module.
ADMIN_FIELDS = {
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
}
FORBIDDEN_KEYS = {
    "prompt_text",
    "message_text",
    "tool_arguments",
    "tool_results",
    "provider_response_body",
    "api_key",
    "secret_values",
    "headers",
    "cookies",
}
SECRET_VALUE = "fake-secret-value-123"
PROMPT = "super-secret-prompt-do-not-echo"


def _usage(
    tenant_id: str,
    *,
    cost: float | None,
    finished_at: str,
    cost_known: bool = True,
) -> UsageRecord:
    return UsageRecord(
        turn_id=f"turn_{tenant_id}",
        session_id=None,
        model=MODEL,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        estimated_cost_usd=cost,
        cost_known=cost_known,
        usage_reported=True,
        steps=1,
        tool_calls=0,
        started_at=finished_at,
        finished_at=finished_at,
        duration_ms=1,
        outcome="completed",
        tenant_id=tenant_id,
    )


def _make_api(
    tmp_path: Path,
    *,
    usage_store: InMemoryUsageStore | None = None,
    audit: AuditLog | None = None,
    limiter: RateLimiter | None = None,
) -> tuple[ChatAPI, ChatService, ChatStore]:
    store = ChatStore(tmp_path / "chat.db")
    provider = StubProvider([text_script("Hello")])
    service = ChatService(
        store=store,
        provider=provider,
        model=MODEL,
        secrets={"FAKE_PROVIDER_TOKEN": SECRET_VALUE},
        usage_store=usage_store,
        spend_guard=(
            SpendGuard(usage_store, SpendCaps(day_usd=5.0, month_usd=20.0))
            if usage_store is not None
            else None
        ),
        audit=audit,
        entitlements=StaticEntitlementStore(
            {
                "owner-unlimited": PlanLimits(plan_id="owner-unlimited"),
                "pilot": PlanLimits(
                    plan_id="pilot",
                    window="month",
                    max_turns=100,
                    max_runs=50,
                    allowed_task_types=("chat",),
                ),
            },
            default_plan_id="owner-unlimited",
        ),
    )
    api = ChatAPI(
        service,
        auth_token="tok",
        csrf_token="csrf",
        limiter=limiter,
    )
    return api, service, store


def _call(
    api: ChatAPI,
    method: str,
    path: str,
    *,
    query: Mapping[str, list[str]] | None = None,
    auth: bool = True,
    token: str = "tok",
) -> Any:
    headers: dict[str, str] = {}
    if auth:
        headers["authorization"] = f"Bearer {token}"
    if method.upper() in {"POST", "PATCH", "DELETE"}:
        headers["x-csrf-token"] = "csrf"
    return api.handle(Request(method, path, query or {}, headers))


def _body(response: Any) -> dict[str, Any]:
    return json.loads(response.body)


# -- owner-only access -----------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/admin/summary",
        "/api/admin/tenants",
        "/api/admin/tenants/owner",
        "/api/admin/runs",
        "/api/admin/runs/run_missing",
        "/api/admin/audit",
    ],
)
def test_every_admin_route_rejects_an_unauthenticated_caller(
    tmp_path: Path, path: str
) -> None:
    api, _service, store = _make_api(tmp_path)
    try:
        for kwargs in ({"auth": False}, {"token": "not-the-owner"}):
            response = _call(api, "GET", path, **kwargs)
            assert response.status == 401, (path, kwargs, response.body)
            assert _body(response)["ok"] is False
            assert b"owner" not in response.body
    finally:
        api.close()
        store.close()


# -- tenant scoping --------------------------------------------------------


def test_summary_and_indexes_only_include_the_callers_tenant(
    tmp_path: Path,
) -> None:
    usage = InMemoryUsageStore()
    usage.record(
        _usage(
            "owner",
            cost=1.25,
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
    )
    usage.record(
        _usage(
            "acme",
            cost=99.0,
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
    )
    api, service, store = _make_api(tmp_path, usage_store=usage)
    recorder = RunRecorder(store)
    try:
        owner_run = recorder.create(
            tenant_id="owner",
            thread_id="ses_owner",
            turn_id="turn_owner",
            created_at="2026-11-09T10:00:00Z",
        )
        foreign_run = recorder.create(
            tenant_id="acme",
            thread_id="ses_acme",
            turn_id="turn_acme",
            created_at="2026-11-09T10:01:00Z",
        )

        summary = _body(_call(api, "GET", "/api/admin/summary"))
        assert summary["tenant_id"] == "owner"
        assert summary["summary"]["runs"]["total"] == 1
        assert summary["summary"]["runs"]["by_status"]["queued"] == 1
        assert summary["summary"]["usage"]["records"] == 1
        assert summary["summary"]["usage"]["total_tokens"] == 15
        assert summary["summary"]["spend"]["month"]["used_usd"] == 1.25
        assert summary["summary"]["spend"]["month"]["limit_usd"] == 20.0
        assert summary["summary"]["plan_id"] == "owner-unlimited"

        tenants = _body(_call(api, "GET", "/api/admin/tenants"))
        assert [item["tenant_id"] for item in tenants["tenants"]] == ["owner"]
        assert tenants["tenants"][0]["runs"]["total"] == 1

        detail = _call(api, "GET", "/api/admin/tenants/owner")
        assert detail.status == 200
        tenant = _body(detail)["tenant"]
        assert tenant["tenant_id"] == "owner"
        assert tenant["plan"]["plan_id"] == "owner-unlimited"
        assert tenant["usage"]["records"] == 1
        assert tenant["runs"]["total"] == 1

        foreign = _call(api, "GET", "/api/admin/tenants/acme")
        missing = _call(api, "GET", "/api/admin/tenants/ghost")
        assert foreign.status == 404
        assert missing.status == 404
        assert foreign.body == missing.body

        runs = _body(_call(api, "GET", "/api/admin/runs"))
        assert [row["run_id"] for row in runs["runs"]] == [owner_run.run_id]
        assert foreign_run.run_id not in {
            row["run_id"] for row in runs["runs"]
        }

        owner_lookup = _call(api, "GET", f"/api/admin/runs/{owner_run.run_id}")
        assert owner_lookup.status == 200
        foreign_lookup = _call(api, "GET", f"/api/admin/runs/{foreign_run.run_id}")
        missing_lookup = _call(api, "GET", "/api/admin/runs/run_missing")
        assert foreign_lookup.status == 404
        assert foreign_lookup.body == missing_lookup.body
    finally:
        api.close()
        store.close()


# -- contract projection / content exclusions ------------------------------


def test_admin_rows_use_the_frozen_field_list_and_leak_no_prompt_or_secret(
    tmp_path: Path,
) -> None:
    audit = AuditLog(tmp_path / "audit" / "audit.jsonl")
    api, service, store = _make_api(tmp_path, audit=audit)
    recorder = RunRecorder(store, on_terminal=lambda _run: None)
    try:
        # A real prompt exists in the chat store and in a deliberately wide
        # audit extra. Neither may reach an admin response.
        thread = service.create_thread()
        store.create_message(
            thread_id=thread["id"],
            turn_id="turn_secret",
            role="user",
            status="complete",
            blocks=[{"type": "text", "text": PROMPT}],
        )
        audit.record(
            "tool_call",
            tenant_id="owner",
            turn_id="turn_secret",
            request_id="req-owned",
            tool="web.search",
            decision="allow",
            command=["echo", PROMPT],
            extra={"prompt_text": PROMPT, "api_key": SECRET_VALUE},
        )
        audit.record("tool_call", tenant_id="acme", actor="system")

        no_turn = recorder.create(
            tenant_id="owner",
            thread_id=thread["id"],
            created_at="2026-11-09T10:00:00Z",
        )
        with_turn = recorder.create(
            tenant_id="owner",
            thread_id=thread["id"],
            turn_id="turn_secret",
            created_at="2026-11-09T10:01:00Z",
        )

        paths = [
            "/api/admin/summary",
            "/api/admin/tenants",
            "/api/admin/tenants/owner",
            "/api/admin/runs",
            f"/api/admin/runs/{no_turn.run_id}",
            f"/api/admin/runs/{with_turn.run_id}",
            "/api/admin/audit",
        ]
        bodies: list[bytes] = []
        for path in paths:
            response = _call(api, "GET", path)
            assert response.status == 200, (path, response.body)
            bodies.append(response.body)
            assert PROMPT.encode() not in response.body
            assert SECRET_VALUE.encode() not in response.body
            assert response.headers.get("X-Request-Id")

        run_rows = _body(_call(api, "GET", "/api/admin/runs"))["runs"]
        assert {row["run_id"] for row in run_rows} == {
            no_turn.run_id,
            with_turn.run_id,
        }
        for row in run_rows:
            assert set(row) <= ADMIN_FIELDS
            assert not (set(row) & FORBIDDEN_KEYS)
        no_turn_row = next(
            row for row in run_rows if row["run_id"] == no_turn.run_id
        )
        assert "turn_id" not in no_turn_row

        detail = _body(_call(api, "GET", "/api/admin/tenants/owner"))["tenant"]
        assert detail["keys"]["FAKE_PROVIDER_TOKEN"] is True
        assert all(isinstance(value, bool) for value in detail["keys"].values())
        assert SECRET_VALUE not in json.dumps(detail)

        audit_rows = _body(_call(api, "GET", "/api/admin/audit"))["audit"]
        assert audit_rows
        assert all(row["tenant_id"] == "owner" for row in audit_rows)
        for row in audit_rows:
            assert "command" not in row
            assert "argument_hash" not in row
            assert "extra" not in row
        serialized = json.dumps(audit_rows)
        assert PROMPT not in serialized
        assert SECRET_VALUE not in serialized
        assert service is not None
    finally:
        api.close()
        store.close()


# -- input handling --------------------------------------------------------


def test_admin_invalid_input_is_typed_and_creates_nothing(tmp_path: Path) -> None:
    api, _service, store = _make_api(tmp_path)
    try:
        bad_limit = _call(
            api, "GET", "/api/admin/runs", query={"limit": ["lots"]}
        )
        assert bad_limit.status == 400
        assert _body(bad_limit)["error"]["code"] == "invalid_request"

        bad_offset = _call(
            api, "GET", "/api/admin/audit", query={"offset": ["-1x"]}
        )
        assert bad_offset.status == 400

        bad_status = _call(
            api, "GET", "/api/admin/runs", query={"status": ["zombie"]}
        )
        assert bad_status.status == 400
        assert _body(bad_status)["error"]["code"] == "invalid_request"

        not_get = _call(api, "POST", "/api/admin/summary")
        assert not_get.status == 405
        assert not_get.headers["Allow"] == "GET"

        unknown = _call(api, "GET", "/api/admin/nope")
        assert unknown.status == 404
        assert _body(unknown)["error"]["code"] == "not_found"

        malformed_run = _call(api, "GET", "/api/admin/runs/run bad!")
        assert malformed_run.status == 404

        assert store.list_threads(tenant_id="owner")[0] == []
        assert store.list_runs(tenant_id="owner") == []
    finally:
        api.close()
        store.close()


# -- rate-limiter class ----------------------------------------------------


def test_admin_reads_spend_the_admin_limiter_class(tmp_path: Path) -> None:
    limiter = RateLimiter({"admin": (0.0, 1.0)})
    api, _service, store = _make_api(tmp_path, limiter=limiter)
    try:
        first = _call(api, "GET", "/api/admin/summary")
        assert first.status == 200
        second = _call(api, "GET", "/api/admin/tenants")
        assert second.status == 429
        error = _body(second)["error"]
        assert error["code"] == "rate_limited"
        assert error["details"]["class"] == "admin"
    finally:
        api.close()
        store.close()

"""KEL-14 M2-D: Prometheus metrics and end-to-end request correlation."""

from __future__ import annotations

import asyncio
import http.client
import io
import json
import math
import threading
from pathlib import Path
from typing import Any

import pytest

from chat import ChatAPI, ChatService, ChatStore, Request
from chat.api import _Handler, make_server
from chat.logging import configure_logging
from chat.metrics import (
    LoggedUsageStore,
    Metrics,
    PROMETHEUS_CONTENT_TYPE,
)
from kernel.provider import ProviderFailure
from kernel.usage import InMemoryUsageStore, JsonlUsageStore, UsageRecord
from openshell.audit import AuditLog, SecretScrubber

from .chatkit import (
    MODEL,
    StubProvider,
    executor_factory,
    make_service,
    send_and_wait,
    text_script,
    tool_script,
)

AUTH = {"authorization": "Bearer tok"}
CSRF = {"x-csrf-token": "csrf"}
SECRET = "planted-secret-value-42"


@pytest.fixture(autouse=True)
def reset_logging() -> Any:
    configure_logging(level="info", secret_values=(), stream=None, enabled=False)
    yield
    configure_logging(level="info", secret_values=(), stream=None, enabled=False)


def samples(text: str) -> dict[str, float]:
    parsed: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        series, _, value = line.rpartition(" ")
        parsed[series] = float(value)
    return parsed


def make_api(
    tmp_path: Path,
    *,
    metrics: Metrics | None = None,
    audit: AuditLog | None = None,
    usage_store: Any | None = None,
) -> tuple[ChatAPI, ChatService, ChatStore]:
    store = ChatStore(tmp_path / "chat.db")
    service = ChatService(
        store=store,
        provider=StubProvider([text_script("Hello", usage=(10, 5))]),
        model=MODEL,
        audit=audit,
        metrics=metrics,
        usage_store=usage_store,
    )
    api = ChatAPI(
        service,
        auth_token="tok",
        csrf_token="csrf",
        metrics=metrics,
    )
    return api, service, store


def call(
    api: ChatAPI,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    auth: bool = True,
) -> Any:
    request_headers = dict(AUTH) if auth else {}
    if method.upper() in {"POST", "PATCH", "DELETE"}:
        request_headers.update(CSRF)
    if body is not None:
        request_headers["content-type"] = "application/json"
    request_headers.update(headers or {})
    raw = json.dumps(body).encode() if body is not None else b""
    return api.handle(Request(method, path, {}, request_headers, raw))


# -- registry ---------------------------------------------------------------


def test_every_named_series_is_exposed_with_exact_increments() -> None:
    metrics = Metrics()

    metrics.observe_http("get", "/api/threads", 200, 0.004)
    metrics.observe_http("get", "/api/threads", 200, 0.020)
    metrics.observe_turn("stop", 0.5)
    metrics.observe_turn("error", 2.0)
    metrics.add_tokens("input", MODEL, 10)
    metrics.add_tokens("output", MODEL, 5)
    metrics.add_cost(0.0001)
    metrics.add_cost(0.0002)
    metrics.observe_tool("docs.answer", "done")
    metrics.observe_tool("docs.answer", "done")
    metrics.observe_tool("docs.answer", "failed")
    metrics.observe_approval_wait(12.5)
    metrics.inc_sse_reconnect()
    metrics.inc_sse_reconnect()
    metrics.add_recoveries(3)

    rendered = metrics.render()
    values = samples(rendered)

    assert values[
        'http_requests_total{method="GET",route="/api/threads",status="200"}'
    ] == 2
    assert values[
        'http_request_duration_seconds_count'
        '{method="GET",route="/api/threads",status="200"}'
    ] == 2
    assert math.isclose(
        values[
            'http_request_duration_seconds_sum'
            '{method="GET",route="/api/threads",status="200"}'
        ],
        0.024,
    )
    assert values['turns_total{outcome="stop"}'] == 1
    assert values['turns_total{outcome="error"}'] == 1
    assert values["turn_duration_seconds_count"] == 2
    assert math.isclose(values["turn_duration_seconds_sum"], 2.5)
    assert values[f'tokens_total{{direction="input",model="{MODEL}"}}'] == 10
    assert values[f'tokens_total{{direction="output",model="{MODEL}"}}'] == 5
    assert math.isclose(values["cost_usd_total"], 0.0003)
    assert values['tool_calls_total{tool="docs.answer",status="done"}'] == 2
    assert values['tool_calls_total{tool="docs.answer",status="failed"}'] == 1
    assert values["approval_wait_seconds_count"] == 1
    assert math.isclose(values["approval_wait_seconds_sum"], 12.5)
    assert values["sse_reconnects_total"] == 2
    assert values["recoveries_total"] == 3

    for name in (
        "http_requests_total",
        "http_request_duration_seconds",
        "turns_total",
        "turn_duration_seconds",
        "tokens_total",
        "cost_usd_total",
        "tool_calls_total",
        "approval_wait_seconds",
        "sse_reconnects_total",
        "recoveries_total",
    ):
        assert f"# TYPE {name} " in rendered


def test_histogram_buckets_are_cumulative() -> None:
    metrics = Metrics()
    metrics.observe_approval_wait(0.3)
    metrics.observe_approval_wait(0.7)
    values = samples(metrics.render())

    assert values['approval_wait_seconds_bucket{le="0.1"}'] == 0
    assert values['approval_wait_seconds_bucket{le="0.5"}'] == 1
    assert values['approval_wait_seconds_bucket{le="1"}'] == 2
    assert values['approval_wait_seconds_bucket{le="+Inf"}'] == 2
    assert values["approval_wait_seconds_count"] == 2
    assert math.isclose(values["approval_wait_seconds_sum"], 1.0)


def test_label_values_are_scrubbed_in_the_exposition() -> None:
    metrics = Metrics(scrubber=SecretScrubber([SECRET]))
    metrics.add_tokens("input", f"model-{SECRET}", 1)
    rendered = metrics.render()
    assert SECRET not in rendered
    assert 'model="model-[redacted]"' in rendered


# -- endpoint ---------------------------------------------------------------


def test_metrics_endpoint_is_auth_gated_prometheus_text(tmp_path: Path) -> None:
    metrics = Metrics()
    api, service, store = make_api(tmp_path, metrics=metrics)
    try:
        unauthenticated = call(api, "GET", "/api/metrics", auth=False)
        assert unauthenticated.status == 401
        assert "X-Request-Id" in unauthenticated.headers

        response = call(api, "GET", "/api/metrics")
        assert response.status == 200
        assert response.headers["Content-Type"] == PROMETHEUS_CONTENT_TYPE
        assert response.headers["Cache-Control"] == "no-store"
        assert "X-Request-Id" in response.headers
        text = response.body.decode()
        assert "# TYPE http_requests_total counter" in text
        assert "# TYPE turns_total counter" in text
        assert "# TYPE tokens_total counter" in text
        assert "# TYPE cost_usd_total counter" in text
        assert "# TYPE tool_calls_total counter" in text
        assert "# TYPE sse_reconnects_total counter" in text
        assert "# TYPE recoveries_total counter" in text
        assert "# TYPE http_request_duration_seconds histogram" in text
        assert "# TYPE turn_duration_seconds histogram" in text
        assert "# TYPE approval_wait_seconds histogram" in text

        assert call(api, "POST", "/api/metrics").status in {403, 404}
    finally:
        api.close()
        store.close()


def test_response_always_carries_a_request_id(tmp_path: Path) -> None:
    metrics = Metrics()
    api, _service, store = make_api(tmp_path, metrics=metrics)
    try:
        supplied = call(
            api,
            "GET",
            "/api/healthz",
            headers={"x-request-id": "req_caller_123"},
        )
        assert supplied.headers["X-Request-Id"] == "req_caller_123"

        injected = call(
            api,
            "GET",
            "/api/healthz",
            headers={"x-request-id": "bad\nid"},
        )
        assert injected.headers["X-Request-Id"] != "bad\nid"
        assert injected.headers["X-Request-Id"].startswith("req_")

        error = call(api, "GET", "/api/threads", auth=False)
        assert error.status == 401
        assert error.headers["X-Request-Id"].startswith("req_")
    finally:
        api.close()
        store.close()


def test_sse_resume_increments_the_reconnect_counter(tmp_path: Path) -> None:
    metrics = Metrics()
    api, service, store = make_api(tmp_path, metrics=metrics)
    try:
        thread = service.create_thread()
        fresh = call(api, "GET", f"/api/threads/{thread['id']}/stream")
        assert fresh.status == 200
        assert metrics.sse_reconnects.value() == 0
        assert fresh.stream is not None
        fresh.stream.close()

        resumed = call(
            api, "GET", f"/api/threads/{thread['id']}/stream", headers={"Last-Event-ID": "4"}
        )
        assert resumed.status == 200
        assert metrics.sse_reconnects.value() == 1
        assert resumed.stream is not None
        resumed.stream.close()
    finally:
        api.close()
        store.close()


def test_server_entrypoint_wires_the_logged_usage_store_and_registry(
    tmp_path: Path,
) -> None:
    from chat.metrics import Metrics as NewMetrics
    from chat.server import ServerConfig, build_app

    class NoopProvider:
        async def stream(self, request: Any) -> Any:  # pragma: no cover
            raise AssertionError("no turn runs")
            yield  # pragma: no cover - async generator

    env = {
        "NEBIUS_API_KEY": "test-api-key-1234567890",
        "NEBIUS_TOKEN_FACTORY_BASE_URL": "http://127.0.0.1:9/v1",
        "KELP_AUTH_TOKEN": "owner-token-1234567890",
        "KELP_DB_PATH": str(tmp_path / "db" / "chat.db"),
        "KELP_ARTIFACT_DIR": str(tmp_path / "artifacts"),
        "KELP_AUDIT_DIR": str(tmp_path / "audit"),
        "KELP_USAGE_DIR": str(tmp_path / "usage"),
        "KELP_WORKSPACE_ROOT": str(tmp_path / "workspace"),
    }
    app = build_app(ServerConfig.from_env(env), provider=NoopProvider(), environ=env)
    try:
        assert isinstance(app.service._usage_store, LoggedUsageStore)
        assert isinstance(app.api.metrics, NewMetrics)
        assert app.service._metrics is app.api.metrics
        assert app.service._audit is app.audit
        # The registry is present at zero and served through the API.
        response = app.api.handle(
            Request(
                "GET",
                "/api/metrics",
                {},
                {"authorization": "Bearer owner-token-1234567890"},
                b"",
            )
        )
        assert response.status == 200
        assert "recoveries_total 0" in response.body.decode()
    finally:
        app.close()


# -- end-to-end correlation -------------------------------------------------


def test_one_send_shares_one_request_id_across_logs_audit_and_usage(
    tmp_path: Path,
) -> None:
    stream = io.StringIO()
    configure_logging(level="debug", secret_values=(SECRET,), stream=stream)
    audit = AuditLog(tmp_path / "audit" / "audit.jsonl", secret_values=(SECRET,))
    usage_path = tmp_path / "usage" / "usage.jsonl"
    inner = JsonlUsageStore(usage_path)
    metrics = Metrics(scrubber=SecretScrubber([SECRET]))
    api, service, store = make_api(
        tmp_path,
        metrics=metrics,
        audit=audit,
        usage_store=LoggedUsageStore(inner),
    )
    prompt = "PROMPT-MUST-NOT-LEAK-123"
    request_id = "req_shared_abc123"
    try:
        thread = service.create_thread()
        response = call(
            api,
            "POST",
            f"/api/threads/{thread['id']}/turns",
            body={"client_turn_id": "ct-one-id", "text": prompt},
            headers={"x-request-id": request_id},
        )
        assert response.status == 202, response.body
        assert response.headers["X-Request-Id"] == request_id
        payload = json.loads(response.body)
        api.runner.run(service.wait_for_turn(payload["turn_id"]))
    finally:
        api.close()
        store.close()

    lines = [json.loads(line) for line in stream.getvalue().splitlines() if line]
    access = [line for line in lines if line["event"] == "http_request"]
    turns = [line for line in lines if line["event"] == "turn_finished"]
    usage_lines = [line for line in lines if line["event"] == "usage"]

    assert len(access) == 1
    assert access[0]["request_id"] == request_id
    assert access[0]["route"] == "/api/threads/{id}/turns"
    assert access[0]["status"] == 202
    assert len(turns) == 1
    assert turns[0]["request_id"] == request_id
    assert turns[0]["turn"] == payload["turn_id"]
    assert len(usage_lines) == 1
    assert usage_lines[0]["request_id"] == request_id

    audit_entries = audit.entries(event="turn_finished")
    assert len(audit_entries) == 1
    assert audit_entries[0]["request_id"] == request_id
    assert audit_entries[0]["turn_id"] == payload["turn_id"]

    records = inner.records()
    assert len(records) == 1
    assert records[0]["request_id"] == request_id
    assert records[0]["turn_id"] == payload["turn_id"]

    metrics_text = metrics.render()
    values = samples(metrics_text)
    assert values['http_requests_total'
                 '{method="POST",route="/api/threads/{id}/turns",status="202"}'] == 1
    assert values['turns_total{outcome="stop"}'] == 1
    assert values["turn_duration_seconds_count"] == 1
    assert values[f'tokens_total{{direction="input",model="{MODEL}"}}'] == 10
    assert values[f'tokens_total{{direction="output",model="{MODEL}"}}'] == 5
    assert math.isclose(values["cost_usd_total"], 0.0000075)

    for text in (
        stream.getvalue(),
        (tmp_path / "audit" / "audit.jsonl").read_text(encoding="utf-8"),
        usage_path.read_text(encoding="utf-8"),
        metrics_text,
    ):
        assert prompt not in text
        assert SECRET not in text


def test_planted_secret_is_redacted_in_logs_metrics_and_audit(tmp_path: Path) -> None:
    stream = io.StringIO()
    configure_logging(level="debug", secret_values=(SECRET,), stream=stream)
    audit = AuditLog(tmp_path / "audit" / "audit.jsonl", secret_values=(SECRET,))
    metrics = Metrics(scrubber=SecretScrubber([SECRET]))

    from chat.logging import log

    log("probe", model=SECRET, thread_id=f"t-{SECRET}")
    metrics.add_tokens("input", f"m-{SECRET}", 1)
    audit.record("probe", extra={"value": SECRET})

    assert SECRET not in stream.getvalue()
    assert SECRET not in metrics.render()
    assert SECRET not in (tmp_path / "audit" / "audit.jsonl").read_text(
        encoding="utf-8"
    )
    assert "[redacted]" in stream.getvalue()
    assert "[redacted]" in metrics.render()
    assert "[redacted]" in (tmp_path / "audit" / "audit.jsonl").read_text(
        encoding="utf-8"
    )


# -- histogram overflow -----------------------------------------------------


def test_observation_above_the_largest_bucket_still_counts() -> None:
    """``+Inf`` and ``_count`` include every observation, not the last bucket."""

    metrics = Metrics()
    metrics.observe_approval_wait(0.3)
    metrics.observe_approval_wait(999.0)  # above the largest finite bucket
    metrics.observe_approval_wait(3600.0)
    values = samples(metrics.render())

    assert values['approval_wait_seconds_bucket{le="600"}'] == 1
    assert values['approval_wait_seconds_bucket{le="+Inf"}'] == 3
    assert values["approval_wait_seconds_count"] == 3
    assert math.isclose(values["approval_wait_seconds_sum"], 0.3 + 999.0 + 3600.0)
    assert metrics.approval_wait.count() == 3


def test_histogram_overflow_is_counted_per_label_set() -> None:
    metrics = Metrics()
    metrics.observe_http("GET", "/api/threads", 200, 0.001)
    metrics.observe_http("GET", "/api/threads", 200, 60.0)
    metrics.observe_http("GET", "/api/threads", 500, 60.0)
    values = samples(metrics.render())
    ok = '{method="GET",route="/api/threads",status="200"}'
    failed = '{method="GET",route="/api/threads",status="500"}'

    assert values[f"http_request_duration_seconds_count{ok}"] == 2
    assert (
        values[
            'http_request_duration_seconds_bucket'
            '{method="GET",route="/api/threads",status="200",le="+Inf"}'
        ]
        == 2
    )
    assert values[f"http_request_duration_seconds_count{failed}"] == 1
    assert (
        values[
            'http_request_duration_seconds_bucket'
            '{method="GET",route="/api/threads",status="500",le="+Inf"}'
        ]
        == 1
    )
    assert metrics.http_duration.count(
        method="GET", route="/api/threads", status="200"
    ) == 2


def test_non_finite_measurements_never_reach_the_exposition() -> None:
    metrics = Metrics()
    metrics.observe_http("GET", "/api/threads", 200, float("nan"))
    metrics.observe_turn("stop", float("inf"))
    metrics.observe_approval_wait(-0.0)
    metrics.add_cost(float("nan"))
    metrics.add_tokens("input", MODEL, 4)

    rendered = metrics.render()
    assert "nan" not in rendered.lower()
    assert "-inf" not in rendered.lower()
    # A non-finite observation is dropped, so the histogram has no series and
    # the cost counter stays at the zero it was booted with.
    assert "http_request_duration_seconds_count" not in rendered
    values = samples(rendered)
    assert values["cost_usd_total"] == 0
    assert values[f'tokens_total{{direction="input",model="{MODEL}"}}'] == 4


# -- usage store proxy ------------------------------------------------------


def _usage_record(**overrides: Any) -> UsageRecord:
    values: dict[str, Any] = {
        "turn_id": "turn_1",
        "session_id": "thread_1",
        "model": MODEL,
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "estimated_cost_usd": None,
        "cost_known": False,
        "usage_reported": True,
        "steps": 1,
        "tool_calls": 0,
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:00:01Z",
        "duration_ms": 1000,
        "outcome": "stop",
    }
    values.update(overrides)
    return UsageRecord(**values)


def test_logged_usage_store_reads_in_memory_records_and_forwards_len() -> None:
    stream = io.StringIO()
    configure_logging(level="debug", stream=stream)
    inner = InMemoryUsageStore()
    store = LoggedUsageStore(inner)
    record = _usage_record()

    store.record(record)

    assert len(store) == 1
    assert store.records() == [record.to_dict()]
    line = json.loads(stream.getvalue().splitlines()[-1])
    assert line["event"] == "usage"
    assert line["turn"] == "turn_1"
    assert line["cost_known"] is False
    assert line["usage_reported"] is True
    assert "cost_usd" not in line  # unknown cost is absent, not zero


# -- tool-call labels and error codes --------------------------------------


def test_model_authored_tool_name_is_bucketed_before_logs_metrics_and_audit(
    tmp_path: Path,
) -> None:
    stream = io.StringIO()
    configure_logging(level="debug", secret_values=(SECRET,), stream=stream)
    metrics = Metrics(scrubber=SecretScrubber([SECRET]))
    audit = AuditLog(tmp_path / "audit" / "audit.jsonl", secret_values=(SECRET,))
    crafted = 'docs.answer"}' + SECRET + "\n" + "z" * 300

    async def executor(call: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "skill": "docs.answer",
            "tool": "docs.answer",
            "duration_ms": 1,
            "output": {"answer": "ok"},
        }

    scripts = [
        tool_script(crafted, {"query": "q"}, call_id="call_bad"),
        tool_script("docs.answer", {"query": "q"}, call_id="call_good"),
        text_script("done"),
    ]
    service, store, _provider = make_service(
        tmp_path,
        scripts,
        tool_executor_factory=executor_factory(executor),
        tools=({"type": "function", "function": {"name": "docs.answer"}},),
        metrics=metrics,
        audit=audit,
    )
    try:
        thread = service.create_thread()

        async def scenario() -> None:
            await send_and_wait(service, thread["id"], "go")

        asyncio.run(scenario())
    finally:
        store.close()

    values = samples(metrics.render())
    assert values['tool_calls_total{tool="unknown",status="done"}'] == 1
    assert values['tool_calls_total{tool="docs.answer",status="done"}'] == 1
    rendered = metrics.render()
    assert SECRET not in rendered
    assert "z" * 40 not in rendered

    lines = [json.loads(line) for line in stream.getvalue().splitlines() if line]
    calls = [line for line in lines if line["event"] == "tool_call"]
    assert [call["tool"] for call in calls] == ["unknown", "docs.answer"]
    assert all(len(call["tool"]) <= 64 for call in calls)
    assert SECRET not in stream.getvalue()
    assert crafted not in stream.getvalue()

    entries = audit.entries(event="tool_call")
    assert [entry["tool"] for entry in entries] == ["unknown", "docs.answer"]
    audit_text = (tmp_path / "audit" / "audit.jsonl").read_text(encoding="utf-8")
    assert SECRET not in audit_text
    assert "z" * 40 not in audit_text


def test_tool_error_code_reaches_log_and_audit_but_never_a_metric_label(
    tmp_path: Path,
) -> None:
    stream = io.StringIO()
    configure_logging(level="debug", stream=stream)
    metrics = Metrics()
    audit = AuditLog(tmp_path / "audit" / "audit.jsonl")

    async def executor(call: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "skill": "docs.answer",
            "tool": "docs.answer",
            "duration_ms": 2,
            "error": {"code": "tool_timeout", "message": "LEAKY-ERROR-DETAIL"},
        }

    scripts = [
        tool_script("docs.answer", {"query": "q"}),
        text_script("sorry"),
    ]
    service, store, _provider = make_service(
        tmp_path,
        scripts,
        tool_executor_factory=executor_factory(executor),
        tools=({"type": "function", "function": {"name": "docs.answer"}},),
        metrics=metrics,
        audit=audit,
    )
    try:
        thread = service.create_thread()

        async def scenario() -> None:
            await send_and_wait(service, thread["id"], "go")

        asyncio.run(scenario())
    finally:
        store.close()

    lines = [json.loads(line) for line in stream.getvalue().splitlines() if line]
    calls = [line for line in lines if line["event"] == "tool_call"]
    assert len(calls) == 1
    assert calls[0]["status"] == "failed"
    assert calls[0]["error_code"] == "tool_timeout"
    assert "LEAKY-ERROR-DETAIL" not in stream.getvalue()

    entries = audit.entries(event="tool_call")
    assert entries[0]["extra"]["error_code"] == "tool_timeout"
    assert "LEAKY-ERROR-DETAIL" not in (
        tmp_path / "audit" / "audit.jsonl"
    ).read_text(encoding="utf-8")

    rendered = metrics.render()
    values = samples(rendered)
    assert values['tool_calls_total{tool="docs.answer",status="failed"}'] == 1
    assert "tool_timeout" not in rendered


class _ExplodingProvider:
    def __init__(self, message: str):
        self.message = message

    async def stream(self, request: Any) -> Any:  # pragma: no cover - async gen
        raise RuntimeError(self.message)
        yield  # pragma: no cover - makes this an async generator


def test_kernel_error_code_is_threaded_into_the_finalized_turn(tmp_path: Path) -> None:
    stream = io.StringIO()
    configure_logging(level="debug", stream=stream)
    metrics = Metrics()
    audit = AuditLog(tmp_path / "audit" / "audit.jsonl")
    store = ChatStore(tmp_path / "chat.db")
    provider = StubProvider(
        [
            [
                ProviderFailure(
                    code="provider_unavailable",
                    message="LEAKY-PROVIDER-BODY",
                    retryable=True,
                )
            ]
        ]
    )
    service = ChatService(
        store=store, provider=provider, model=MODEL, audit=audit, metrics=metrics
    )
    try:
        thread = service.create_thread()

        async def scenario() -> None:
            await send_and_wait(service, thread["id"], "go")

        asyncio.run(scenario())
    finally:
        store.close()

    lines = [json.loads(line) for line in stream.getvalue().splitlines() if line]
    errors = [line for line in lines if line["event"] == "turn_error"]
    finished = [line for line in lines if line["event"] == "turn_finished"]
    assert errors and errors[0]["error_code"] == "provider_unavailable"
    assert len(finished) == 1
    assert finished[0]["status"] == "error"
    assert finished[0]["error_code"] == "provider_unavailable"
    assert "LEAKY-PROVIDER-BODY" not in stream.getvalue()

    entry = audit.entries(event="turn_finished")[0]
    assert entry["extra"]["error_code"] == "provider_unavailable"
    assert "provider_unavailable" not in metrics.render()
    assert samples(metrics.render())['turns_total{outcome="error"}'] == 1


def test_catch_all_logs_only_the_exception_class_name(tmp_path: Path) -> None:
    stream = io.StringIO()
    configure_logging(level="debug", stream=stream)
    metrics = Metrics()
    audit = AuditLog(tmp_path / "audit" / "audit.jsonl")
    store = ChatStore(tmp_path / "chat.db")
    leaked = "EXCEPTION-MESSAGE-MUST-NOT-LEAK"
    service = ChatService(
        store=store,
        provider=_ExplodingProvider(leaked),
        model=MODEL,
        audit=audit,
        metrics=metrics,
    )
    try:
        thread = service.create_thread()

        async def scenario() -> None:
            await send_and_wait(service, thread["id"], "go")

        asyncio.run(scenario())
    finally:
        store.close()

    raw = stream.getvalue()
    lines = [json.loads(line) for line in raw.splitlines() if line]
    crashed = [line for line in lines if line["event"] == "turn_crashed"]
    finished = [line for line in lines if line["event"] == "turn_finished"]
    assert crashed and crashed[0]["error_code"] == "RuntimeError"
    assert finished[0]["status"] == "error"
    assert finished[0]["error_code"] == "provider_error"
    assert leaked not in raw
    assert leaked not in (tmp_path / "audit" / "audit.jsonl").read_text(
        encoding="utf-8"
    )
    assert "RuntimeError" not in metrics.render()
    assert samples(metrics.render())['turns_total{outcome="error"}'] == 1


# -- stdlib handler --------------------------------------------------------


def test_stdlib_handler_logging_is_silenced_and_errors_use_the_json_sink() -> None:
    stream = io.StringIO()
    configure_logging(level="debug", stream=stream)

    handler = object.__new__(_Handler)
    handler.log_message('"%s" %s %s', "GET /api/threads HTTP/1.1", "400", "-")
    handler.log_error(
        "code %s message %s", "400", "Bad request version (GET /api/threads)"
    )

    lines = [json.loads(line) for line in stream.getvalue().splitlines() if line]
    assert [line["event"] for line in lines] == ["http_error"]
    assert lines[0]["level"] == "warning"
    assert "GET /api/threads" not in stream.getvalue()


def test_served_request_emits_one_json_access_line_and_no_stderr_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stream = io.StringIO()
    configure_logging(level="debug", stream=stream)
    api, _service, store = make_api(tmp_path, metrics=Metrics())
    server = make_server(api, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[0], server.server_address[1]
        connection = http.client.HTTPConnection(host, port, timeout=5)
        connection.request("GET", "/api/healthz")
        response = connection.getresponse()
        assert response.status == 200
        response.read()
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        api.close()
        store.close()
        thread.join(timeout=5)

    captured = capsys.readouterr()
    assert "GET /api/healthz" not in captured.err
    assert "- - [" not in captured.err

    lines = [json.loads(line) for line in stream.getvalue().splitlines() if line]
    access = [line for line in lines if line["event"] == "http_request"]
    assert len(access) == 1
    assert access[0]["route"] == "/api/healthz"
    assert access[0]["status"] == 200


def test_malformed_and_oversized_error_codes_are_bounded(tmp_path: Path) -> None:
    stream = io.StringIO()
    configure_logging(level="debug", stream=stream)
    metrics = Metrics()
    audit = AuditLog(tmp_path / "audit" / "audit.jsonl")
    store = ChatStore(tmp_path / "chat.db")
    long_code = "a" * 200
    provider = StubProvider(
        [
            [
                ProviderFailure(
                    code="bad code\nwith-control", message="x", retryable=False
                )
            ],
            [ProviderFailure(code=long_code, message="x", retryable=False)],
        ]
    )
    service = ChatService(
        store=store, provider=provider, model=MODEL, audit=audit, metrics=metrics
    )
    try:
        async def scenario() -> None:
            for index in range(2):
                thread = service.create_thread()
                await send_and_wait(service, thread["id"], f"go {index}")

        asyncio.run(scenario())
    finally:
        store.close()

    raw = stream.getvalue()
    finished = [
        json.loads(line)
        for line in raw.splitlines()
        if line and json.loads(line)["event"] == "turn_finished"
    ]
    assert [entry["error_code"] for entry in finished] == ["unknown", "a" * 64]
    assert "bad code" not in raw
    assert long_code not in raw
    entries = audit.entries(event="turn_finished")
    assert [entry["extra"]["error_code"] for entry in entries] == [
        "unknown",
        "a" * 64,
    ]

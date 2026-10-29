"""KEL-14 M2-D: Prometheus metrics and end-to-end request correlation."""

from __future__ import annotations

import io
import json
import math
from pathlib import Path
from typing import Any

import pytest

from chat import ChatAPI, ChatService, ChatStore, Request
from chat.logging import configure_logging
from chat.metrics import (
    LoggedUsageStore,
    Metrics,
    PROMETHEUS_CONTENT_TYPE,
)
from kernel.usage import JsonlUsageStore
from openshell.audit import AuditLog, SecretScrubber

from .chatkit import MODEL, StubProvider, text_script

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

"""KEL-14 M2-D: structured logging contract.

One JSON object per line, an allow-listed field set, ambient request ids, and
mandatory redaction. These tests pin the rules that keep prompt text out of
stdout and a planted secret out of every sink.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from chat.logging import (
    configure_logging,
    current_request_id,
    is_valid_request_id,
    log,
    log_level,
    new_request_id,
    request_context,
    reset_request_id,
    set_request_id,
)


@pytest.fixture(autouse=True)
def reset_logging() -> Any:
    configure_logging(level="info", secret_values=(), stream=None, enabled=False)
    yield
    configure_logging(level="info", secret_values=(), stream=None, enabled=False)


def _lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if line.strip()
    ]


def test_record_carries_the_contract_fields() -> None:
    stream = io.StringIO()
    configure_logging(level="debug", stream=stream)

    with request_context("req_test_123"):
        record = log(
            "http_request",
            thread_id="th_1",
            turn="turn_1",
            tool="docs.answer",
            duration_ms=12,
            status=200,
            method="GET",
            route="/api/threads/{id}",
        )

    assert record is not None
    parsed = _lines(stream)[0]
    for field in (
        "ts",
        "level",
        "event",
        "request_id",
        "thread_id",
        "turn",
        "tool",
        "duration_ms",
        "status",
    ):
        assert parsed[field] == record[field]
    assert parsed["event"] == "http_request"
    assert parsed["level"] == "info"
    assert parsed["request_id"] == "req_test_123"
    assert parsed["duration_ms"] == 12
    assert parsed["status"] == 200
    assert parsed["ts"].endswith("Z")


def test_prompt_body_and_argument_text_never_reach_the_log() -> None:
    stream = io.StringIO()
    configure_logging(level="debug", stream=stream)

    log(
        "leak_attempt",
        request_id="req_leak",
        status=200,
        text="PROMPT-MUST-NOT-LEAK",
        body="BODY-MUST-NOT-LEAK",
        prompt="SYSTEM-PROMPT-MUST-NOT-LEAK",
        arguments={"query": "ARGUMENT-MUST-NOT-LEAK"},
        content="CONTENT-MUST-NOT-LEAK",
        result={"output": "RESULT-MUST-NOT-LEAK"},
        note="NOTE-MUST-NOT-LEAK",
    )

    raw = stream.getvalue()
    for planted in (
        "PROMPT-MUST-NOT-LEAK",
        "BODY-MUST-NOT-LEAK",
        "SYSTEM-PROMPT-MUST-NOT-LEAK",
        "ARGUMENT-MUST-NOT-LEAK",
        "CONTENT-MUST-NOT-LEAK",
        "RESULT-MUST-NOT-LEAK",
        "NOTE-MUST-NOT-LEAK",
    ):
        assert planted not in raw
    parsed = _lines(stream)[0]
    assert set(parsed) == {"ts", "level", "event", "request_id", "status"}
    assert parsed["event"] == "leak_attempt"


def test_log_level_is_honoured() -> None:
    stream = io.StringIO()
    configure_logging(level="warning", stream=stream)

    assert log("debug_line", level="debug") is None
    assert log("info_line", level="info") is None
    assert stream.getvalue() == ""
    assert log_level() == "warning"

    assert log("warning_line", level="warning") is not None
    assert log("error_line", level="error") is not None
    events = [line["event"] for line in _lines(stream)]
    assert events == ["warning_line", "error_line"]

    configure_logging(level="error", stream=stream)
    assert log("late_warning", level="warning") is None


def test_unknown_level_falls_back_to_info() -> None:
    stream = io.StringIO()
    assert configure_logging(level="verbose", stream=stream) == "info"
    assert log("info_line", level="info") is not None
    assert log("debug_line", level="debug") is None


def test_request_id_is_ambient_and_scoped() -> None:
    stream = io.StringIO()
    configure_logging(level="debug", stream=stream)
    assert current_request_id() is None

    token = set_request_id("req_outer")
    try:
        log("outer")
        with request_context("req_inner"):
            log("inner")
        log("outer_again")
    finally:
        reset_request_id(token)

    assert current_request_id() is None
    ids = [(line["event"], line["request_id"]) for line in _lines(stream)]
    assert ids == [
        ("outer", "req_outer"),
        ("inner", "req_inner"),
        ("outer_again", "req_outer"),
    ]


def test_secret_values_are_redacted_from_every_field() -> None:
    secret = "planted-secret-value-42"
    stream = io.StringIO()
    configure_logging(level="debug", secret_values=(secret,), stream=stream)

    log(
        "redaction",
        thread_id=f"thread-{secret}",
        status=secret,
        model=secret,
        error_code=f"code {secret}",
    )
    log("authorization", status="Authorization: Bearer abc.def.ghi")

    raw = stream.getvalue()
    assert secret not in raw
    assert "abc.def.ghi" not in raw
    assert raw.count("[redacted]") >= 4
    parsed = _lines(stream)[0]
    assert parsed["thread_id"] == "thread-[redacted]"
    assert parsed["status"] == "[redacted]"
    assert parsed["model"] == "[redacted]"


def test_request_id_validation_blocks_log_injection() -> None:
    generated = new_request_id()
    assert generated.startswith("req_") and len(generated) == 36
    assert is_valid_request_id("req_abc-123.def:1")
    assert is_valid_request_id(generated)
    assert not is_valid_request_id("")
    assert not is_valid_request_id("bad id with spaces")
    assert not is_valid_request_id("req_ok\nsecond_line")
    assert not is_valid_request_id("x" * 129)
    assert not is_valid_request_id(None)


def test_request_id_rejects_every_trailing_control_character() -> None:
    """``re.match`` with ``$`` accepts a final newline; ``fullmatch`` does not."""

    for value in (
        "req_ok\n",
        "req_ok\r",
        "req_ok\n\n",
        "req_ok\t",
        " req_ok",
        "req_ok ",
        "req_ok\u2028",
        "req_ok\x00",
    ):
        assert not is_valid_request_id(value), repr(value)
    assert is_valid_request_id("req_ok")


def test_an_invalid_ambient_request_id_is_not_echoed() -> None:
    stream = io.StringIO()
    configure_logging(level="debug", stream=stream)

    with request_context("req_ok\nforged: true"):
        record = log("suspicious")

    assert record is not None
    assert "request_id" not in record
    parsed = _lines(stream)[0]
    assert "request_id" not in parsed
    assert "\nforged" not in stream.getvalue()


def test_non_finite_numbers_are_dropped_not_serialised() -> None:
    stream = io.StringIO()
    configure_logging(level="debug", stream=stream)

    record = log(
        "impossible_measurement",
        duration_ms=float("nan"),
        cost_usd=float("inf"),
        status=200,
        count=7,
    )

    assert record is not None
    assert "duration_ms" not in record
    assert "cost_usd" not in record
    assert record["status"] == 200
    assert record["count"] == 7
    parsed = _lines(stream)[0]
    assert parsed == record
    raw = stream.getvalue()
    assert "NaN" not in raw
    assert "Infinity" not in raw


def test_server_entrypoint_applies_kelp_log_level(tmp_path: Any) -> None:
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
        "KELP_LOG_LEVEL": "warning",
    }
    app = build_app(ServerConfig.from_env(env), provider=NoopProvider(), environ=env)
    try:
        assert log_level() == "warning"
        stream = io.StringIO()
        configure_logging(stream=stream)  # keep the configured level
        assert log("suppressed", level="info") is None
        assert log("emitted", level="warning") is not None
        assert [line["event"] for line in _lines(stream)] == ["emitted"]
    finally:
        app.close()

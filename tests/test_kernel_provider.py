"""Tests for the Token Factory streaming client (KEL-6)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from kernel.config import KernelConfig
from kernel.provider import (
    ChatRequest,
    ProviderFailure,
    StreamEnd,
    TextDelta,
    TokenFactoryClient,
    ToolCallReady,
    UsageReport,
    classify_status,
)

API_KEY = "test-key-not-a-real-secret"


def sse(*chunks: Any, done: bool = True) -> str:
    body = "\n\n".join(f"data: {json.dumps(chunk)}" for chunk in chunks)
    if done:
        body += "\n\ndata: [DONE]"
    return body + "\n\n"


def client_with(handler: Any) -> TokenFactoryClient:
    config = KernelConfig(api_key=API_KEY)
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return TokenFactoryClient(config, client=http)


def request() -> ChatRequest:
    return ChatRequest(
        model="nvidia/Nemotron-3_5-Super-VL",
        messages=[{"role": "user", "content": "hello"}],
        tools=[{"type": "function", "function": {"name": "web.search", "parameters": {}}}],
        max_tokens=256,
    )


def drain(client: TokenFactoryClient) -> list[Any]:
    async def run() -> list[Any]:
        async with client:
            return [event async for event in client.stream(request())]

    return asyncio.run(run())


def test_text_stream_and_usage() -> None:
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(
            200,
            text=sse(
                {"choices": [{"delta": {"role": "assistant"}}]},
                {"choices": [{"delta": {"content": "Hel"}}]},
                {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13}},
            ),
        )

    events = drain(client_with(handler))
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Hel", "lo"]
    assert UsageReport(11, 2, 13) in events
    assert events[-1] == StreamEnd("stop")
    body = captured["body"]
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["max_tokens"] == 256
    assert body["tool_choice"] == "auto"
    assert body["messages"] == [{"role": "user", "content": "hello"}]


def test_tool_call_deltas_are_assembled() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=sse(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "function": {"name": "web.search", "arguments": '{"qu'},
                                    }
                                ]
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {"index": 0, "function": {"arguments": 'ery": "nebius"}'}}
                                ]
                            }
                        }
                    ]
                },
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ),
        )

    events = drain(client_with(handler))
    calls = [e for e in events if isinstance(e, ToolCallReady)]
    assert len(calls) == 1
    assert calls[0].call_id == "call_1"
    assert calls[0].name == "web.search"
    assert calls[0].arguments == {"query": "nebius"}
    assert events[-1] == StreamEnd("tool_calls")


def test_missing_api_key_is_a_typed_event() -> None:
    def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("no request should be made without a key")

    client = TokenFactoryClient(KernelConfig(api_key=None), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    events = drain(client)
    assert len(events) == 1
    assert isinstance(events[0], ProviderFailure)
    assert events[0].code == "missing_api_key"
    assert events[0].retryable is False


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, "provider_auth", False),
        (403, "provider_auth", False),
        (404, "provider_model_not_found", False),
        (408, "provider_timeout", True),
        (429, "provider_rate_limited", True),
        (500, "provider_unavailable", True),
        (503, "provider_unavailable", True),
        (400, "provider_rejected", False),
    ],
)
def test_http_status_classification(status: int, code: str, retryable: bool) -> None:
    assert classify_status(status) == (code, retryable)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "upstream detail"}})

    events = drain(client_with(handler))
    assert len(events) == 1
    failure = events[0]
    assert isinstance(failure, ProviderFailure)
    assert failure.code == code
    assert failure.retryable is retryable
    assert failure.status == status
    assert API_KEY not in failure.message
    assert "upstream detail" not in failure.message


def test_read_timeout_is_retryable() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow upstream", request=req)

    events = drain(client_with(handler))
    assert isinstance(events[0], ProviderFailure)
    assert events[0].code == "provider_timeout"
    assert events[0].retryable is True


def test_connection_error_is_retryable() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=req)

    events = drain(client_with(handler))
    assert isinstance(events[0], ProviderFailure)
    assert events[0].code == "provider_unreachable"
    assert events[0].retryable is True


def test_malformed_chunk_is_protocol_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="data: {not json}\n\ndata: [DONE]\n\n")

    events = drain(client_with(handler))
    assert isinstance(events[0], ProviderFailure)
    assert events[0].code == "provider_protocol_error"
    assert events[0].retryable is True


def test_invalid_tool_arguments_are_rejected() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=sse(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "function": {"name": "web.search", "arguments": "{bad"},
                                    }
                                ]
                            }
                        }
                    ]
                },
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ),
        )

    events = drain(client_with(handler))
    assert isinstance(events[0], ProviderFailure)
    assert events[0].code == "invalid_tool_arguments"
    assert events[0].retryable is False


def test_repr_never_contains_the_api_key() -> None:
    client = client_with(lambda req: httpx.Response(200, text=sse()))
    assert API_KEY not in repr(client)
    assert API_KEY not in str(client)
    assert "api_key_configured=True" in repr(client)


def test_auth_header_is_sent_but_never_echoed() -> None:
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["authorization"] = req.headers.get("authorization")
        return httpx.Response(200, text=sse({"choices": [{"delta": {"content": "ok"}}]}))

    events = drain(client_with(handler))
    assert captured["authorization"] == f"Bearer {API_KEY}"
    for event in events:
        assert API_KEY not in repr(event)


def test_done_marker_is_required_for_clean_end() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse({"choices": [{"delta": {"content": "ok"}}]}, done=False))

    events = drain(client_with(handler))
    assert events[-1] == StreamEnd("stop")

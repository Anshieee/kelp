"""Tests for the OpenAI-compatible streaming provider client (KEL-6)."""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any, Iterator

import httpx
import pytest

from kernel.config import KernelConfig
from kernel.provider import (
    ChatRequest,
    ProviderFailure,
    StreamEnd,
    TextDelta,
    ProviderClient,
    ToolCallReady,
    UsageReport,
    classify_status,
)

API_KEY = "test-key-not-a-real-secret"
BASE_URL = "https://provider.example.test/v1"


def sse(*chunks: Any, done: bool = True) -> str:
    body = "\n\n".join(f"data: {json.dumps(chunk)}" for chunk in chunks)
    if done:
        body += "\n\ndata: [DONE]"
    return body + "\n\n"


def client_with(handler: Any) -> ProviderClient:
    config = KernelConfig(api_key=API_KEY, base_url=BASE_URL)
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return ProviderClient(config, client=http)


def request() -> ChatRequest:
    return ChatRequest(
        model="nvidia/nemotron-3.5-super-vl",
        messages=[{"role": "user", "content": "hello"}],
        tools=[{"type": "function", "function": {"name": "web.search", "parameters": {}}}],
        max_tokens=256,
    )


def drain(client: ProviderClient) -> list[Any]:
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
                                    {"index": 0, "function": {"arguments": 'ery": "gpu"}'}}
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
    assert calls[0].arguments == {"query": "gpu"}
    assert events[-1] == StreamEnd("tool_calls")


def test_tool_names_are_rewritten_for_the_wire_and_restored() -> None:
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
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
                                        "function": {
                                            "name": "web_search",
                                            "arguments": '{"query": "x"}',
                                        },
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
    assert captured["body"]["tools"][0]["function"]["name"] == "web_search"
    calls = [e for e in events if isinstance(e, ToolCallReady)]
    assert [call.name for call in calls] == ["web.search"]


def test_transcript_tool_names_are_rewritten_too() -> None:
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(
            200, text=sse({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        )

    async def run() -> list[Any]:
        request = ChatRequest(
            model="m",
            messages=[
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "web.search", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "name": "web.search", "content": "{}"},
            ],
            tools=[
                {"type": "function", "function": {"name": "web.search", "parameters": {}}}
            ],
        )
        async with client_with(handler) as client:
            return [event async for event in client.stream(request)]

    asyncio.run(run())
    messages = captured["body"]["messages"]
    assert messages[1]["tool_calls"][0]["function"]["name"] == "web_search"
    assert messages[2]["name"] == "web_search"


def test_top_level_combinators_are_dropped_from_parameters() -> None:
    captured: dict[str, Any] = {}
    schema = {
        "type": "object",
        "properties": {"kind": {"enum": ["a", "b"]}},
        "required": ["kind"],
        "oneOf": [{"required": ["kind"]}],
        "anyOf": [{"required": ["kind"]}],
        "allOf": [{"required": ["kind"]}],
        "not": {"required": ["other"]},
        "const": "kind",
        "enum": ["a", "b"],
    }

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(
            200, text=sse({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        )

    async def run() -> list[Any]:
        request = ChatRequest(
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            tools=[
                {
                    "type": "function",
                    "function": {"name": "github.read", "parameters": schema},
                }
            ],
        )
        async with client_with(handler) as client:
            return [event async for event in client.stream(request)]

    events = asyncio.run(run())
    assert events[-1] == StreamEnd("stop")
    parameters = captured["body"]["tools"][0]["function"]["parameters"]
    for refused in ("oneOf", "anyOf", "allOf", "not", "const", "enum"):
        assert refused not in parameters
    assert parameters["type"] == "object"
    assert parameters["properties"] == {"kind": {"enum": ["a", "b"]}}


def test_truncation_collisions_round_trip() -> None:
    """Two long dotted names that truncate alike get distinct wires and restore."""
    first = "a." + "x" * 70
    second = "a." + "y" * 70
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        tools = captured["body"]["tools"]
        wires = [tool["function"]["name"] for tool in tools]
        assert wires[0] != wires[1]
        assert all(len(wire) <= 64 for wire in wires)
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
                                        "function": {
                                            "name": wires[0],
                                            "arguments": "{}",
                                        },
                                    },
                                    {
                                        "index": 1,
                                        "id": "call_2",
                                        "function": {
                                            "name": wires[1],
                                            "arguments": "{}",
                                        },
                                    },
                                ]
                            }
                        }
                    ]
                },
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ),
        )

    async def run() -> list[Any]:
        request = ChatRequest(
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            tools=[
                {"type": "function", "function": {"name": first, "parameters": {}}},
                {"type": "function", "function": {"name": second, "parameters": {}}},
            ],
        )
        async with client_with(handler) as client:
            return [event async for event in client.stream(request)]

    events = asyncio.run(run())
    calls = [e for e in events if isinstance(e, ToolCallReady)]
    assert [call.name for call in calls] == [first, second]


def test_unknown_tool_name_passes_through() -> None:
    """A name the model invents is delivered as-is, not dropped or mapped."""

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
                                        "function": {
                                            "name": "hallucinated_tool",
                                            "arguments": "{}",
                                        },
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
    calls = [e for e in events if isinstance(e, ToolCallReady)]
    assert [call.name for call in calls] == ["hallucinated_tool"]


def test_only_refused_keys_leaves_an_object_schema() -> None:
    """A parameters map with nothing but refused keys still wires as an object."""
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(
            200, text=sse({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        )

    async def run() -> list[Any]:
        request = ChatRequest(
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "github.read",
                        "parameters": {"oneOf": [{"required": ["kind"]}]},
                    },
                }
            ],
        )
        async with client_with(handler) as client:
            return [event async for event in client.stream(request)]

    events = asyncio.run(run())
    assert events[-1] == StreamEnd("stop")
    assert captured["body"]["tools"][0]["function"]["parameters"] == {"type": "object"}


def test_tenant_mismatch_never_reaches_the_wire() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        seen.append(req)
        raise AssertionError("a tenant-mismatched request must not be sent")

    async def run() -> list[Any]:
        request = ChatRequest(
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "web.search", "parameters": {}}}],
            tenant_id="someone-else",
        )
        async with client_with(handler) as client:
            return [event async for event in client.stream(request)]

    events = asyncio.run(run())
    assert len(events) == 1
    assert isinstance(events[0], ProviderFailure)
    assert events[0].code == "tenant_mismatch"
    assert events[0].retryable is False
    assert seen == []


def test_missing_api_key_is_a_typed_event() -> None:
    def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("no request should be made without a key")

    client = ProviderClient(
        KernelConfig(api_key=None, base_url=BASE_URL),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    events = drain(client)
    assert len(events) == 1
    assert isinstance(events[0], ProviderFailure)
    assert events[0].code == "missing_api_key"
    assert events[0].retryable is False


def test_missing_base_url_is_a_typed_event() -> None:
    """No default host: an unset endpoint must never inherit one."""

    def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("no request should be made without a base URL")

    client = ProviderClient(
        KernelConfig(api_key=API_KEY, base_url=None),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    events = drain(client)
    assert len(events) == 1
    assert isinstance(events[0], ProviderFailure)
    assert events[0].code == "missing_base_url"
    assert events[0].retryable is False
    assert "KELP_PROVIDER_BASE_URL" in events[0].message


_PROXY_VARS = (
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "all_proxy",
    "ALL_PROXY",
    "no_proxy",
    "NO_PROXY",
)


def _point_http_proxy_at(monkeypatch: pytest.MonkeyPatch, port: int) -> None:
    """Clear inherited proxy settings and point ``http_proxy`` at one sink.

    ``no_proxy`` must be cleared as well: urlopen and HTTPX otherwise exclude
    loopback from proxying, and the absolute-form proof would silently pass.
    """

    for name in _PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{port}")


@contextmanager
def _fake_provider_server() -> Iterator[SimpleNamespace]:
    """A disposable loopback endpoint that also records each request target.

    It answers any POST with one synthetic SSE completion, so it can stand in
    for both the provider origin and an HTTP proxy sink. No external host is
    contacted and no real model token is produced.
    """

    targets: list[tuple[str, str]] = []
    body = sse(
        {
            "choices": [
                {
                    "delta": {"content": "fake-local-token"},
                    "finish_reason": "stop",
                }
            ]
        }
    ).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server API
            length = int(self.headers.get("content-length", "0"))
            self.rfile.read(length)
            targets.append((self.command, self.path))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield SimpleNamespace(port=int(server.server_address[1]), targets=targets)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def _assert_owned_http_config(
    client: ProviderClient, config: KernelConfig, *, trust_env: bool
) -> httpx.AsyncClient:
    """Assert only public ``httpx.AsyncClient`` attributes on the repo seam."""

    owned = client._client
    assert owned.trust_env is trust_env
    assert owned.follow_redirects is False
    assert owned.timeout.connect == config.connect_timeout_s
    assert owned.timeout.read == config.read_timeout_s
    assert owned.timeout.write == config.request_write_timeout_s
    assert owned.is_closed is False
    return owned


def test_trust_env_false_seals_the_owned_transport_and_uses_origin_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Local provider ignores process proxy settings and dials the endpoint.

    The same loopback server is the configured endpoint and the ``http_proxy``
    sink, so its recorded request target distinguishes the branches on the wire:
    the sealed client sends origin-form, while an env-trusting client would send
    the absolute-form target to the proxy.
    """

    with _fake_provider_server() as fake:
        _point_http_proxy_at(monkeypatch, fake.port)
        config = KernelConfig(
            api_key=API_KEY, base_url=f"http://127.0.0.1:{fake.port}/v1"
        )

        async def run() -> tuple[list[Any], httpx.AsyncClient]:
            client = ProviderClient(config, trust_env=False)
            owned = _assert_owned_http_config(client, config, trust_env=False)
            try:
                events = [event async for event in client.stream(request())]
            finally:
                await client.aclose()
            return events, owned

        events, owned = asyncio.run(run())
        assert owned.is_closed is True
        assert [e.text for e in events if isinstance(e, TextDelta)] == [
            "fake-local-token"
        ]
        assert events[-1] == StreamEnd("stop")
        assert fake.targets == [("POST", "/v1/chat/completions")]


def test_hosted_default_trusts_the_proxy_and_injected_clients_keep_their_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``trust_env`` defaults to true; an injected client keeps its config/owner."""

    with _fake_provider_server() as fake:
        _point_http_proxy_at(monkeypatch, fake.port)
        config = KernelConfig(
            api_key=API_KEY, base_url=f"http://127.0.0.1:{fake.port}/v1"
        )

        async def run() -> tuple[list[Any], httpx.AsyncClient]:
            client = ProviderClient(config)
            owned = _assert_owned_http_config(client, config, trust_env=True)
            try:
                events = [event async for event in client.stream(request())]
            finally:
                await client.aclose()
            return events, owned

        events, owned = asyncio.run(run())
        assert owned.is_closed is True
        assert events[-1] == StreamEnd("stop")
        # HTTPX sends absolute-form to an HTTP proxy; the sink records it.
        assert fake.targets == [
            ("POST", f"http://127.0.0.1:{fake.port}/v1/chat/completions")
        ]

        async def injected_run() -> None:
            injected = httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda req: httpx.Response(200, text=sse())
                )
            )
            try:
                client = ProviderClient(config, client=injected, trust_env=False)
                assert client._client is injected
                assert injected.trust_env is True
                await client.aclose()
                assert injected.is_closed is False
            finally:
                await injected.aclose()

        asyncio.run(injected_run())


def test_config_reads_the_provider_environment_and_has_no_default_host() -> None:
    config = KernelConfig.from_env(
        {"KELP_PROVIDER_BASE_URL": "https://openrouter.ai/api/v1/",
         "KELP_PROVIDER_API_KEY": API_KEY}
    )
    assert config.base_url == "https://openrouter.ai/api/v1"
    assert config.api_key == API_KEY
    assert config.base_url_configured is True

    blank = KernelConfig.from_env({"KELP_PROVIDER_BASE_URL": "   "})
    assert blank.base_url is None
    assert blank.base_url_configured is False



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


class _RecordingLedger:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def record(self, **kwargs: Any) -> None:
        self.rows.append(kwargs)


def test_stream_without_done_marker_or_finish_reason_is_a_protocol_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=sse({"choices": [{"delta": {"content": "upstream secret text"}}]}, done=False),
        )

    ledger = _RecordingLedger()
    client = client_with(handler)
    client.ledger = ledger  # type: ignore[assignment]
    events = drain(client)

    assert not any(isinstance(event, StreamEnd) for event in events)
    failure = events[-1]
    assert isinstance(failure, ProviderFailure)
    assert failure.code == "provider_protocol_error"
    assert failure.retryable is True
    assert "upstream secret text" not in failure.message
    assert [row["outcome"] for row in ledger.rows] == ["provider_protocol_error"]


def test_empty_stream_without_done_marker_is_a_protocol_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="")

    events = drain(client_with(handler))
    assert isinstance(events[-1], ProviderFailure)
    assert events[-1].code == "provider_protocol_error"


def test_finish_reason_without_done_marker_still_ends_cleanly() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=sse(
                {"choices": [{"delta": {"content": "ok"}}]},
                {"choices": [{"delta": {}, "finish_reason": "length"}]},
                done=False,
            ),
        )

    ledger = _RecordingLedger()
    client = client_with(handler)
    client.ledger = ledger  # type: ignore[assignment]
    events = drain(client)

    assert events[-1] == StreamEnd("length")
    assert not any(isinstance(event, ProviderFailure) for event in events)
    assert [row["outcome"] for row in ledger.rows] == ["completed"]

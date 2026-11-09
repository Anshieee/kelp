"""OpenAI-compatible chat completions provider.

The endpoint is required configuration (``KELP_PROVIDER_BASE_URL``): there is no
default host, so no transcript can be sent anywhere the operator did not name.

Design rules:

- The client yields typed events instead of raising; the loop turns them into
  user-visible error events. Network errors never escape as exceptions.
- Streaming tool calls are assembled from deltas and yielded once, with parsed
  JSON arguments.
- The API key is never included in an exception, event, or ``repr``.
- ``ChatRequest.tenant_id`` must equal the tenant this client is bound to, and
  it is never written into the request body.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Mapping

import httpx

from openshell.egress_ledger import EgressLedger, recipient_for_host

from ..config import KernelConfig
from .base import (
    OWNER_TENANT_ID,
    ChatRequest,
    ProviderEvent,
    ProviderFailure,
    StreamEnd,
    TextDelta,
    ToolCallReady,
    UsageReport,
    classify_status,
    tenant_failure,
)

__all__ = ["OpenAICompatibleClient"]


_FAILURE_MESSAGES = {
    "provider_auth": "The model provider rejected the API key (HTTP {status}).",
    "provider_model_not_found": "The model provider does not expose this model (HTTP 404).",
    "provider_timeout": "The model provider timed out (HTTP {status}).",
    "provider_rate_limited": "The model provider rate-limited the request (HTTP 429); retry after a pause.",
    "provider_unavailable": "The model provider is unavailable (HTTP {status}); retry is safe.",
    "provider_rejected": "The model provider rejected the request (HTTP {status}).",
    "provider_error": "The model provider returned an unexpected status (HTTP {status}).",
}


def _status_failure(status: int) -> ProviderFailure:
    code, retryable = classify_status(status)
    message = _FAILURE_MESSAGES.get(
        code, "The model provider request failed (HTTP {status})."
    )
    return ProviderFailure(code, message.format(status=status), retryable, status)


async def _iter_sse_data(lines: AsyncIterator[str]) -> AsyncIterator[str]:
    """Yield the payload of each SSE event, joining multi-line ``data:`` fields."""

    buffer: list[str] = []
    async for line in lines:
        if line == "":
            if buffer:
                yield "\n".join(buffer)
                buffer.clear()
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            buffer.append(line[5:].lstrip(" "))
    if buffer:
        yield "\n".join(buffer)


@dataclass
class _CallAccumulator:
    call_id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass
class _StreamState:
    calls: dict[int, _CallAccumulator] = field(default_factory=dict)
    finish_reason: str | None = None

    def handle(self, chunk: Mapping[str, Any]) -> list[ProviderEvent]:
        events: list[ProviderEvent] = []

        usage = chunk.get("usage")
        if isinstance(usage, Mapping):
            input_tokens = int(usage.get("prompt_tokens") or 0)
            output_tokens = int(usage.get("completion_tokens") or 0)
            total_tokens = int(usage.get("total_tokens") or input_tokens + output_tokens)
            if input_tokens or output_tokens:
                events.append(UsageReport(input_tokens, output_tokens, total_tokens))

        choices = chunk.get("choices")
        if not isinstance(choices, list):
            return events
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            finish = choice.get("finish_reason")
            if isinstance(finish, str) and finish:
                self.finish_reason = finish
            delta = choice.get("delta")
            if not isinstance(delta, Mapping):
                continue
            content = delta.get("content")
            if isinstance(content, str) and content:
                events.append(TextDelta(content))
            tool_calls = delta.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for entry in tool_calls:
                if not isinstance(entry, Mapping):
                    continue
                index = int(entry.get("index", len(self.calls)))
                accumulator = self.calls.setdefault(index, _CallAccumulator())
                if entry.get("id"):
                    accumulator.call_id = str(entry["id"])
                function = entry.get("function")
                if isinstance(function, Mapping):
                    name = function.get("name")
                    if isinstance(name, str) and name and not accumulator.name:
                        accumulator.name = name
                    arguments = function.get("arguments")
                    if isinstance(arguments, str):
                        accumulator.arguments += arguments
        return events

    def finish(self) -> list[ProviderEvent]:
        events: list[ProviderEvent] = []
        for index in sorted(self.calls):
            accumulator = self.calls[index]
            if not accumulator.name:
                continue
            raw = accumulator.arguments.strip()
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return [
                    ProviderFailure(
                        "invalid_tool_arguments",
                        "The model produced tool arguments that are not valid JSON.",
                        retryable=False,
                    )
                ]
            if not isinstance(parsed, Mapping):
                return [
                    ProviderFailure(
                        "invalid_tool_arguments",
                        "The model produced tool arguments that are not a JSON object.",
                        retryable=False,
                    )
                ]
            events.append(
                ToolCallReady(
                    index=index,
                    call_id=accumulator.call_id or f"call_{index}",
                    name=accumulator.name,
                    arguments=dict(parsed),
                )
            )
        events.append(StreamEnd(self.finish_reason or "stop"))
        return events


class OpenAICompatibleClient:
    """Async streaming client for an OpenAI-compatible endpoint.

    Reuse one instance per process. The client is bound to one tenant and never
    sends ``ChatRequest.tenant_id`` upstream.
    """

    def __init__(
        self,
        config: KernelConfig,
        *,
        client: httpx.AsyncClient | None = None,
        ledger: EgressLedger | None = None,
        tenant_id: str = OWNER_TENANT_ID,
    ):
        self._config = config
        self.tenant_id = tenant_id
        self._owns_client = client is None
        #: KEL-22 egress account. Public so ``build_app`` can attach the
        #: process ledger after construction, keeping ``ProviderClient(config)``
        #: a one-argument call for deployments and test harnesses that replace it.
        self.ledger = ledger
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=config.connect_timeout_s,
                read=config.read_timeout_s,
                write=config.request_write_timeout_s,
                pool=config.connect_timeout_s,
            ),
            follow_redirects=False,
        )

    @classmethod
    def from_env(cls, *, client: httpx.AsyncClient | None = None) -> "OpenAICompatibleClient":
        return cls(KernelConfig.from_env(), client=client)

    @property
    def config(self) -> KernelConfig:
        return self._config

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "OpenAICompatibleClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        return f"ProviderClient(base_url={self._config.base_url!r}, api_key_configured={self._config.api_key_configured})"

    __str__ = __repr__

    def _record_egress(
        self, url: str, request_bytes: int, response_bytes: int, outcome: str
    ) -> None:
        """Ledger row for one completed, failed, or refused call.

        The recipient is resolved from the actual URL, so an operator-supplied
        ``KELP_PROVIDER_BASE_URL`` is recorded as ``unlisted`` rather than
        silently attributed to a host this repository documents.
        """

        if self.ledger is None:
            return
        try:
            host = urllib.parse.urlsplit(url).hostname
            self.ledger.record(
                recipient=recipient_for_host(host),
                purpose="chat.completions",
                request_bytes=request_bytes,
                response_bytes=response_bytes,
                outcome=outcome,
                method="POST",
                host=host,
            )
        except Exception:  # noqa: BLE001 - ledger is accounting, not traffic
            return

    async def stream(self, request: ChatRequest) -> AsyncIterator[ProviderEvent]:
        failure = tenant_failure(request, self.tenant_id)
        if failure is not None:
            yield failure
            return

        base_url = self._config.base_url
        if not base_url:
            yield ProviderFailure(
                "missing_base_url",
                "The model provider base URL is not configured (set KELP_PROVIDER_BASE_URL).",
                retryable=False,
            )
            return
        if not self._config.api_key:
            yield ProviderFailure(
                "missing_api_key",
                "The model provider API key is not configured (set KELP_PROVIDER_API_KEY).",
                retryable=False,
            )
            return

        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [dict(message) for message in request.messages],
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": request.temperature,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = max(1, int(request.max_tokens))
        if request.tools:
            payload["tools"] = [dict(tool) for tool in request.tools]
            payload["tool_choice"] = "auto"

        url = f"{base_url}/chat/completions"
        # KEL-22: one ledger row per call. Counts only — the payload itself
        # (the transcript) never touches the ledger.
        request_bytes = len(
            json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), default=str
            ).encode("utf-8")
        )
        response_bytes = 0
        outcome = "failed"
        try:
            async with self._client.stream(
                "POST", url, headers=headers, json=payload
            ) as response:
                if response.status_code >= 400:
                    outcome = "provider_error"
                    yield _status_failure(response.status_code)
                    return

                state = _StreamState()
                async for data in _iter_sse_data(response.aiter_lines()):
                    response_bytes += len(data.encode("utf-8")) + 1
                    if not data.strip():
                        continue
                    if data.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        outcome = "provider_protocol_error"
                        yield ProviderFailure(
                            "provider_protocol_error",
                            "The model provider sent a malformed streaming chunk.",
                            retryable=True,
                        )
                        return
                    if not isinstance(chunk, Mapping):
                        outcome = "provider_protocol_error"
                        yield ProviderFailure(
                            "provider_protocol_error",
                            "The model provider sent a non-object streaming chunk.",
                            retryable=True,
                        )
                        return
                    for event in state.handle(chunk):
                        if isinstance(event, ProviderFailure):
                            outcome = event.code
                            yield event
                            return
                        yield event

                for event in state.finish():
                    yield event
                outcome = "completed"
        except httpx.TimeoutException:
            outcome = "timeout"
            yield ProviderFailure(
                "provider_timeout",
                "The model provider did not respond within the configured timeout.",
                retryable=True,
            )
        except httpx.HTTPError as exc:
            outcome = "unreachable"
            yield ProviderFailure(
                "provider_unreachable",
                f"Could not reach the model provider ({type(exc).__name__}).",
                retryable=True,
            )
        finally:
            self._record_egress(url, request_bytes, response_bytes, outcome)

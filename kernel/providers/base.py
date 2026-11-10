"""Provider-neutral request, event, and failure shapes for the kernel.

Nothing in this module knows a vendor, a host, or a wire protocol. A provider
implements one streaming method and yields the closed ``ProviderEvent`` union;
the agent loop consumes that union and never sees a vendor response body.

Failures are values, not exceptions: a provider call yields a ``ProviderFailure``
and returns, so a broken endpoint cannot tear down a turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Mapping, Protocol, Sequence, Union

__all__ = [
    "ChatRequest",
    "OWNER_TENANT_ID",
    "Provider",
    "ProviderEvent",
    "ProviderFailure",
    "StreamEnd",
    "TextDelta",
    "ToolCall",
    "ToolCallReady",
    "UsageReport",
    "classify_status",
    "tenant_failure",
]

#: The only tenant in this version. A provider instance is bound to one tenant
#: and refuses a request that names another.
OWNER_TENANT_ID = "owner"


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class ChatRequest:
    """One model request. Field order is part of the frozen contract.

    ``tenant_id`` is the sixth field and always last. It identifies the bound
    principal's tenant for the lifetime of the call; it is never serialized into
    the upstream request body.
    """

    model: str
    messages: Sequence[Mapping[str, Any]]
    tools: Sequence[Mapping[str, Any]] = ()
    temperature: float = 0.2
    max_tokens: int | None = None
    tenant_id: str = OWNER_TENANT_ID


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolCallReady:
    index: int
    call_id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class UsageReport:
    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class ProviderFailure:
    code: str
    message: str
    retryable: bool
    status: int | None = None


@dataclass(frozen=True)
class StreamEnd:
    finish_reason: str


ProviderEvent = Union[TextDelta, ToolCallReady, UsageReport, ProviderFailure, StreamEnd]


def classify_status(status: int) -> tuple[str, bool]:
    """Map an HTTP status to ``(code, retryable)``. No body is included."""

    if status in (401, 403):
        return "provider_auth", False
    if status == 404:
        return "provider_model_not_found", False
    if status == 408:
        return "provider_timeout", True
    if status == 429:
        return "provider_rate_limited", True
    if 500 <= status < 600:
        return "provider_unavailable", True
    if 400 <= status < 500:
        return "provider_rejected", False
    return "provider_error", status >= 500


def tenant_failure(request: ChatRequest, tenant_id: str) -> ProviderFailure | None:
    """Refuse a request whose tenant is not the provider's bound tenant.

    This is a defensive half of the tenant gate: the caller is expected to bind
    the principal before the call, and the provider fails closed if it did not.
    The message names neither tenant, so an error row cannot leak an id.
    """

    if request.tenant_id == tenant_id:
        return None
    return ProviderFailure(
        "tenant_mismatch",
        "The request names a tenant this provider is not bound to.",
        retryable=False,
    )


class Provider(Protocol):
    """The seam every provider implements. No other method is required."""

    def stream(self, request: ChatRequest) -> AsyncIterator[ProviderEvent]:
        """Yield typed events for one request; never raise into the caller."""
        ...

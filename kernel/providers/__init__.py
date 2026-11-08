"""Provider-neutral shapes and the registry that builds a streaming client.

Public surface::

    from kernel.providers import ChatRequest, ProviderEvent, build_provider

    client = build_provider(KernelConfig.from_env())
    async for event in client.stream(ChatRequest(model="m", messages=[])):
        ...

The compatibility spellings ``kernel.provider`` and ``ProviderClient`` remain
available; they are aliases, not a second implementation.
"""

from .base import (
    OWNER_TENANT_ID,
    ChatRequest,
    Provider,
    ProviderEvent,
    ProviderFailure,
    StreamEnd,
    TextDelta,
    ToolCall,
    ToolCallReady,
    UsageReport,
    classify_status,
    tenant_failure,
)
from .openai_compatible import OpenAICompatibleClient
from .registry import ProviderPricing, ProviderSpec, build_provider

#: Compatibility spelling used by the frozen provider seam.
ProviderClient = OpenAICompatibleClient

__all__ = [
    "ChatRequest",
    "OWNER_TENANT_ID",
    "OpenAICompatibleClient",
    "Provider",
    "ProviderClient",
    "ProviderEvent",
    "ProviderFailure",
    "ProviderPricing",
    "ProviderSpec",
    "StreamEnd",
    "TextDelta",
    "ToolCall",
    "ToolCallReady",
    "UsageReport",
    "build_provider",
    "classify_status",
    "tenant_failure",
]

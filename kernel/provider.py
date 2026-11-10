"""Compatibility shim for the provider seam.

The streaming implementation moved to :mod:`kernel.providers`. This module keeps
the historical import path working: every public name is re-exported, and
``TokenFactoryClient`` is an alias for ``ProviderClient`` so a deployment built
against the contract's name-agnostic seam can rename without a code fork.

There is no wire logic here. New code should import from ``kernel.providers``.
"""

from __future__ import annotations

from .providers.base import (
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
from .providers.openai_compatible import OpenAICompatibleClient

#: The seam's historical name. Patching ``chat.server.ProviderClient`` remains
#: the supported way to replace the default provider.
ProviderClient = OpenAICompatibleClient

#: Contract-compatible alias: the same client under the other accepted name.
TokenFactoryClient = ProviderClient

__all__ = [
    "ChatRequest",
    "OWNER_TENANT_ID",
    "OpenAICompatibleClient",
    "Provider",
    "ProviderClient",
    "ProviderEvent",
    "ProviderFailure",
    "StreamEnd",
    "TextDelta",
    "TokenFactoryClient",
    "ToolCall",
    "ToolCallReady",
    "UsageReport",
    "classify_status",
    "tenant_failure",
]

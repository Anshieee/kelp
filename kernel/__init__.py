"""Kernel agent loop (KEL-6 / P0-1).

Public surface::

    from kernel import AgentLoop, TokenFactoryClient, TurnBudget, JsonlUsageStore

    client = TokenFactoryClient.from_env()
    loop = AgentLoop(
        provider=client,
        tool_executor=RegistryToolExecutor(registry),
        usage_store=JsonlUsageStore("var/usage.jsonl"),
    )
    async for event in loop.run_turn([{"role": "user", "content": "hi"}]):
        ...

Defaults: primary model ``nvidia/Nemotron-3_5-Super-VL`` (documents, vision),
background model ``nvidia/Nemotron-3_5-Lightning`` (titles, summaries, compaction).
"""

from .config import (
    API_KEY_ENV,
    DEFAULT_BACKGROUND_MODEL,
    DEFAULT_PRIMARY_MODEL,
    KernelConfig,
    ModelPricing,
    ModelRole,
    estimate_cost,
    pricing_for,
)
from .events import (
    Error,
    TokenDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
    UsageUpdated,
)
from .loop import (
    AgentLoop,
    CallableToolExecutor,
    CancelToken,
    RegistryToolExecutor,
    ToolExecutor,
    TurnBudget,
)
from .provider import (
    ChatRequest,
    ProviderFailure,
    StreamEnd,
    TextDelta,
    TokenFactoryClient,
    ToolCall,
    ToolCallReady,
    UsageReport,
)
from .usage import InMemoryUsageStore, JsonlUsageStore, UsageRecord, UsageStore

__all__ = [
    "API_KEY_ENV",
    "AgentLoop",
    "CallableToolExecutor",
    "CancelToken",
    "ChatRequest",
    "DEFAULT_BACKGROUND_MODEL",
    "DEFAULT_PRIMARY_MODEL",
    "Error",
    "InMemoryUsageStore",
    "JsonlUsageStore",
    "KernelConfig",
    "ModelPricing",
    "ModelRole",
    "ProviderFailure",
    "RegistryToolExecutor",
    "StreamEnd",
    "TextDelta",
    "TokenDelta",
    "TokenFactoryClient",
    "ToolCall",
    "ToolCallFinished",
    "ToolCallReady",
    "ToolCallStarted",
    "ToolExecutor",
    "TurnBudget",
    "TurnFinished",
    "TurnStarted",
    "UsageRecord",
    "UsageReport",
    "UsageStore",
    "UsageUpdated",
    "estimate_cost",
    "pricing_for",
]

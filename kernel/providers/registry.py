"""Provider registry: build the streaming client a configuration selects.

A ``ProviderSpec`` names an OpenAI-compatible API root, the environment variable
that holds its key, its default model, and its published pricing. Nothing here
chooses a host: an unset or blank base URL stays unset and becomes the typed
``missing_base_url`` failure on first use.

``build_provider`` accepts either a ``KernelConfig`` (the legacy one-provider
path) or a ``ProviderSpec``. The optional ``factory`` keeps the
``chat.server.ProviderClient`` seam patchable with a one-argument callable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Callable, Mapping

import httpx

from openshell.egress_ledger import EgressLedger

from ..config import KernelConfig
from .base import OWNER_TENANT_ID, Provider
from .openai_compatible import OpenAICompatibleClient

__all__ = ["ProviderPricing", "ProviderSpec", "build_provider"]


@dataclass(frozen=True)
class ProviderPricing:
    """USD per million tokens, as published by the provider."""

    input_per_million_usd: float
    output_per_million_usd: float


@dataclass(frozen=True)
class ProviderSpec:
    """Configuration record for one OpenAI-compatible provider.

    Field order is frozen by the service contract. ``api_key_env`` is a variable
    name, never a value.
    """

    name: str
    base_url: str
    api_key_env: str
    default_model: str
    pricing: ProviderPricing


def _config_from_spec(
    spec: ProviderSpec,
    *,
    api_key: str | None,
    environ: Mapping[str, str] | None,
) -> KernelConfig:
    """Map a spec to the transport config, resolving the named key variable."""

    key = api_key
    if key is None and spec.api_key_env:
        source = os.environ if environ is None else environ
        key = source.get(spec.api_key_env) or None
    return KernelConfig(
        base_url=(spec.base_url or "").strip().rstrip("/") or None,
        primary_model=spec.default_model,
        api_key=key,
    )


def build_provider(
    source: KernelConfig | ProviderSpec,
    *,
    factory: Callable[[KernelConfig], Provider] | None = None,
    api_key: str | None = None,
    environ: Mapping[str, str] | None = None,
    client: httpx.AsyncClient | None = None,
    ledger: EgressLedger | None = None,
    tenant_id: str = OWNER_TENANT_ID,
) -> Provider:
    """Return a provider implementing ``stream(ChatRequest) -> AsyncIterator``.

    ``factory`` is the patchable seam: when supplied it receives the resolved
    ``KernelConfig`` as its only argument, so a deployment may replace
    ``chat.server.ProviderClient`` with any one-argument callable. The default
    factory constructs ``OpenAICompatibleClient`` and forwards ``client``,
    ``ledger``, and ``tenant_id``.
    """

    if isinstance(source, ProviderSpec):
        config = _config_from_spec(source, api_key=api_key, environ=environ)
    else:
        config = source
        if api_key is not None:
            config = replace(config, api_key=api_key)

    if factory is not None:
        return factory(config)
    return OpenAICompatibleClient(
        config,
        client=client,
        ledger=ledger,
        tenant_id=tenant_id,
    )

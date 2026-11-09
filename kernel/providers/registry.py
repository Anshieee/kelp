"""Provider registry: build the streaming client a configuration selects.

A ``ProviderSpec`` names an OpenAI-compatible API root, the environment variable
that holds its key, its default model, and its published pricing. Nothing here
chooses a host: an unset or blank base URL stays unset and becomes the typed
``missing_base_url`` failure on first use.

Credentials are resolved through a :class:`kernel.secrets.SecretResolver` that
the caller supplies. This module never reads :data:`os.environ` on its own: the
server passes both its authoritative environment mapping and the resolver it
built at boot, so a library embedder cannot accidentally inherit a different
process environment. A missing key is not an exception here; the provider
receives ``api_key=None`` and yields the typed ``missing_api_key`` failure
before any network call.

``build_provider`` accepts either a ``KernelConfig`` (the legacy one-provider
path) or a ``ProviderSpec``. The optional ``factory`` keeps the
``chat.server.ProviderClient`` seam patchable with a one-argument callable.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Mapping

import httpx

from openshell.egress_ledger import EgressLedger

from ..config import API_KEY_ENV, KernelConfig
from ..secrets import MissingSecret, SecretResolver
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


def _resolve_key(
    name: str,
    *,
    api_key: str | None,
    resolver: SecretResolver | None,
    environ: Mapping[str, str] | None,
) -> str | None:
    """Resolve one key without ever consulting the process environment implicitly.

    Precedence is the explicit argument, then a supplied resolver. When no
    resolver is supplied, the caller's explicit mapping is consulted. A
    resolver miss is deliberately swallowed and becomes ``None`` rather than
    falling through to another source: a per-tenant resolver must fail closed,
    never silently borrow a process-wide key. The provider already has a typed
    ``missing_api_key`` path, and raising here would turn a per-request failure
    into a boot crash for a caller that expects lazy failure. The server
    performs its own fail-fast check at boot.
    """

    if api_key is not None:
        return api_key
    if resolver is not None and name:
        try:
            return resolver.resolve(name)
        except MissingSecret:
            return None
    if environ is not None and name:
        raw = environ.get(name)
        return raw if isinstance(raw, str) and raw.strip() else None
    return None


def _config_from_spec(
    spec: ProviderSpec,
    *,
    api_key: str | None,
    resolver: SecretResolver | None,
    environ: Mapping[str, str] | None,
) -> KernelConfig:
    """Map a spec to the transport config, resolving the named key variable."""

    key = _resolve_key(
        spec.api_key_env,
        api_key=api_key,
        resolver=resolver,
        environ=environ,
    )
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
    resolver: SecretResolver | None = None,
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

    ``resolver`` is the credential seam. The server passes a resolver built from
    its boot configuration. When a resolver is present it is authoritative: its
    value wins over the source config's existing key, and a miss fails closed
    with ``api_key=None`` instead of falling back to that key or to
    :data:`os.environ`. When it is absent, a ``ProviderSpec`` resolves the
    explicit ``api_key`` argument and then its ``api_key_env`` name from the
    caller's explicit ``environ`` mapping; a ``KernelConfig`` uses the explicit
    ``api_key`` argument and then its own key.
    """

    if isinstance(source, ProviderSpec):
        config = _config_from_spec(
            source,
            api_key=api_key,
            resolver=resolver,
            environ=environ,
        )
    else:
        config = source
        if api_key is not None:
            key = api_key
        elif resolver is not None:
            try:
                key = resolver.resolve(API_KEY_ENV)
            except MissingSecret:
                key = None
        else:
            key = config.api_key
        if key != config.api_key:
            config = replace(config, api_key=key)

    if factory is not None:
        return factory(config)
    return OpenAICompatibleClient(
        config,
        client=client,
        ledger=ledger,
        tenant_id=tenant_id,
    )

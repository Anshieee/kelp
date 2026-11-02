"""Kernel configuration: the model provider endpoint, model roles, and pricing.

The provider is selected by ``KELP_PROVIDER`` (Nebius Token Factory by default,
OpenRouter when set to ``openrouter``); both speak the same OpenAI-compatible
API, so only the endpoint, credential env var, and default models differ.

Secrets are read from the environment at construction time and are never echoed.
``KernelConfig.from_env`` is deliberately tolerant: a missing API key does not
crash startup, it produces a typed ``missing_api_key`` event on first use.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

__all__ = [
    "API_KEY_ENV",
    "BACKGROUND_MODEL_ENV",
    "BASE_URL_ENV",
    "DEFAULT_BACKGROUND_MODEL",
    "DEFAULT_BASE_URL",
    "DEFAULT_PRIMARY_MODEL",
    "DEFAULT_PRICING",
    "KernelConfig",
    "ModelPricing",
    "ModelRole",
    "PRIMARY_MODEL_ENV",
    "PROVIDER_BASE_URL_ENV",
    "PROVIDER_ENV",
    "PROVIDERS",
    "Provider",
    "ProviderDefaults",
]

BASE_URL_ENV = "NEBIUS_TOKEN_FACTORY_BASE_URL"
LEGACY_BASE_URL_ENV = "NEBIUS_BASE_URL"
API_KEY_ENV = "NEBIUS_API_KEY"
PRIMARY_MODEL_ENV = "KELP_PRIMARY_MODEL"
BACKGROUND_MODEL_ENV = "KELP_BACKGROUND_MODEL"

DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1"
#: Default conversational model: multimodal, documents and screenshots.
DEFAULT_PRIMARY_MODEL = "nvidia/Nemotron-3_5-Super-VL"
#: Cheap model for titles, summaries, compaction, and other background work.
DEFAULT_BACKGROUND_MODEL = "nvidia/Nemotron-3_5-Lightning"


class Provider(str, Enum):
    """A named, OpenAI-compatible model provider."""

    NEBIUS = "nebius"
    OPENROUTER = "openrouter"


@dataclass(frozen=True)
class ProviderDefaults:
    """Endpoint, credential env var, and default models for one provider."""

    label: str
    base_url: str
    api_key_env: str
    primary_model: str
    background_model: str


#: Selects the provider. Defaults to Nebius Token Factory, the endpoint this
#: repository ships against. The provider's base URL and key stay server
#: config: neither is ever echoed to a client.
PROVIDER_ENV = "KELP_PROVIDER"
#: Provider-neutral base-URL override. The Nebius-specific envs below stay
#: honoured for the default provider so existing deployments do not move.
PROVIDER_BASE_URL_ENV = "KELP_PROVIDER_BASE_URL"

#: Every provider the server can be pointed at. An unknown ``KELP_PROVIDER`` is
#: a startup error rather than a silent fall back to Nebius.
#:
#: The OpenRouter slugs are defaults chosen to work out of the box; set
#: ``KELP_PRIMARY_MODEL`` / ``KELP_BACKGROUND_MODEL`` to the models you want.
#: No pricing rows ship for them, so accounting reports their cost as unknown
#: rather than a guessed number until entries are added to ``DEFAULT_PRICING``.
PROVIDERS: Mapping[str, ProviderDefaults] = {
    Provider.NEBIUS.value: ProviderDefaults(
        label="Nebius Token Factory",
        base_url=DEFAULT_BASE_URL,
        api_key_env=API_KEY_ENV,
        primary_model=DEFAULT_PRIMARY_MODEL,
        background_model=DEFAULT_BACKGROUND_MODEL,
    ),
    Provider.OPENROUTER.value: ProviderDefaults(
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        primary_model="openai/gpt-4o-mini",
        background_model="openai/gpt-4o-mini",
    ),
}


class ModelRole(str, Enum):
    """Which configured model a caller wants."""

    PRIMARY = "primary"
    BACKGROUND = "background"


@dataclass(frozen=True)
class ModelPricing:
    """USD per one million tokens (Token Factory list price)."""

    input_per_million: float
    output_per_million: float

    def estimate(self, input_tokens: int, output_tokens: int) -> float:
        return round(
            (input_tokens / 1_000_000) * self.input_per_million
            + (output_tokens / 1_000_000) * self.output_per_million,
            8,
        )


#: List prices as published on the Token Factory model page (2026-10-28).
#: Unknown models intentionally have no entry: accounting reports cost as
#: unknown rather than silently recording $0.
DEFAULT_PRICING: Mapping[str, ModelPricing] = {
    "nvidia/Nemotron-3_5-Super-VL": ModelPricing(0.30, 0.90),
    "nvidia/Nemotron-3_5-Lightning": ModelPricing(0.06, 0.24),
    "nvidia/nemotron-3-super-120b-a12b": ModelPricing(0.30, 0.90),
    "nvidia/Nemotron-3-Ultra-550b-a55b": ModelPricing(1.00, 3.00),
}


@dataclass(frozen=True)
class KernelConfig:
    base_url: str = DEFAULT_BASE_URL
    primary_model: str = DEFAULT_PRIMARY_MODEL
    background_model: str = DEFAULT_BACKGROUND_MODEL
    provider: str = Provider.NEBIUS.value
    provider_label: str = "Nebius Token Factory"
    api_key_env: str = API_KEY_ENV
    api_key: str | None = field(default=None, repr=False)
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 90.0
    request_write_timeout_s: float = 15.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "KernelConfig":
        source = os.environ if env is None else env
        provider = (source.get(PROVIDER_ENV) or Provider.NEBIUS.value).strip().lower()
        defaults = PROVIDERS.get(provider)
        if defaults is None:
            raise ValueError(
                f"unknown provider {provider!r}; expected one of {sorted(PROVIDERS)}"
            )

        # A provider-neutral override wins everywhere. The Nebius-specific envs
        # apply only to Nebius, so pointing KELP_PROVIDER at another provider is
        # not silently overridden by a leftover NEBIUS_* value.
        base_url = source.get(PROVIDER_BASE_URL_ENV)
        if not base_url and provider == Provider.NEBIUS.value:
            base_url = source.get(BASE_URL_ENV) or source.get(LEGACY_BASE_URL_ENV)

        return cls(
            base_url=(base_url or defaults.base_url).rstrip("/"),
            primary_model=source.get(PRIMARY_MODEL_ENV) or defaults.primary_model,
            background_model=source.get(BACKGROUND_MODEL_ENV)
            or defaults.background_model,
            provider=provider,
            provider_label=defaults.label,
            api_key_env=defaults.api_key_env,
            api_key=source.get(defaults.api_key_env) or None,
        )

    @property
    def api_key_configured(self) -> bool:
        return bool(self.api_key)

    def model_for(self, role: ModelRole | str) -> str:
        try:
            resolved = ModelRole(role)
        except ValueError:
            raise ValueError(f"unknown model role {role!r}") from None
        return self.primary_model if resolved is ModelRole.PRIMARY else self.background_model

    def __repr__(self) -> str:
        return (
            "KernelConfig("
            f"provider={self.provider!r}, "
            f"base_url={self.base_url!r}, "
            f"primary_model={self.primary_model!r}, "
            f"background_model={self.background_model!r}, "
            f"api_key_configured={self.api_key_configured})"
        )

    __str__ = __repr__


def pricing_for(model: str) -> ModelPricing | None:
    return DEFAULT_PRICING.get(model)


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    pricing = pricing_for(model)
    if pricing is None:
        return None
    return pricing.estimate(input_tokens, output_tokens)

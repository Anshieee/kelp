"""Kernel configuration: model provider endpoint, model roles, and pricing.

The provider endpoint has no default. This process sends the whole transcript to
whatever OpenAI-compatible service the operator names, so the endpoint is a
required, explicit choice rather than a baked-in third party: a missing
``KELP_PROVIDER_BASE_URL`` is a typed failure, never a silent call somewhere
else.

Secrets are read from the environment at construction time and are never echoed.
``KernelConfig.from_env`` is deliberately tolerant: a missing API key does not
crash startup, it produces a typed ``missing_api_key`` event on first use (the
server entry point does refuse to start without one).
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
    "DEFAULT_PRIMARY_MODEL",
    "DEFAULT_PRICING",
    "KernelConfig",
    "ModelPricing",
    "ModelRole",
    "PRIMARY_MODEL_ENV",
]

BASE_URL_ENV = "KELP_PROVIDER_BASE_URL"
API_KEY_ENV = "KELP_PROVIDER_API_KEY"
PRIMARY_MODEL_ENV = "KELP_PRIMARY_MODEL"
BACKGROUND_MODEL_ENV = "KELP_BACKGROUND_MODEL"

#: Default conversational model: multimodal, documents and screenshots.
#: The slugs are OpenRouter's, the endpoint this repository documents.
DEFAULT_PRIMARY_MODEL = "nvidia/nemotron-3.5-super-vl"
#: Cheap model for titles, summaries, compaction, and other background work.
DEFAULT_BACKGROUND_MODEL = "nvidia/nemotron-3.5-lightning"


class ModelRole(str, Enum):
    """Which configured model a caller wants."""

    PRIMARY = "primary"
    BACKGROUND = "background"


@dataclass(frozen=True)
class ModelPricing:
    """USD per one million tokens for one model at one provider."""

    input_per_million: float
    output_per_million: float

    def estimate(self, input_tokens: int, output_tokens: int) -> float:
        return round(
            (input_tokens / 1_000_000) * self.input_per_million
            + (output_tokens / 1_000_000) * self.output_per_million,
            8,
        )


#: Deliberately empty. The endpoint is operator-chosen, so list prices are
#: provider-specific and cannot be shipped as a repo-wide default. An unknown
#: model reports cost as unknown rather than silently recording $0, and the
#: spend guard treats an unmeasurable window as blocked unless the operator
#: sets ``KELP_SPEND_UNKNOWN_PRICE_MODE=count_as`` with a fixed amount.
DEFAULT_PRICING: Mapping[str, ModelPricing] = {}


@dataclass(frozen=True)
class KernelConfig:
    base_url: str | None = None
    primary_model: str = DEFAULT_PRIMARY_MODEL
    background_model: str = DEFAULT_BACKGROUND_MODEL
    api_key: str | None = field(default=None, repr=False)
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 90.0
    request_write_timeout_s: float = 15.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "KernelConfig":
        source = os.environ if env is None else env
        base_url = (source.get(BASE_URL_ENV) or "").strip().rstrip("/") or None
        key = source.get(API_KEY_ENV) or None
        return cls(
            base_url=base_url,
            primary_model=source.get(PRIMARY_MODEL_ENV) or DEFAULT_PRIMARY_MODEL,
            background_model=source.get(BACKGROUND_MODEL_ENV) or DEFAULT_BACKGROUND_MODEL,
            api_key=key,
        )

    @property
    def api_key_configured(self) -> bool:
        return bool(self.api_key)

    @property
    def base_url_configured(self) -> bool:
        return bool(self.base_url)

    def model_for(self, role: ModelRole | str) -> str:
        try:
            resolved = ModelRole(role)
        except ValueError:
            raise ValueError(f"unknown model role {role!r}") from None
        return self.primary_model if resolved is ModelRole.PRIMARY else self.background_model

    def __repr__(self) -> str:
        return (
            "KernelConfig("
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

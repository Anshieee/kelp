"""Kernel configuration: model provider endpoint, model roles, and pricing.

The provider endpoint has no default. This process sends the whole transcript to
whatever OpenAI-compatible service the operator names, so the endpoint is a
required, explicit choice rather than a baked-in third party: a missing
``KELP_PROVIDER_BASE_URL`` is a typed failure, never a silent call somewhere
else.

``KELP_PROVIDER`` names the provider for records and admin rows. This build
speaks the OpenAI-compatible API, so the default is ``openai-compatible``; a
second provider is configured by changing this value and the endpoint, not by
forking the code. Vendor model ids live only in
:mod:`kernel.providers.presets`, as documented examples.

Secrets are read from the environment at construction time and are never echoed.
``KernelConfig.from_env`` is deliberately tolerant: a missing API key does not
crash startup, it produces a typed ``missing_api_key`` event on first use (the
server entry point does refuse to start without one).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from .secrets import TENANT_SECRETS_FILE_ENV

__all__ = [
    "API_KEY_ENV",
    "BACKGROUND_MODEL_ENV",
    "BASE_URL_ENV",
    "DEFAULT_BACKGROUND_MODEL",
    "DEFAULT_PRIMARY_MODEL",
    "DEFAULT_PRICING",
    "DEFAULT_PROVIDER",
    "DEFAULT_TENANT_ID",
    "KernelConfig",
    "LEGACY_ENV_ALIASES",
    "ModelPricing",
    "ModelRole",
    "PRICING_JSON_ENV",
    "PRIMARY_MODEL_ENV",
    "PROVIDER_ENV",
    "TENANT_SECRETS_FILE_ENV",
]

PROVIDER_ENV = "KELP_PROVIDER"
BASE_URL_ENV = "KELP_PROVIDER_BASE_URL"
API_KEY_ENV = "KELP_PROVIDER_API_KEY"
PRIMARY_MODEL_ENV = "KELP_PRIMARY_MODEL"
BACKGROUND_MODEL_ENV = "KELP_BACKGROUND_MODEL"
PRICING_JSON_ENV = "KELP_PRICING_JSON"

#: Provider id written to records and admin rows. The wire protocol is the one
#: this build implements; the name is configuration, not a code path.
DEFAULT_PROVIDER = "openai-compatible"

#: The only tenant in this version. Kept here so a provider built from the
#: service config carries the same binding the provider seam expects.
DEFAULT_TENANT_ID = "owner"

#: Explicit compatibility aliases for deployments that used the earlier
#: Nebius-named environment. The canonical ``KELP_*`` name always wins; an alias
#: is consulted only when the canonical value is unset or blank. They are never
#: defaults: with no provider configuration, the endpoint stays unset.
LEGACY_ENV_ALIASES: Mapping[str, tuple[str, ...]] = {
    BASE_URL_ENV: ("NEBIUS_BASE_URL", "NEBIUS_API_BASE_URL"),
    API_KEY_ENV: ("NEBIUS_API_KEY",),
    PRIMARY_MODEL_ENV: ("NEBIUS_PRIMARY_MODEL",),
    BACKGROUND_MODEL_ENV: ("NEBIUS_BACKGROUND_MODEL",),
}

#: Filled from :mod:`kernel.providers.presets` at the bottom of this module.
#: They remain module constants so existing imports keep working; the values
#: are examples, not a vendor requirement.
DEFAULT_PRIMARY_MODEL = ""
DEFAULT_BACKGROUND_MODEL = ""


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


#: USD per one million tokens. Populated at import time from the documented
#: examples in :mod:`kernel.providers.presets`; ``KELP_PRICING_JSON`` adds or
#: replaces rows on the configuration built by ``from_env``. Only models listed
#: here have a known price: a model with no entry stays unknown rather than
#: becoming a fabricated number.
DEFAULT_PRICING: dict[str, ModelPricing] = {}


def _text(source: Mapping[str, str], name: str) -> str:
    """The canonical value if non-blank, else the first non-blank alias."""

    raw = source.get(name)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    for alias in LEGACY_ENV_ALIASES.get(name, ()):
        candidate = source.get(alias)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _secret(source: Mapping[str, str], name: str) -> str | None:
    """Read a key without trimming its value; whitespace-only means unset."""

    raw = source.get(name)
    if isinstance(raw, str) and raw.strip():
        return raw
    for alias in LEGACY_ENV_ALIASES.get(name, ()):
        candidate = source.get(alias)
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return None


def _pricing_table(raw: str) -> dict[str, ModelPricing]:
    """The built-in example table plus an optional JSON override.

    The JSON value is an object of model id to ``input_per_million`` and
    ``output_per_million`` (USD per one million tokens). A malformed table is a
    configuration error, never a silently different price.
    """

    table = dict(DEFAULT_PRICING)
    if not raw:
        return table
    try:
        payload = json.loads(raw)
    except ValueError:
        raise ValueError(f"{PRICING_JSON_ENV} must be valid JSON.") from None
    if not isinstance(payload, dict):
        raise ValueError(f"{PRICING_JSON_ENV} must be a JSON object.")
    for model, rates in payload.items():
        if not isinstance(model, str) or not model.strip():
            raise ValueError(
                f"{PRICING_JSON_ENV} keys must be non-empty model ids."
            )
        if not isinstance(rates, dict):
            raise ValueError(
                f"{PRICING_JSON_ENV} entry {model!r} must be an object with "
                "input_per_million and output_per_million."
            )
        try:
            input_rate = float(rates["input_per_million"])
            output_rate = float(rates["output_per_million"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(
                f"{PRICING_JSON_ENV} entry {model!r} must have numeric "
                "input_per_million and output_per_million values."
            ) from None
        if input_rate < 0 or output_rate < 0:
            raise ValueError(
                f"{PRICING_JSON_ENV} entry {model!r} cannot be negative."
            )
        table[model.strip()] = ModelPricing(input_rate, output_rate)
    return table


@dataclass(frozen=True)
class KernelConfig:
    provider: str = DEFAULT_PROVIDER
    base_url: str | None = None
    primary_model: str = field(default_factory=lambda: DEFAULT_PRIMARY_MODEL)
    background_model: str = field(default_factory=lambda: DEFAULT_BACKGROUND_MODEL)
    api_key: str | None = field(default=None, repr=False)
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 90.0
    request_write_timeout_s: float = 15.0
    tenant_id: str = DEFAULT_TENANT_ID
    tenant_secrets_file: str | None = field(default=None, repr=False)
    pricing: Mapping[str, ModelPricing] = field(
        default_factory=lambda: DEFAULT_PRICING, repr=False
    )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "KernelConfig":
        source = os.environ if env is None else env
        base_url = _text(source, BASE_URL_ENV).rstrip("/") or None
        return cls(
            provider=_text(source, PROVIDER_ENV) or DEFAULT_PROVIDER,
            base_url=base_url,
            primary_model=_text(source, PRIMARY_MODEL_ENV) or DEFAULT_PRIMARY_MODEL,
            background_model=(
                _text(source, BACKGROUND_MODEL_ENV) or DEFAULT_BACKGROUND_MODEL
            ),
            api_key=_secret(source, API_KEY_ENV),
            tenant_secrets_file=_text(source, TENANT_SECRETS_FILE_ENV) or None,
            pricing=_pricing_table(_text(source, PRICING_JSON_ENV)),
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

    def pricing_for(self, model: str) -> ModelPricing | None:
        """This configuration's price for ``model``, if it has one."""

        return pricing_for(model, self.pricing)

    def estimate_cost(
        self, model: str, input_tokens: int, output_tokens: int
    ) -> float | None:
        """This configuration's estimated cost, or ``None`` when unpriced."""

        return estimate_cost(model, input_tokens, output_tokens, self.pricing)

    def __repr__(self) -> str:
        return (
            "KernelConfig("
            f"provider={self.provider!r}, "
            f"base_url={self.base_url!r}, "
            f"primary_model={self.primary_model!r}, "
            f"background_model={self.background_model!r}, "
            f"tenant_secrets_file_configured={self.tenant_secrets_file is not None}, "
            f"api_key_configured={self.api_key_configured})"
        )

    __str__ = __repr__


def pricing_for(
    model: str, pricing: Mapping[str, ModelPricing] | None = None
) -> ModelPricing | None:
    table = DEFAULT_PRICING if pricing is None else pricing
    return table.get(model)


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    pricing: Mapping[str, ModelPricing] | None = None,
) -> float | None:
    row = pricing_for(model, pricing)
    if row is None:
        return None
    return row.estimate(input_tokens, output_tokens)


def _load_example_defaults() -> None:
    """Take the documented example model ids and prices from ``presets``.

    Imported lazily at module scope's end so ``kernel.providers`` may import this
    module while its package initialises, and so the vendor ids stay in one
    documented place rather than scattered through runtime configuration.
    """

    from .providers.presets import (
        EXAMPLE_BACKGROUND_MODEL,
        EXAMPLE_PRICING_USD_PER_MILLION,
        EXAMPLE_PRIMARY_MODEL,
    )

    global DEFAULT_PRIMARY_MODEL, DEFAULT_BACKGROUND_MODEL
    DEFAULT_PRIMARY_MODEL = EXAMPLE_PRIMARY_MODEL
    DEFAULT_BACKGROUND_MODEL = EXAMPLE_BACKGROUND_MODEL
    DEFAULT_PRICING.update(
        {
            model: ModelPricing(input_rate, output_rate)
            for model, (input_rate, output_rate) in (
                EXAMPLE_PRICING_USD_PER_MILLION.items()
            )
        }
    )


_load_example_defaults()

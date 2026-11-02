"""Provider selection for the model endpoint (``KELP_PROVIDER``).

OpenRouter is OpenAI-compatible, so only the base URL, the credential env var,
and the default models change. These tests pin the precedence: a leftover
Nebius-specific value must not silently override another provider, and an
unknown provider must fail rather than fall back.
"""

from __future__ import annotations

import pytest

from chat.server import ConfigError, ServerConfig
from kernel.config import (
    DEFAULT_BASE_URL,
    DEFAULT_PRIMARY_MODEL,
    PROVIDERS,
    KernelConfig,
    Provider,
)


def test_default_provider_is_nebius_token_factory() -> None:
    config = KernelConfig.from_env({})
    assert config.provider == Provider.NEBIUS.value
    assert config.base_url == DEFAULT_BASE_URL
    assert config.api_key_env == "NEBIUS_API_KEY"
    assert config.primary_model == DEFAULT_PRIMARY_MODEL


def test_openrouter_selects_its_endpoint_key_and_models() -> None:
    config = KernelConfig.from_env(
        {"KELP_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "sk-or-x"}
    )
    defaults = PROVIDERS[Provider.OPENROUTER.value]
    assert config.base_url == "https://openrouter.ai/api/v1"
    assert config.api_key == "sk-or-x"
    assert config.api_key_env == "OPENROUTER_API_KEY"
    assert config.provider_label == "OpenRouter"
    assert config.primary_model == defaults.primary_model
    assert config.background_model == defaults.background_model


def test_nebius_specific_base_url_cannot_override_another_provider() -> None:
    config = KernelConfig.from_env(
        {
            "KELP_PROVIDER": "openrouter",
            "NEBIUS_TOKEN_FACTORY_BASE_URL": "http://127.0.0.1:9/v1",
        }
    )
    assert config.base_url == "https://openrouter.ai/api/v1"


def test_provider_neutral_base_url_wins_for_any_provider() -> None:
    config = KernelConfig.from_env(
        {
            "KELP_PROVIDER": "openrouter",
            "KELP_PROVIDER_BASE_URL": "https://gateway.test/v1/",
        }
    )
    assert config.base_url == "https://gateway.test/v1"


def test_legacy_nebius_base_url_is_still_honoured() -> None:
    config = KernelConfig.from_env({"NEBIUS_BASE_URL": "http://127.0.0.1:9/v1"})
    assert config.base_url == "http://127.0.0.1:9/v1"


def test_model_env_overrides_the_provider_default() -> None:
    config = KernelConfig.from_env(
        {
            "KELP_PROVIDER": "openrouter",
            "KELP_PRIMARY_MODEL": "anthropic/claude-3.5-sonnet",
        }
    )
    assert config.primary_model == "anthropic/claude-3.5-sonnet"


def test_unknown_provider_is_an_error_not_a_fallback() -> None:
    with pytest.raises(ValueError, match="unknown provider"):
        KernelConfig.from_env({"KELP_PROVIDER": "openai"})


def test_a_missing_key_is_tolerated_at_config_level() -> None:
    config = KernelConfig.from_env({"KELP_PROVIDER": "openrouter"})
    assert config.api_key is None
    assert config.api_key_configured is False


def test_repr_names_the_provider_and_never_the_key() -> None:
    config = KernelConfig.from_env(
        {"KELP_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "sk-or-secret-value"}
    )
    assert "openrouter" in repr(config)
    assert "sk-or-secret-value" not in repr(config)


def test_boot_gate_requires_the_selected_providers_key() -> None:
    with pytest.raises(ConfigError) as openrouter:
        ServerConfig.from_env({"KELP_PROVIDER": "openrouter", "KELP_AUTH_TOKEN": "t"})
    assert "OPENROUTER_API_KEY" in str(openrouter.value)

    with pytest.raises(ConfigError) as nebius:
        ServerConfig.from_env({"KELP_AUTH_TOKEN": "t"})
    assert "NEBIUS_API_KEY" in str(nebius.value)


def test_boot_gate_accepts_the_openrouter_key() -> None:
    config = ServerConfig.from_env(
        {
            "KELP_PROVIDER": "openrouter",
            "OPENROUTER_API_KEY": "sk-or-x",
            "KELP_AUTH_TOKEN": "t",
        }
    )
    assert config.kernel.api_key == "sk-or-x"

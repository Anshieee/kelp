"""Stripe wallet placeholder — configuration only, no network calls yet.

The account owner will supply a test key. Until then this module exists to pin
the contract: the secret key is read from the server environment, never
hardcoded, never sent to the browser, and never included in ``repr``, status
payloads, or logs.

When the key is available, add the Stripe SDK behind ``StripeWallet`` and keep
the same rule: values stay server-side.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping

__all__ = [
    "STRIPE_SECRET_KEY_ENV",
    "StripeConfig",
    "StripeWallet",
    "WalletNotConfigured",
]

STRIPE_SECRET_KEY_ENV = "STRIPE_SECRET_KEY"


class WalletNotConfigured(RuntimeError):
    """Raised when wallet work is attempted before the test key is provided."""

    def __init__(self) -> None:
        super().__init__(
            "Stripe is not configured. Set STRIPE_SECRET_KEY on the server "
            "(environment or secret manager); never put it in client code."
        )


@dataclass(frozen=True)
class StripeConfig:
    secret_key: str = field(repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "StripeConfig":
        source = os.environ if env is None else env
        key = (source.get(STRIPE_SECRET_KEY_ENV) or "").strip()
        if not key:
            raise WalletNotConfigured()
        return cls(key)

    @property
    def mode(self) -> str:
        if self.secret_key.startswith("sk_test_"):
            return "test"
        if self.secret_key.startswith("sk_live_"):
            return "live"
        return "unknown"

    def __repr__(self) -> str:
        return f"StripeConfig(mode={self.mode!r}, secret_key=<redacted>)"

    __str__ = __repr__


class StripeWallet:
    """Adapter boundary for saved cards and payments. Not implemented yet."""

    provider = "stripe"

    def __init__(self, config: StripeConfig):
        self._config = config

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "StripeWallet":
        return cls(StripeConfig.from_env(env))

    def status(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "configured": True,
            "mode": self._config.mode,
            "saved_cards": "not_implemented",
            "network_calls": False,
        }

    def __repr__(self) -> str:
        return f"StripeWallet(mode={self._config.mode!r}, configured=True)"

    __str__ = __repr__

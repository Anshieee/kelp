"""Stripe wallet placeholder tests (KEL-7): env only, no hardcoded keys."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from payments import STRIPE_SECRET_KEY_ENV, StripeConfig, StripeWallet, WalletNotConfigured

TEST_KEY = "sk_test_abc123"
SOURCE_ROOT = Path(__file__).resolve().parents[1]
_KEY_LITERAL = re.compile(r"sk_(?:live|test)_[A-Za-z0-9]{8,}")


def test_missing_key_is_not_configured() -> None:
    with pytest.raises(WalletNotConfigured):
        StripeConfig.from_env({})
    with pytest.raises(WalletNotConfigured):
        StripeConfig.from_env({STRIPE_SECRET_KEY_ENV: "   "})


def test_configured_wallet_never_exposes_the_key() -> None:
    config = StripeConfig.from_env({STRIPE_SECRET_KEY_ENV: TEST_KEY})
    wallet = StripeWallet(config)

    assert config.mode == "test"
    assert TEST_KEY not in repr(config)
    assert TEST_KEY not in str(config)
    assert TEST_KEY not in repr(wallet)
    status = wallet.status()
    assert status["configured"] is True
    assert status["mode"] == "test"
    assert status["network_calls"] is False
    assert TEST_KEY not in str(status)


@pytest.mark.parametrize(
    ("key", "mode"),
    [("sk_live_abc123", "live"), ("pk_test_abc123", "unknown"), ("something", "unknown")],
)
def test_mode_detection(key: str, mode: str) -> None:
    assert StripeConfig(key).mode == mode


def test_import_does_not_require_a_key() -> None:
    # ``payments.stripe_wallet`` is already imported by this test module, so an
    # in-process ``import_module`` only returns the cached module and proves
    # nothing. A fresh interpreter with the variable removed runs the module
    # body for real, so a key read at import time would fail here.
    env = {
        name: value
        for name, value in os.environ.items()
        if name != STRIPE_SECRET_KEY_ENV
    }
    env["PYTHONPATH"] = str(SOURCE_ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    assert STRIPE_SECRET_KEY_ENV not in env
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import payments, payments.stripe_wallet as m;"
            "print(m.StripeWallet.__name__, m.STRIPE_SECRET_KEY_ENV)",
        ],
        cwd=SOURCE_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["StripeWallet", STRIPE_SECRET_KEY_ENV]


def test_no_hardcoded_stripe_keys_in_source() -> None:
    offenders = []
    for path in SOURCE_ROOT.glob("payments/**/*.py"):
        if _KEY_LITERAL.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path))
    assert offenders == []


def test_env_example_has_empty_value() -> None:
    example = SOURCE_ROOT / "payments" / ".env.example"
    lines = [
        line.strip()
        for line in example.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith(STRIPE_SECRET_KEY_ENV + "=")
    ]
    assert lines == [STRIPE_SECRET_KEY_ENV + "="]

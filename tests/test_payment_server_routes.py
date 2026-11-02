"""Production ChatAPI routing for wallet and hosted Checkout."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from chat.api import Request
from chat.server import ServerConfig, build_app


def _env(root: Path, **overrides: str) -> dict[str, str]:
    values = {
        "KELP_PROVIDER_API_KEY": "test-provider-key-1234567890",
        "KELP_PROVIDER_BASE_URL": "https://openrouter.ai/api/v1",
        "KELP_AUTH_TOKEN": "owner-token-1234567890",
        "KELP_CSRF_TOKEN": "csrf-token-1234567890",
        "KELP_DB_PATH": str(root / "db" / "chat.db"),
        "KELP_ARTIFACT_DIR": str(root / "artifacts"),
        "KELP_AUDIT_DIR": str(root / "audit"),
        "KELP_USAGE_DIR": str(root / "usage"),
        "KELP_WORKSPACE_ROOT": str(root / "workspace"),
        "KELP_PORT": "0",
    }
    values.update(overrides)
    return values


def _call(app: Any, method: str, path: str, *, headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    response = app.api.handle(
        Request(method=method, path=path, headers=headers or {})
    )
    return response.status, json.loads(response.body)


def test_payment_routes_use_chat_auth_and_csrf_and_fail_closed(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    app = build_app(ServerConfig.from_env(env), environ=env)
    try:
        status, body = _call(app, "GET", "/api/wallet")
        assert status == 401
        assert body["error"]["code"] == "unauthorized"

        owner = {"authorization": f"Bearer {env['KELP_AUTH_TOKEN']}"}
        status, body = _call(app, "GET", "/api/wallet", headers=owner)
        assert status == 503
        assert body["error"]["code"] == "wallet_not_configured"

        status, body = _call(
            app, "POST", "/api/wallet/checkout", headers=owner
        )
        assert status == 403
        assert body["error"]["code"] == "csrf_failed"

        owner_csrf = {**owner, "x-csrf-token": env["KELP_CSRF_TOKEN"]}
        status, body = _call(
            app, "POST", "/api/wallet/checkout", headers=owner_csrf
        )
        assert status == 503
        assert body["error"]["code"] == "wallet_not_configured"

        status, body = _call(app, "POST", "/api/wallet/checkout/webhook")
        assert status == 503
        assert body["error"]["code"] == "wallet_not_configured"

        assert app.wallet_api is not None
        assert app.checkout_api is not None
        assert app.wallet_api.wallet._gateway is None
        assert app.checkout_api.checkout.wallet._gateway is None
        assert app.checkout_api.checkout.wallet._razorpay_transport is None
    finally:
        app.close()


def test_checkout_is_mounted_separately_when_test_keys_are_configured(
    tmp_path: Path,
) -> None:
    env = _env(
        tmp_path,
        RAZORPAY_KEY_ID="rzp_test_abcd1234",
        RAZORPAY_KEY_SECRET="test-secret-value-1234567890",
    )
    app = build_app(ServerConfig.from_env(env), environ=env)
    try:
        owner = {"authorization": f"Bearer {env['KELP_AUTH_TOKEN']}"}
        status, body = _call(
            app, "GET", "/api/wallet/checkout", headers=owner
        )
        assert status == 200
        assert body["checkout"]["configured"] is True
        assert body["checkout"]["mode"] == "test"
        assert app.checkout_api is not None
        assert app.checkout_api.checkout.wallet._razorpay_transport is None
    finally:
        app.close()

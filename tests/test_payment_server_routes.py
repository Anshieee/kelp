"""Production ChatAPI routing for wallet and hosted Checkout."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from chat.api import Request
from chat.logging import configure_logging, log
from chat.server import ServerConfig, build_app
from openshell.egress_ledger import EgressLedger
from payments.razorpay_gateway import HttpRazorpayTransport


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


def test_razorpay_credentials_are_scrubbed_across_server_surfaces(
    tmp_path: Path,
) -> None:
    credentials = {
        "RAZORPAY_KEY_ID": "rzp_test_walletscrub1234",
        "RAZORPAY_KEY_SECRET": "wallet-checkout-secret-123456",
        "RAZORPAY_WEBHOOK_SECRET": "wallet-webhook-secret-123456",
    }
    unconfigured_env = _env(tmp_path / "unconfigured")
    unconfigured_app = build_app(
        ServerConfig.from_env(unconfigured_env), environ=unconfigured_env
    )
    env = _env(tmp_path / "configured", **credentials)
    app = build_app(ServerConfig.from_env(env), environ=env)
    errors: list[str] = []
    # build_app installed the process-wide scrubber from the service secrets.
    # Redirect only the destination into a buffer this test owns, and leave
    # ``secret_values`` unset so the scrubber under test stays the one build_app
    # configured (passing the credentials here would make the check vacuous).
    log_buffer = io.StringIO()
    configure_logging(level="debug", stream=log_buffer)
    try:
        owner = {"authorization": f"Bearer {env['KELP_AUTH_TOKEN']}"}
        owner_csrf = {**owner, "x-csrf-token": env["KELP_CSRF_TOKEN"]}

        # Exercise both configured payment routes and a client error. The
        # invalid amount is rejected before any provider request is made.
        for method, path, headers, body, expected_status in (
            ("GET", "/api/wallet", owner, b"", 200),
            ("GET", "/api/wallet/checkout", owner, b"", 200),
            (
                "POST",
                "/api/wallet/checkout",
                owner_csrf,
                json.dumps({"amount_cents": credentials["RAZORPAY_KEY_SECRET"]}).encode(),
                400,
            ),
        ):
            response = app.api.handle(
                Request(method=method, path=path, headers=headers, body=body)
            )
            assert response.status == expected_status
            if response.status >= 400:
                errors.append(response.body.decode())

        # An unconfigured deployment fails closed on both wallet and hosted
        # Checkout, including the separate webhook route.
        unconfigured_owner = {
            "authorization": f"Bearer {unconfigured_env['KELP_AUTH_TOKEN']}"
        }
        unconfigured_csrf = {
            **unconfigured_owner,
            "x-csrf-token": unconfigured_env["KELP_CSRF_TOKEN"],
        }
        for method, path, headers in (
            ("GET", "/api/wallet", unconfigured_owner),
            ("POST", "/api/wallet/checkout", unconfigured_csrf),
            ("POST", "/api/wallet/checkout/webhook", {}),
        ):
            response = unconfigured_app.api.handle(
                Request(method=method, path=path, headers=headers)
            )
            assert response.status == 503
            errors.append(response.body.decode())

        # Put each credential through the actual app-level log, metrics, and
        # audit scrubbers. This makes the regression fail if build_app stops
        # sharing payment credential values with any of those surfaces.
        for value in credentials.values():
            log("payment_scrubber_probe", error_code=value)
            app.api.metrics.observe_http("GET", value, 200, 0.001)
            app.audit.record("payment_scrubber_probe", extra={"value": value})

        metrics_response = app.api.handle(
            Request("GET", "/api/metrics", headers=owner)
        )
        assert metrics_response.status == 200
        metrics_output = metrics_response.body.decode()
        audit_output = app.config.audit_path.read_text(encoding="utf-8")
        log_output = log_buffer.getvalue()

        # Positive control: every probe reached the sink, so the absence checks
        # below can fail. Each probe must carry the redaction marker instead of
        # the value it was given.
        records = [json.loads(line) for line in log_output.splitlines() if line.strip()]
        probes = [r for r in records if r["event"] == "payment_scrubber_probe"]
        assert len(probes) == len(credentials)
        assert all(probe["error_code"] == "[redacted]" for probe in probes)
        assert any(r["event"] == "http_request" for r in records)

        for secret in credentials.values():
            assert secret not in log_output
            assert secret not in metrics_output
            assert secret not in audit_output
            assert all(secret not in body for body in errors)
    finally:
        configure_logging(level="info", secret_values=(), stream=None, enabled=False)
        app.close()
        unconfigured_app.close()


# -- egress: the running server's Razorpay calls are accounted for --------------

_KEYS = {
    "RAZORPAY_KEY_ID": "rzp_test_egressprobe1234",
    "RAZORPAY_KEY_SECRET": "-".join(("egress", "probe", "secret", "1234567890")),
}


def _patch_httpx_client(
    monkeypatch: pytest.MonkeyPatch, handler: Any
) -> list[httpx.Request]:
    """Make every ``httpx.Client`` the payment transport builds offline."""

    seen: list[httpx.Request] = []
    real_client = httpx.Client

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def factory(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(recording)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)
    return seen


def _owner(env: dict[str, str]) -> dict[str, str]:
    return {
        "authorization": f"Bearer {env['KELP_AUTH_TOKEN']}",
        "x-csrf-token": env["KELP_CSRF_TOKEN"],
    }


def test_build_app_threads_the_apps_egress_ledger_into_both_razorpay_gateways(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path, **_KEYS)
    app = build_app(ServerConfig.from_env(env), environ=env)
    try:
        assert app.wallet_api is not None
        assert app.checkout_api is not None
        for wallet in (app.wallet_api.wallet, app.checkout_api.checkout.wallet):
            gateway = wallet._transport()  # noqa: SLF001
            assert isinstance(gateway.egress, EgressLedger)
            # The same audit trail the model client and skills write into.
            assert gateway.egress.audit is app.audit
            # Still lazy: nothing HTTP-shaped exists until a payment call.
            assert gateway._transport is None  # noqa: SLF001
            assert wallet._razorpay_transport is None  # noqa: SLF001
    finally:
        app.close()


def test_with_both_keys_the_server_calls_razorpay_and_writes_one_egress_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path, **_KEYS)
    app = build_app(ServerConfig.from_env(env), environ=env)
    try:
        seen = _patch_httpx_client(
            monkeypatch,
            lambda request: httpx.Response(200, json={"id": "cust_egressprobe42"}),
        )

        status, body = _call(
            app, "POST", "/api/wallet/setup-intent", headers=_owner(env)
        )

        assert status == 200, body
        # The finding: the running server really does reach api.razorpay.com.
        assert [request.url.host for request in seen] == ["api.razorpay.com"]
        rows = [
            row
            for row in app.audit.entries(event="egress")
            if row["extra"]["recipient"] == "razorpay"
        ]
        assert len(rows) == 1
        extra = rows[0]["extra"]
        assert extra["purpose"] == "payments.razorpay.customer.create"
        assert extra["outcome"] == "completed"
        assert extra["status"] == 200
        assert extra["host"] == "api.razorpay.com"
        audit_text = app.config.audit_path.read_text(encoding="utf-8")
        for secret in (*_KEYS.values(), "cust_egressprobe42"):
            assert secret not in audit_text
    finally:
        app.close()


def test_with_both_keys_a_razorpay_rejection_is_one_provider_error_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path, **_KEYS)
    app = build_app(ServerConfig.from_env(env), environ=env)
    try:
        _patch_httpx_client(
            monkeypatch,
            lambda request: httpx.Response(
                401, json={"error": {"code": "BAD_REQUEST_ERROR"}}
            ),
        )

        status, _body = _call(
            app, "POST", "/api/wallet/setup-intent", headers=_owner(env)
        )

        assert status >= 400
        rows = [
            row["extra"]
            for row in app.audit.entries(event="egress")
            if row["extra"]["recipient"] == "razorpay"
        ]
        assert [(row["outcome"], row["status"]) for row in rows] == [
            ("provider_error", 401)
        ]
    finally:
        app.close()


def test_with_no_keys_no_transport_is_built_and_nothing_is_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(self: Any, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("an unconfigured build_app constructed a transport")

    monkeypatch.setattr(HttpRazorpayTransport, "__init__", refuse)
    env = _env(tmp_path)
    app = build_app(ServerConfig.from_env(env), environ=env)
    try:
        seen = _patch_httpx_client(
            monkeypatch, lambda request: httpx.Response(500, json={})
        )
        owner = _owner(env)
        for method, path in (
            ("GET", "/api/wallet"),
            ("POST", "/api/wallet/setup-intent"),
            ("POST", "/api/wallet/checkout"),
        ):
            status, body = _call(app, method, path, headers=owner)
            assert status == 503, (path, body)
            assert body["error"]["code"] == "wallet_not_configured"

        assert seen == []
        assert app.wallet_api is not None
        assert app.checkout_api is not None
        assert app.wallet_api.wallet._gateway is None  # noqa: SLF001
        assert app.checkout_api.checkout.wallet._gateway is None  # noqa: SLF001
        assert [
            row
            for row in app.audit.entries(event="egress")
            if row["extra"]["recipient"] == "razorpay"
        ] == []
    finally:
        app.close()

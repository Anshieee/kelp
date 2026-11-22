"""Constant-time string comparison and its non-ASCII behaviour at the HTTP edge."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from chat import ChatService, ChatStore
from chat.api import AuthFailureTracker, ChatAPI, RateLimiter, Request
from chat.checkout import CheckoutAPI
from chat.compare import constant_time_equals
from chat.tenancy import StaticTenantResolver, TenantMismatchError
from chat.wallet import WalletAPI

from .chatkit import MODEL, StubProvider, text_script
from .test_razorpay_checkout import CheckoutTransport, make_checkout
from .test_wallet import FakeGateway, make_wallet

CAFE = "caf\u00e9"


# -- unit -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("secret", "secret", True),
        ("secret", "Secret", False),
        ("secret", "secre", False),
        ("", "", True),
        ("", "x", False),
        (CAFE, CAFE, True),
        (CAFE, "cafe", False),
        ("\u2603\u00e9", "\u2603\u00e9", True),
        ("\ud800", "\ud800", True),  # lone surrogate must not raise
        ("\ud800", "a", False),
    ],
)
def test_constant_time_equals(left: str, right: str, expected: bool) -> None:
    assert constant_time_equals(left, right) is expected


# -- ChatAPI ----------------------------------------------------------------


def _api(tmp_path: Path, **kwargs: Any) -> tuple[ChatAPI, ChatStore]:
    store = ChatStore(tmp_path / "chat.db")
    service = ChatService(
        store=store, provider=StubProvider([text_script("hi")]), model=MODEL
    )
    options: dict[str, Any] = {"auth_token": "tok", "csrf_token": "csrf"}
    options.update(kwargs)
    api = ChatAPI(service, **options)
    return api, store


def _handle(
    api: ChatAPI, method: str, path: str, headers: dict[str, str], body: bytes = b""
) -> Any:
    return api.handle(Request(method, path, {}, headers, body))


def test_non_ascii_bearer_is_a_401_and_counts_as_a_failure(tmp_path: Path) -> None:
    api, store = _api(
        tmp_path,
        limiter=RateLimiter(
            {n: (1_000_000.0, 1_000_000.0) for n in ("auth", "chat", "upload", "read", "admin")}
        ),
        auth_failures=AuthFailureTracker(attempts=3, lockout_seconds=60.0),
    )
    try:
        bad = {"authorization": f"Bearer {CAFE}"}
        first = _handle(api, "GET", "/api/threads", bad)
        assert first.status == 401
        assert json.loads(first.body)["error"]["code"] == "unauthorized"
        assert _handle(api, "GET", "/api/threads", bad).status == 401
        # The third attempt trips the same lockout an ASCII bad token would.
        assert _handle(api, "GET", "/api/threads", bad).status == 429
    finally:
        api.close()
        store.close()


def test_non_ascii_csrf_header_is_a_403(tmp_path: Path) -> None:
    api, store = _api(tmp_path)
    try:
        response = _handle(
            api,
            "POST",
            "/api/threads",
            {
                "authorization": "Bearer tok",
                "x-csrf-token": CAFE,
                "content-type": "application/json",
            },
            b"{}",
        )
        assert response.status == 403
        assert json.loads(response.body)["error"]["code"] == "csrf_failed"
    finally:
        api.close()
        store.close()


def test_non_ascii_origin_is_not_same_origin(tmp_path: Path) -> None:
    api, store = _api(tmp_path, csrf_token=None)
    try:
        response = _handle(
            api,
            "POST",
            "/api/threads",
            {
                "authorization": "Bearer tok",
                "origin": f"https://{CAFE}.example",
                "host": "app.example",
                "content-type": "application/json",
            },
            b"{}",
        )
        assert response.status == 403
    finally:
        api.close()
        store.close()


def test_non_ascii_session_cookie_and_csrf_pair_are_refused(tmp_path: Path) -> None:
    api, store = _api(tmp_path)
    try:
        sign_in = _handle(api, "POST", "/api/auth/session", {"authorization": "Bearer tok"})
        assert sign_in.status == 201
        cookies = {c.split("=", 1)[0]: c.split(";", 1)[0].split("=", 1)[1] for c in sign_in.cookies}
        headers = {
            "cookie": f"session={cookies['session']}; csrf={CAFE}",
            "origin": "https://app.example",
            "host": "app.example",
            "x-csrf-token": CAFE,
            "content-type": "application/json",
        }
        response = _handle(api, "POST", "/api/threads", headers, b"{}")
        assert response.status == 403
        assert json.loads(response.body)["error"]["code"] == "csrf_failed"
    finally:
        api.close()
        store.close()


# -- tenancy / wallet / checkout --------------------------------------------


def test_static_resolver_treats_a_non_ascii_token_as_a_mismatch() -> None:
    resolver = StaticTenantResolver("root-token")
    with pytest.raises(TenantMismatchError):
        resolver.resolve(CAFE)
    assert StaticTenantResolver(CAFE).resolve(CAFE).tenant_id == "owner"


def test_wallet_non_ascii_bearer_is_401_and_non_ascii_csrf_is_403() -> None:
    api = WalletAPI(make_wallet(gateway=FakeGateway()), auth_token="tok", csrf_token="csrf")
    unauthorized = api.handle(
        Request("GET", "/api/wallet", {}, {"authorization": f"Bearer {CAFE}"}, b"")
    )
    assert unauthorized.status == 401
    cookie = api.handle(
        Request("GET", "/api/wallet", {}, {"cookie": f"session={CAFE}"}, b"")
    )
    assert cookie.status == 401
    forbidden = api.handle(
        Request(
            "POST",
            "/api/wallet/setup-intent",
            {},
            {"authorization": "Bearer tok", "x-csrf-token": CAFE},
            b"",
        )
    )
    assert forbidden.status == 403


def test_checkout_non_ascii_bearer_is_401_and_non_ascii_csrf_is_403() -> None:
    api = CheckoutAPI(
        make_checkout(transport=CheckoutTransport()),
        auth_token="tok",
        csrf_token="csrf",
    )
    unauthorized = api.handle(
        Request(
            "POST", "/api/wallet/checkout", {}, {"authorization": f"Bearer {CAFE}"}, b""
        )
    )
    assert unauthorized.status == 401
    cookie = api.handle(
        Request("POST", "/api/wallet/checkout", {}, {"cookie": f"session={CAFE}"}, b"")
    )
    assert cookie.status == 401
    forbidden = api.handle(
        Request(
            "POST",
            "/api/wallet/checkout",
            {},
            {"authorization": "Bearer tok", "x-csrf-token": CAFE},
            b"",
        )
    )
    assert forbidden.status == 403

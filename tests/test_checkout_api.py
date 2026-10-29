"""KEL-10 hosted Checkout HTTP surface tests. Fake transport, no network."""

from __future__ import annotations

import json
from typing import Any, Mapping

import pytest

from chat.api import Request, Response
from chat.checkout import CheckoutAPI
from payments.razorpay_checkout import (
    RazorpayCheckout,
    expected_payment_signature,
    expected_webhook_signature,
)
from payments.razorpay_gateway import RazorpayWallet
from tests.test_razorpay_checkout import (
    KEY_ID,
    TEST_SECRET,
    WEBHOOK_SECRET,
    CheckoutTransport,
    make_checkout,
    make_wallet,
    signature_for,
    webhook_event,
)

TOKEN = "test-session-token"
CSRF = "test-csrf-token"
PAN = "4242424242424242"


@pytest.fixture
def transport() -> CheckoutTransport:
    return CheckoutTransport()


@pytest.fixture
def api(transport: CheckoutTransport) -> CheckoutAPI:
    return CheckoutAPI(
        make_checkout(transport=transport), auth_token=TOKEN, csrf_token=CSRF
    )


def call(
    api: CheckoutAPI,
    method: str,
    path: str,
    *,
    body: Any = None,
    headers: Mapping[str, str] | None = None,
    auth: bool = True,
    csrf: bool = True,
) -> Response:
    request_headers = dict(headers or {})
    if auth:
        request_headers.setdefault("authorization", f"Bearer {TOKEN}")
    if csrf:
        request_headers.setdefault("x-csrf-token", CSRF)
    if body is None:
        raw = b""
    elif isinstance(body, bytes):
        raw = body
    else:
        raw = json.dumps(body).encode("utf-8")
        request_headers.setdefault("content-type", "application/json")
    return api.handle(
        Request(
            method=method,
            path=path,
            query={},
            headers=request_headers,
            body=raw,
        )
    )


def payload(response: Response) -> dict[str, Any]:
    return json.loads(response.body.decode("utf-8"))


def create(api: CheckoutAPI, **overrides: Any) -> dict[str, Any]:
    body = {"amount_cents": 1500, "description": "Coffee", "idempotency_key": "checkout-key-1"}
    body.update(overrides)
    response = call(api, "POST", "/api/wallet/checkout", body=body)
    assert response.status == 201
    return payload(response)


# -- auth ------------------------------------------------------------------


def test_create_requires_a_session(api: CheckoutAPI) -> None:
    response = call(
        api,
        "POST",
        "/api/wallet/checkout",
        body={"amount_cents": 1500},
        auth=False,
    )
    assert response.status == 401
    assert payload(response)["error"]["code"] == "unauthorized"


def test_create_requires_csrf(api: CheckoutAPI) -> None:
    response = call(
        api,
        "POST",
        "/api/wallet/checkout",
        body={"amount_cents": 1500},
        csrf=False,
    )
    assert response.status == 403
    assert payload(response)["error"]["code"] == "csrf_failed"


def test_unknown_path_is_404(api: CheckoutAPI) -> None:
    response = call(api, "GET", "/api/wallet/checkout/nope")
    assert response.status == 404


# -- create ----------------------------------------------------------------


def test_create_returns_a_checkout_payload(api: CheckoutAPI, transport: CheckoutTransport) -> None:
    body = create(api)

    checkout = body["checkout"]
    assert checkout["key_id"] == KEY_ID
    assert checkout["order_id"] == "order_test0001"
    assert checkout["receipt"] == body["proposal"]["id"]
    assert checkout["amount_cents"] == 1500
    assert checkout["currency"] == "usd"
    assert checkout["name"] == "Kelp"
    assert body["idempotent"] is False
    assert TEST_SECRET not in json.dumps(body)
    assert WEBHOOK_SECRET not in json.dumps(body)
    assert transport.orders[0]["receipt"] == body["proposal"]["id"]


def test_repeat_create_is_a_200_with_the_same_order(api: CheckoutAPI) -> None:
    first = create(api)
    response = call(
        api,
        "POST",
        "/api/wallet/checkout",
        body={"amount_cents": 1500, "idempotency_key": "checkout-key-1"},
    )

    assert response.status == 200
    body = payload(response)
    assert body["idempotent"] is True
    assert body["checkout"]["order_id"] == first["checkout"]["order_id"]


def test_create_rejects_bad_amounts(api: CheckoutAPI) -> None:
    for amount in (0, -1, True, "100", 100_000_000):
        response = call(
            api, "POST", "/api/wallet/checkout", body={"amount_cents": amount}
        )
        assert response.status == 400
        assert payload(response)["error"]["code"] == "invalid_amount"


def test_create_rejects_unknown_fields(api: CheckoutAPI) -> None:
    response = call(
        api,
        "POST",
        "/api/wallet/checkout",
        body={"amount_cents": 1500, "unknown_field": True},
    )
    assert response.status == 400
    assert payload(response)["error"]["code"] == "invalid_request"


def test_create_rejects_raw_card_data(api: CheckoutAPI) -> None:
    response = call(
        api,
        "POST",
        "/api/wallet/checkout",
        body={"amount_cents": 1500, "description": PAN},
    )
    assert response.status == 422
    assert payload(response)["error"]["code"] == "unstorable_card"
    assert PAN not in response.body.decode("utf-8")


def test_status_never_carries_a_key(api: CheckoutAPI) -> None:
    response = call(api, "GET", "/api/wallet/checkout")
    body = payload(response)

    assert response.status == 200
    assert body["checkout"]["configured"] is True
    assert body["checkout"]["checkout"] == "hosted"
    assert TEST_SECRET not in response.body.decode("utf-8")
    assert KEY_ID not in response.body.decode("utf-8")


# -- confirm ---------------------------------------------------------------


def settle(api: CheckoutAPI, order_id: str, payment_id: str = "pay_test0001") -> Response:
    return call(
        api,
        "POST",
        "/api/wallet/checkout/confirm",
        body={
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature_for(order_id, payment_id),
        },
    )


def test_confirm_settles_and_returns_a_receipt(api: CheckoutAPI) -> None:
    created = create(api)
    order_id = created["checkout"]["order_id"]

    response = settle(api, order_id)
    body = payload(response)

    assert response.status == 200
    assert body["proposal"]["status"] == "paid"
    assert body["receipt"]["amount_cents"] == 1500
    assert body["receipt"]["order_id"] == created["proposal"]["id"]
    assert body["idempotent"] is False


def test_confirm_replay_returns_the_same_receipt(api: CheckoutAPI) -> None:
    created = create(api)
    order_id = created["checkout"]["order_id"]

    first = payload(settle(api, order_id))
    second = payload(settle(api, order_id))

    assert second["idempotent"] is True
    assert second["receipt"] == first["receipt"]


def test_confirm_with_a_bad_signature_is_401(api: CheckoutAPI, transport: CheckoutTransport) -> None:
    created = create(api)
    order_id = created["checkout"]["order_id"]

    response = call(
        api,
        "POST",
        "/api/wallet/checkout/confirm",
        body={
            "razorpay_order_id": order_id,
            "razorpay_payment_id": "pay_test0001",
            "razorpay_signature": "0" * 64,
        },
    )

    assert response.status == 401
    assert payload(response)["error"]["code"] == "signature_invalid"
    assert transport.fetch_calls == []


def test_confirm_requires_all_fields(api: CheckoutAPI) -> None:
    response = call(
        api,
        "POST",
        "/api/wallet/checkout/confirm",
        body={"razorpay_order_id": "order_test0001"},
    )
    assert response.status == 400
    assert payload(response)["error"]["code"] == "invalid_request"


def test_confirm_rejects_an_unknown_order(api: CheckoutAPI) -> None:
    response = call(
        api,
        "POST",
        "/api/wallet/checkout/confirm",
        body={
            "razorpay_order_id": "order_unknown1",
            "razorpay_payment_id": "pay_test0001",
            "razorpay_signature": signature_for("order_unknown1", "pay_test0001"),
        },
    )
    assert response.status == 404
    assert payload(response)["error"]["code"] == "unknown_proposal"


# -- webhook ---------------------------------------------------------------


def webhook_call(
    api: CheckoutAPI, body: bytes, signature: str | None
) -> Response:
    headers = {}
    if signature is not None:
        headers["x-razorpay-signature"] = signature
    # No bearer token and no CSRF: the HMAC is the authentication.
    return call(
        api,
        "POST",
        "/api/wallet/checkout/webhook",
        body=body,
        headers=headers,
        auth=False,
        csrf=False,
    )


def test_webhook_is_not_session_authenticated(api: CheckoutAPI) -> None:
    created = create(api)
    order_id = created["checkout"]["order_id"]
    body = json.dumps(webhook_event(order_id, "pay_test0001")).encode("utf-8")
    signature = expected_webhook_signature(WEBHOOK_SECRET, body)

    response = webhook_call(api, body, signature)

    assert response.status == 200
    assert payload(response)["proposal"]["status"] == "paid"


def test_webhook_with_a_bad_signature_is_401(
    api: CheckoutAPI, transport: CheckoutTransport
) -> None:
    created = create(api)
    order_id = created["checkout"]["order_id"]
    body = json.dumps(webhook_event(order_id, "pay_test0001")).encode("utf-8")

    response = webhook_call(api, body, "0" * 64)

    assert response.status == 401
    assert payload(response)["error"]["code"] == "signature_invalid"
    assert transport.fetch_calls == []


def test_webhook_without_a_signature_is_401(api: CheckoutAPI) -> None:
    created = create(api)
    order_id = created["checkout"]["order_id"]
    body = json.dumps(webhook_event(order_id, "pay_test0001")).encode("utf-8")

    response = webhook_call(api, body, None)

    assert response.status == 401


def test_webhook_replay_is_200_and_idempotent(api: CheckoutAPI) -> None:
    created = create(api)
    order_id = created["checkout"]["order_id"]
    body = json.dumps(webhook_event(order_id, "pay_test0001")).encode("utf-8")
    signature = expected_webhook_signature(WEBHOOK_SECRET, body)

    webhook_call(api, body, signature)
    response = webhook_call(api, body, signature)

    assert response.status == 200
    assert payload(response)["idempotent"] is True


def test_webhook_for_an_unknown_order_is_acknowledged(api: CheckoutAPI) -> None:
    body = json.dumps(webhook_event("order_unknown1", "pay_test0001")).encode("utf-8")
    signature = expected_webhook_signature(WEBHOOK_SECRET, body)

    response = webhook_call(api, body, signature)
    body_json = payload(response)

    assert response.status == 200
    assert body_json["handled"] is False
    assert body_json["reason"] == "unknown_order"


# -- configuration ---------------------------------------------------------


def test_unconfigured_checkout_returns_503() -> None:
    api = CheckoutAPI(
        RazorpayCheckout(wallet=RazorpayWallet.from_env({})),
        auth_token=TOKEN,
        csrf_token=CSRF,
    )

    response = call(
        api,
        "POST",
        "/api/wallet/checkout",
        body={"amount_cents": 1500},
    )

    assert response.status == 503
    assert payload(response)["error"]["code"] == "not_configured"
    assert TEST_SECRET not in response.body.decode("utf-8")

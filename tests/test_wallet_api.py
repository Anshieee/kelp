"""KEL-10 wallet HTTP surface tests. Fake transport, no network, no real keys."""

from __future__ import annotations

import json
from typing import Any, Mapping

import pytest

from chat.api import Request, Response
from chat.wallet import WalletAPI
from payments.stripe_wallet import STRIPE_SECRET_KEY_ENV
from payments.wallet import (
    SpendCaps,
    StripeGatewayError,
    Wallet,
    WalletLedger,
    redact_text,
)
from tests.test_wallet import (
    PAN,
    PM_ID,
    TEST_KEY,
    FakeGateway,
    make_wallet,
)

TOKEN = "test-session-token"
CSRF = "test-csrf-token"


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
def api(gateway: FakeGateway) -> WalletAPI:
    return WalletAPI(
        make_wallet(gateway=gateway), auth_token=TOKEN, csrf_token=CSRF
    )


def call(
    api: WalletAPI,
    method: str,
    path: str,
    *,
    body: Any = None,
    headers: Mapping[str, str] | None = None,
    auth: bool = True,
    csrf: bool = True,
    query: Mapping[str, list[str]] | None = None,
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
            query=dict(query or {}),
            headers=request_headers,
            body=raw,
        )
    )


def payload(response: Response) -> dict[str, Any]:
    return json.loads(response.body.decode("utf-8"))


def text(response: Response) -> str:
    return response.body.decode("utf-8")


# -- auth ------------------------------------------------------------------


def test_wallet_endpoints_require_auth(api: WalletAPI) -> None:
    response = call(api, "GET", "/api/wallet", auth=False)

    assert response.status == 401
    assert payload(response)["error"]["code"] == "unauthorized"


def test_session_cookie_is_accepted(api: WalletAPI) -> None:
    response = call(
        api,
        "GET",
        "/api/wallet",
        auth=False,
        headers={"cookie": f"session={TOKEN}"},
    )
    assert response.status == 200


def test_mutations_require_csrf(api: WalletAPI) -> None:
    response = call(api, "POST", "/api/wallet/setup-intent", csrf=False)

    assert response.status == 403
    assert payload(response)["error"]["code"] == "csrf_failed"


def test_cross_origin_mutation_is_rejected_without_a_token(gateway: FakeGateway) -> None:
    open_api = WalletAPI(make_wallet(gateway=gateway))
    response = call(
        open_api,
        "POST",
        "/api/wallet/setup-intent",
        csrf=False,
        headers={"origin": "https://evil.example", "host": "localhost:8765"},
    )
    assert response.status == 403

    same_origin = call(
        open_api,
        "POST",
        "/api/wallet/setup-intent",
        csrf=False,
        headers={"origin": "http://localhost:8765", "host": "localhost:8765"},
    )
    assert same_origin.status == 200


# -- reads -----------------------------------------------------------------


def test_status_endpoint_never_includes_the_key(api: WalletAPI) -> None:
    response = call(api, "GET", "/api/wallet")

    assert response.status == 200
    body = payload(response)
    assert body["wallet"]["configured"] is True
    assert body["wallet"]["mode"] == "test"
    assert TEST_KEY not in text(response)


def test_list_cards_starts_empty(api: WalletAPI) -> None:
    response = call(api, "GET", "/api/wallet/cards")

    assert response.status == 200
    assert payload(response) == {"ok": True, "cards": []}


def test_limits_endpoint(api: WalletAPI) -> None:
    response = call(api, "GET", "/api/wallet/limits")

    assert response.status == 200
    limits = payload(response)["limits"]
    assert limits["currency"] == "usd"
    assert limits["per_purchase_cents"] == 5000
    assert limits["monthly_cents"] == 20_000
    assert limits["spent_cents"] == 0
    assert limits["reserved_cents"] == 0
    assert limits["remaining_cents"] == 20_000


def test_limits_rejects_a_bad_currency(api: WalletAPI) -> None:
    response = call(
        api, "GET", "/api/wallet/limits", query={"currency": ["usd1"]}
    )
    assert response.status == 400
    assert payload(response)["error"]["code"] == "invalid_currency"


# -- adding a card ---------------------------------------------------------


def test_setup_intent_returns_a_client_secret_but_not_the_key(api: WalletAPI) -> None:
    response = call(api, "POST", "/api/wallet/setup-intent")

    assert response.status == 200
    intent = payload(response)["setup_intent"]
    assert intent["id"] == "seti_test_123"
    assert intent["client_secret"].startswith("seti_")
    assert TEST_KEY not in text(response)


def test_attach_saves_an_allowlisted_card(api: WalletAPI) -> None:
    response = call(
        api,
        "POST",
        "/api/wallet/attach",
        body={"payment_method_id": PM_ID, "make_default": True},
    )

    assert response.status == 200
    card = payload(response)["card"]
    assert card["last4"] == "4242"
    assert card["brand"] == "visa"
    assert card["is_default"] is True
    assert PAN not in text(response)

    listed = payload(call(api, "GET", "/api/wallet/cards"))["cards"]
    assert [item["id"] for item in listed] == [PM_ID]


def test_attach_rejects_card_details_and_never_echoes_them(api: WalletAPI) -> None:
    response = call(
        api,
        "POST",
        "/api/wallet/attach",
        body={"payment_method_id": PM_ID, "number": PAN, "cvc": "123"},
    )

    assert response.status == 422
    assert payload(response)["error"]["code"] == "unstorable_card"
    assert PAN not in text(response)
    assert "123" not in payload(response)["error"].get("details", {})
    assert payload(call(api, "GET", "/api/wallet/cards"))["cards"] == []


def test_unknown_body_fields_are_rejected_without_echoing_values(api: WalletAPI) -> None:
    response = call(
        api,
        "POST",
        "/api/wallet/attach",
        body={"payment_method_id": PM_ID, "surprise": "do-not-echo"},
    )

    assert response.status == 400
    body = payload(response)
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["details"]["fields"] == ["surprise"]
    assert "do-not-echo" not in text(response)


# -- proposals -------------------------------------------------------------


def test_propose_and_idempotent_retry(api: WalletAPI, gateway: FakeGateway) -> None:
    body = {
        "amount_cents": 1500,
        "currency": "usd",
        "description": "coffee",
        "idempotency_key": "order-api-123",
    }

    first = call(api, "POST", "/api/wallet/propose", body=body)
    second = call(api, "POST", "/api/wallet/propose", body=body)

    assert first.status == 201
    assert payload(first)["proposal"]["status"] == "pending"
    assert second.status == 200
    assert payload(second)["idempotent"] is True
    assert payload(first)["proposal"]["id"] == payload(second)["proposal"]["id"]
    assert gateway.payments == []


def test_propose_enforces_the_per_purchase_cap(api: WalletAPI) -> None:
    response = call(api, "POST", "/api/wallet/propose", body={"amount_cents": 5001})

    assert response.status == 409
    error = payload(response)["error"]
    assert error["code"] == "cap_exceeded"
    assert error["details"]["per_purchase_cap_cents"] == 5000


@pytest.mark.parametrize("amount", [0, -100, 12.5, True, "100", None])
def test_propose_rejects_bad_amounts(api: WalletAPI, amount: Any) -> None:
    response = call(
        api, "POST", "/api/wallet/propose", body={"amount_cents": amount}
    )

    assert response.status == 400
    assert payload(response)["error"]["code"] == "invalid_amount"


def test_propose_rejects_a_bad_currency(api: WalletAPI) -> None:
    response = call(
        api,
        "POST",
        "/api/wallet/propose",
        body={"amount_cents": 100, "currency": "dollars"},
    )
    assert response.status == 400
    assert payload(response)["error"]["code"] == "invalid_currency"


def test_card_like_description_is_rejected_without_echoing(api: WalletAPI) -> None:
    response = call(
        api,
        "POST",
        "/api/wallet/propose",
        body={"amount_cents": 100, "description": f"card {PAN}"},
    )

    assert response.status == 422
    assert payload(response)["error"]["code"] == "unstorable_card"
    assert PAN not in text(response)


# -- approval and payment --------------------------------------------------


def _ready_to_pay(api: WalletAPI, amount: int = 1500) -> dict[str, Any]:
    attached = call(
        api,
        "POST",
        "/api/wallet/attach",
        body={"payment_method_id": PM_ID, "make_default": True},
    )
    assert attached.status == 200
    proposed = call(
        api,
        "POST",
        "/api/wallet/propose",
        body={"amount_cents": amount, "idempotency_key": "order-api-pay-1"},
    )
    assert proposed.status == 201
    return payload(proposed)["proposal"]


def test_approve_pays_and_returns_a_receipt(api: WalletAPI, gateway: FakeGateway) -> None:
    proposal = _ready_to_pay(api)

    response = call(
        api,
        "POST",
        "/api/wallet/approve",
        body={"proposal_id": proposal["id"]},
    )

    assert response.status == 200
    body = payload(response)
    assert body["proposal"]["status"] == "paid"
    receipt = body["proposal"]["receipt"]
    assert receipt["payment_intent_id"] == "pi_test_123"
    assert receipt["amount_cents"] == 1500
    assert receipt["last4"] == "4242"
    assert len(gateway.payments) == 1
    assert gateway.payments[0]["idempotency_key"] == f"kelp-order-{proposal['id']}"
    assert TEST_KEY not in text(response)
    assert PAN not in text(response)

    again = call(
        api,
        "POST",
        "/api/wallet/approve",
        body={"proposal_id": proposal["id"]},
    )
    assert payload(again)["idempotent"] is True
    assert len(gateway.payments) == 1


def test_retryable_payment_failure_is_resumable_over_http(
    api: WalletAPI, gateway: FakeGateway
) -> None:
    proposal = _ready_to_pay(api)
    gateway.payment_error = StripeGatewayError(
        "Can't reach the card processor right now.", retryable=True
    )

    failed = call(
        api,
        "POST",
        "/api/wallet/approve",
        body={"proposal_id": proposal["id"]},
    )
    assert failed.status == 502
    error = payload(failed)["error"]
    assert error["code"] == "payment_failed"
    assert error["retryable"] is True

    limits = payload(call(api, "GET", "/api/wallet/limits"))["limits"]
    assert limits["spent_cents"] == 0
    assert limits["reserved_cents"] == 1500

    gateway.payment_error = None
    retried = call(
        api,
        "POST",
        "/api/wallet/approve",
        body={"proposal_id": proposal["id"]},
    )
    assert retried.status == 200
    assert payload(retried)["proposal"]["status"] == "paid"
    assert len(gateway.payments) == 2
    assert len(gateway.intents) == 1


def test_deny_returns_a_denial_without_a_receipt(
    api: WalletAPI, gateway: FakeGateway
) -> None:
    proposal = _ready_to_pay(api)

    response = call(
        api,
        "POST",
        "/api/wallet/approve",
        body={"proposal_id": proposal["id"], "decision": "deny"},
    )

    assert response.status == 200
    assert payload(response)["proposal"]["status"] == "denied"
    assert "receipt" not in payload(response)["proposal"]
    assert gateway.payments == []


def test_approve_without_a_card_is_a_conflict(api: WalletAPI, gateway: FakeGateway) -> None:
    proposed = call(
        api,
        "POST",
        "/api/wallet/propose",
        body={"amount_cents": 500, "idempotency_key": "order-api-nocard"},
    )
    proposal_id = payload(proposed)["proposal"]["id"]

    response = call(
        api,
        "POST",
        "/api/wallet/approve",
        body={"proposal_id": proposal_id},
    )

    assert response.status == 409
    assert payload(response)["error"]["code"] == "no_saved_card"
    assert gateway.payments == []


def test_invalid_decision_is_rejected(api: WalletAPI) -> None:
    proposal = _ready_to_pay(api)
    response = call(
        api,
        "POST",
        "/api/wallet/approve",
        body={"proposal_id": proposal["id"], "decision": "maybe"},
    )
    assert response.status == 400
    assert payload(response)["error"]["code"] == "invalid_request"


# -- errors ----------------------------------------------------------------


def test_unconfigured_wallet_reports_503(gateway: FakeGateway) -> None:
    wallet = Wallet(
        config=None,
        caps=SpendCaps(),
        ledger=WalletLedger(),
        gateway=gateway,
    )
    api = WalletAPI(wallet, auth_token=TOKEN, csrf_token=CSRF)

    response = call(api, "POST", "/api/wallet/setup-intent")

    assert response.status == 503
    assert payload(response)["error"]["code"] == "not_configured"


def test_malformed_json_is_a_400(api: WalletAPI) -> None:
    response = call(api, "POST", "/api/wallet/propose", body=b"{not json")
    assert response.status == 400
    assert payload(response)["error"]["code"] == "invalid_request"


def test_unknown_routes_and_methods_are_404(api: WalletAPI) -> None:
    assert call(api, "GET", "/api/wallet/unknown").status == 404
    assert call(api, "GET", "/api/nope").status == 404
    assert call(api, "POST", "/api/wallet/cards").status == 404
    assert call(api, "GET", "/api/wallet/propose").status == 404
    assert call(api, "POST", "/api/wallet/setup-intent/extra").status == 404


def test_responses_are_uncacheable(api: WalletAPI) -> None:
    response = call(api, "GET", "/api/wallet/cards")
    assert response.headers["Cache-Control"] == "no-store"


def test_redaction_helper_is_used_by_the_api() -> None:
    assert redact_text(f"prefix {TEST_KEY}") == "prefix [redacted]"
    assert PAN not in redact_text(f"card {PAN}")

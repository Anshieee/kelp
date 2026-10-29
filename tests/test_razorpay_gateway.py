"""KEL-10 Razorpay gateway tests: fake transport only, no network, no real keys.

The fake mirrors the two Razorpay guarantees the wallet relies on:

- an idempotency key returns the *same* response instead of creating a second
  order or payment;
- a card token belongs to one customer and is single-use.

Credential-shaped literals live only in this file (and the ``payments`` source
tree is scanned to make sure none leak into it).
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any, Mapping

import pytest

from payments.razorpay_gateway import (
    CUSTOMER_ID_ENV,
    KEY_ID_ENV,
    KEY_SECRET_ENV,
    SUPPORTED_CURRENCIES,
    HttpRazorpayTransport,
    RazorpayConfig,
    RazorpayGateway,
    RazorpayGatewayError,
    RazorpayNotConfigured,
    RazorpayWallet,
    redact_razorpay_payload,
    redact_razorpay_text,
    token_id_from_wallet_id,
    wallet_payment_intent_id,
    wallet_payment_method_id,
)
from payments.stripe_wallet import WalletNotConfigured
from payments.wallet import (
    MONTHLY_CAP_ENV,
    PER_PURCHASE_CAP_ENV,
    Card,
    SpendCaps,
    WalletError,
    WalletLedger,
)

KEY_ID = "rzp_test_abc123"
TEST_SECRET = "razorpaysecret123"
CUSTOMER = "cust_test1234"
TOKEN = "token_visa4242"
OTHER_TOKEN = "token_mc4444"
PAN = "4242424242424242"
SOURCE_ROOT = Path(__file__).resolve().parents[1]
_KEY_LITERAL = re.compile(r"rzp_(?:live|test)_[A-Za-z0-9]{8,}")


class FakeTransport:
    """Records calls and returns Razorpay-shaped payloads. Never networks."""

    def __init__(self) -> None:
        self.customers: list[dict[str, Any]] = []
        self.created_customers: list[Mapping[str, Any]] = []
        self.token_lookups: list[dict[str, Any]] = []
        self.listings: list[dict[str, Any]] = []
        self.orders: list[dict[str, Any]] = []
        self.payments: list[dict[str, Any]] = []
        self.captures: list[dict[str, Any]] = []
        self.token_payloads: dict[str, Mapping[str, Any]] = {}
        self.customer_tokens: list[Mapping[str, Any]] = []
        self.order_error: Exception | None = None
        self.payment_error: Exception | None = None
        self.payment_status = "captured"
        self.capture_status = "captured"
        self.payment_amount: int | None = None
        self._orders: dict[str, Mapping[str, Any]] = {}
        self._payments: dict[str, Mapping[str, Any]] = {}
        self._seq = 0

    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}test{self._seq:04d}"

    def create_customer(
        self, *, key_id: str, key_secret: str, notes: Mapping[str, str]
    ) -> Mapping[str, Any]:
        self.customers.append(
            {"key_id": key_id, "key_secret": key_secret, "notes": dict(notes)}
        )
        created = {"id": self._next("cust_"), "entity": "customer"}
        self.created_customers.append(created)
        return created

    def _last_customer(self) -> Mapping[str, Any]:
        return self.created_customers[-1]

    def fetch_token(
        self, *, key_id: str, key_secret: str, token_id: str
    ) -> Mapping[str, Any]:
        self.token_lookups.append(
            {"key_id": key_id, "key_secret": key_secret, "token_id": token_id}
        )
        existing = self.token_payloads.get(token_id)
        if existing is not None:
            return existing
        return {
            "id": token_id,
            "entity": "token",
            "customer_id": CUSTOMER,
            "method": "card",
            "used": False,
            "card": {
                "network": "Visa",
                "last4": "4242",
                "expiry_month": 12,
                "expiry_year": 2030,
            },
        }

    def list_tokens(
        self, *, key_id: str, key_secret: str, customer_id: str
    ) -> list[Mapping[str, Any]]:
        self.listings.append(
            {"key_id": key_id, "key_secret": key_secret, "customer_id": customer_id}
        )
        return list(self.customer_tokens)

    def create_order(
        self,
        *,
        key_id: str,
        key_secret: str,
        amount_cents: int,
        currency: str,
        notes: Mapping[str, str],
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        self.orders.append(
            {
                "key_id": key_id,
                "key_secret": key_secret,
                "amount_cents": amount_cents,
                "currency": currency,
                "notes": dict(notes),
                "idempotency_key": idempotency_key,
            }
        )
        if self.order_error is not None:
            raise self.order_error
        existing = self._orders.get(idempotency_key)
        if existing is not None:
            return existing
        order = {
            "id": self._next("order_"),
            "entity": "order",
            "amount": amount_cents,
            "currency": currency,
            "status": "created",
        }
        self._orders[idempotency_key] = order
        return order

    def create_payment(
        self,
        *,
        key_id: str,
        key_secret: str,
        order_id: str,
        token_id: str,
        amount_cents: int,
        currency: str,
        description: str,
        customer_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        self.payments.append(
            {
                "key_id": key_id,
                "key_secret": key_secret,
                "order_id": order_id,
                "token_id": token_id,
                "amount_cents": amount_cents,
                "currency": currency,
                "description": description,
                "customer_id": customer_id,
                "idempotency_key": idempotency_key,
            }
        )
        if self.payment_error is not None:
            raise self.payment_error
        existing = self._payments.get(idempotency_key)
        if existing is not None:
            return existing
        payment = {
            "id": self._next("pay_"),
            "entity": "payment",
            "order_id": order_id,
            "amount": self.payment_amount or amount_cents,
            "currency": currency,
            "status": self.payment_status,
            "method": "card",
        }
        self._payments[idempotency_key] = payment
        return payment

    def capture_payment(
        self,
        *,
        key_id: str,
        key_secret: str,
        payment_id: str,
        amount_cents: int,
        currency: str,
    ) -> Mapping[str, Any]:
        self.captures.append(
            {
                "key_id": key_id,
                "key_secret": key_secret,
                "payment_id": payment_id,
                "amount_cents": amount_cents,
                "currency": currency,
            }
        )
        return {
            "id": payment_id,
            "entity": "payment",
            "amount": amount_cents,
            "currency": currency,
            "status": self.capture_status,
        }


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


def make_wallet(
    *,
    transport: FakeTransport | None = None,
    caps: SpendCaps | None = None,
    customer_id: str = CUSTOMER,
    config: RazorpayConfig | None = None,
    ledger: WalletLedger | None = None,
) -> RazorpayWallet:
    return RazorpayWallet(
        config=config or RazorpayConfig(key_id=KEY_ID, secret_key=TEST_SECRET),
        caps=caps or SpendCaps(per_purchase_cents=5000, monthly_cents=20_000),
        ledger=ledger or WalletLedger(":memory:"),
        gateway=RazorpayGateway(key_id=KEY_ID, transport=transport or FakeTransport()),
        customer_id=customer_id,
    )


def add_card(wallet: RazorpayWallet, token: str = TOKEN) -> dict[str, Any]:
    return wallet.attach(wallet_payment_method_id(token), make_default=True)


def ledger_text(wallet: RazorpayWallet) -> str:
    return "\n".join(wallet.ledger._conn.iterdump())  # noqa: SLF001 - test inspection


# -- configuration ---------------------------------------------------------


def test_missing_keys_are_unconfigured() -> None:
    for env in ({}, {KEY_ID_ENV: "   "}, {KEY_SECRET_ENV: "  "}):
        with pytest.raises(RazorpayNotConfigured):
            RazorpayConfig.from_env(env)


def test_not_configured_is_the_wallet_sentinel() -> None:
    # The HTTP surface already catches WalletNotConfigured for a 503.
    assert issubclass(RazorpayNotConfigured, WalletNotConfigured)
    with pytest.raises(WalletNotConfigured):
        RazorpayConfig.from_env({})


def test_half_a_credential_pair_is_a_configuration_error() -> None:
    with pytest.raises(WalletError) as caught:
        RazorpayWallet.from_env({KEY_ID_ENV: KEY_ID})
    assert caught.value.code == "invalid_config"
    with pytest.raises(WalletError):
        RazorpayWallet.from_env({KEY_SECRET_ENV: TEST_SECRET})


@pytest.mark.parametrize(
    ("key_id", "mode"),
    [
        ("rzp_test_abc123", "test"),
        ("rzp_live_abc123", "live"),
        ("pk_test_abc123", "unknown"),
        ("something", "unknown"),
    ],
)
def test_mode_detection(key_id: str, mode: str) -> None:
    assert RazorpayConfig(key_id=key_id, secret_key="s").mode == mode


def test_configured_wallet_never_exposes_either_key(transport: FakeTransport) -> None:
    wallet = make_wallet(transport=transport)
    status_text = json.dumps(wallet.status(), default=str)

    assert wallet.status()["provider"] == "razorpay"
    assert wallet.status()["configured"] is True
    assert wallet.status()["mode"] == "test"
    assert wallet.status()["network_calls"] is False
    for blob in (
        repr(RazorpayConfig(key_id=KEY_ID, secret_key=TEST_SECRET)),
        str(RazorpayConfig(key_id=KEY_ID, secret_key=TEST_SECRET)),
        repr(wallet),
        str(wallet),
        status_text,
        ledger_text(wallet),
    ):
        assert TEST_SECRET not in blob
        assert KEY_ID not in blob


def test_from_env_reads_only_the_server_environment() -> None:
    wallet = RazorpayWallet.from_env(
        {
            KEY_ID_ENV: KEY_ID,
            KEY_SECRET_ENV: TEST_SECRET,
            CUSTOMER_ID_ENV: CUSTOMER,
            PER_PURCHASE_CAP_ENV: "1234",
            MONTHLY_CAP_ENV: "5678",
        }
    )

    assert wallet.configured is True
    assert wallet.customer_id == CUSTOMER
    assert wallet.caps == SpendCaps(1234, 5678)
    assert wallet.limits("usd")["remaining_cents"] == 5678


def test_unconfigured_wallet_is_visible_but_cannot_take_payments() -> None:
    import socket

    def _no_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a network connection was attempted")

    original = socket.socket.connect
    socket.socket.connect = _no_network  # type: ignore[assignment]
    try:
        wallet = RazorpayWallet.from_env({})
        assert wallet.status()["configured"] is False
        assert wallet.status()["mode"] == "unconfigured"
        with pytest.raises(RazorpayNotConfigured):
            wallet.create_setup_intent()
        with pytest.raises(RazorpayNotConfigured):
            wallet.attach(wallet_payment_method_id(TOKEN))

        wallet.ledger.add_card(Card(wallet_payment_method_id(TOKEN), "visa", "4242", 12, 2030, True))
        proposal, _ = wallet.propose(1000)
        with pytest.raises(WalletNotConfigured):
            wallet.approve(proposal.id)
    finally:
        socket.socket.connect = original  # type: ignore[assignment]


def test_non_razorpay_key_id_is_refused_before_a_call() -> None:
    wallet = make_wallet(config=RazorpayConfig(key_id="pk_test_abc123", secret_key="s"))
    with pytest.raises(WalletError) as caught:
        wallet.create_setup_intent()
    assert caught.value.code == "invalid_config"


def test_no_hardcoded_razorpay_keys_in_source() -> None:
    offenders = [
        str(path)
        for path in (SOURCE_ROOT / "payments").glob("*.py")
        if _KEY_LITERAL.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_import_does_not_require_keys_or_build_a_client() -> None:
    assert HttpRazorpayTransport()._client is None  # noqa: SLF001 - laziness check
    assert RazorpayGateway(key_id=KEY_ID)._transport is None  # noqa: SLF001


# -- id mapping ------------------------------------------------------------


def test_wallet_ids_round_trip() -> None:
    assert wallet_payment_method_id(TOKEN) == "pm_rzpvisa4242"
    assert token_id_from_wallet_id("pm_rzpvisa4242") == TOKEN
    assert wallet_payment_intent_id("pay_test0001") == "pi_rzptest0001"


@pytest.mark.parametrize(
    "value", ["", "token_", "token_!!", "../../etc/passwd", "pm_x", None]
)
def test_wallet_ids_reject_malformed_input(value: Any) -> None:
    with pytest.raises((WalletError, RazorpayGatewayError)):
        wallet_payment_method_id(value)
    with pytest.raises(WalletError):
        token_id_from_wallet_id(value)


@pytest.mark.parametrize(
    "value", ["token_visa4242", "../../etc/passwd", "pm_x", "", "pm_rzp"]
)
def test_token_lookup_rejects_non_wallet_ids(value: str) -> None:
    with pytest.raises(WalletError):
        token_id_from_wallet_id(value)


# -- customers and cards ---------------------------------------------------


def test_setup_intent_returns_a_handle_and_no_key(transport: FakeTransport) -> None:
    wallet = make_wallet(transport=transport, customer_id="")
    result = wallet.create_setup_intent()

    # Razorpay has no client_secret; the customer handle is the only value the
    # browser needs for card-on-file, and it carries no key material.
    assert result["id"].startswith("seti_rzp_")
    assert result["client_secret"].startswith("cust_")
    assert result["client_secret"] == transport._last_customer()["id"]  # noqa: SLF001
    assert KEY_ID not in json.dumps(result)
    assert TEST_SECRET not in json.dumps(result)
    assert transport.customers[0]["key_id"] == KEY_ID
    assert transport.customers[0]["key_secret"] == TEST_SECRET
    assert transport.customers[0]["notes"] == {"source": "kelp-wallet"}


def test_setup_intent_reuses_a_configured_customer(transport: FakeTransport) -> None:
    wallet = make_wallet(transport=transport)
    result = wallet.create_setup_intent()

    assert result["client_secret"] == CUSTOMER
    assert transport.customers == []  # no needless customer creation


def test_attach_keeps_only_allowlisted_card_fields(transport: FakeTransport) -> None:
    wallet = make_wallet(transport=transport)
    card = add_card(wallet)

    assert card == {
        "id": "pm_rzpvisa4242",
        "brand": "visa",
        "last4": "4242",
        "exp_month": 12,
        "exp_year": 2030,
        "is_default": True,
    }
    assert transport.token_lookups[0]["token_id"] == TOKEN
    assert transport.token_lookups[0]["key_secret"] == TEST_SECRET
    assert PAN not in ledger_text(wallet)
    assert TEST_SECRET not in ledger_text(wallet)


def test_attach_accepts_a_masked_card_number_for_last4(transport: FakeTransport) -> None:
    transport.token_payloads[TOKEN] = {
        "id": TOKEN,
        "entity": "token",
        "customer_id": CUSTOMER,
        "method": "card",
        "used": False,
        "card": {
            "network": "MasterCard",
            "number": "XXXX XXXX XXXX 4444",
            "expiry_month": "01",
            "expiry_year": "2031",
        },
    }
    wallet = make_wallet(transport=transport)
    card = add_card(wallet)

    assert card["brand"] == "mastercard"
    assert card["last4"] == "4444"
    assert card["exp_month"] == 1
    assert "XXXX" not in json.dumps(card)


def test_attach_never_stores_a_raw_pan(transport: FakeTransport) -> None:
    transport.token_payloads[TOKEN] = {
        "id": TOKEN,
        "customer_id": CUSTOMER,
        "method": "card",
        "used": False,
        "card": {
            "network": "Visa",
            "number": PAN,
            "cvc": "123",
            "expiry_month": 12,
            "expiry_year": 2030,
        },
    }
    wallet = make_wallet(transport=transport)
    card = add_card(wallet)

    assert card["last4"] == "4242"
    assert PAN not in json.dumps(card)
    assert PAN not in ledger_text(wallet)


def test_used_token_and_foreign_token_are_rejected(transport: FakeTransport) -> None:
    wallet = make_wallet(transport=transport)

    transport.token_payloads[TOKEN] = {
        "id": TOKEN,
        "customer_id": CUSTOMER,
        "method": "card",
        "used": True,
        "card": {"network": "Visa", "last4": "4242", "expiry_month": 12, "expiry_year": 2030},
    }
    with pytest.raises(WalletError) as caught:
        wallet.attach(wallet_payment_method_id(TOKEN))
    assert caught.value.code == "invalid_payment_method"

    transport.token_payloads[TOKEN] = {
        "id": TOKEN,
        "customer_id": "cust_someoneelse",
        "method": "card",
        "used": False,
        "card": {"network": "Visa", "last4": "4242", "expiry_month": 12, "expiry_year": 2030},
    }
    with pytest.raises(WalletError) as caught:
        wallet.attach(wallet_payment_method_id(TOKEN))
    assert caught.value.code == "invalid_payment_method"
    assert wallet.list_cards() == []


def test_malformed_card_token_is_not_a_payment_method(transport: FakeTransport) -> None:
    wallet = make_wallet(transport=transport)
    transport.token_payloads[TOKEN] = {
        "id": TOKEN,
        "customer_id": CUSTOMER,
        "method": "card",
        "card": {"network": "Visa", "expiry_month": 13, "expiry_year": 2030},
    }

    with pytest.raises(WalletError) as caught:
        wallet.attach(wallet_payment_method_id(TOKEN))
    assert caught.value.code == "invalid_payment_method"


def test_attach_requires_a_customer(transport: FakeTransport) -> None:
    wallet = make_wallet(transport=transport, customer_id="")
    with pytest.raises(WalletError) as caught:
        wallet.attach(wallet_payment_method_id(TOKEN))
    assert caught.value.code == "invalid_config"
    assert transport.token_lookups == []


def test_attach_rejects_a_path_traversal_id(transport: FakeTransport) -> None:
    wallet = make_wallet(transport=transport)
    with pytest.raises(WalletError) as caught:
        wallet.attach("pm_rzp../../etc/passwd")
    assert caught.value.code == "invalid_payment_method"
    assert transport.token_lookups == []


def test_refresh_lists_tokens_and_skips_unusable_ones(transport: FakeTransport) -> None:
    wallet = make_wallet(transport=transport)
    transport.customer_tokens = [
        {
            "id": TOKEN,
            "customer_id": CUSTOMER,
            "method": "card",
            "used": False,
            "card": {"network": "Visa", "last4": "4242", "expiry_month": 12, "expiry_year": 2030},
        },
        {
            "id": OTHER_TOKEN,
            "customer_id": CUSTOMER,
            "method": "card",
            "used": False,
            "card": {"network": "MasterCard", "last4": "4444", "expiry_month": 1, "expiry_year": 2031},
        },
        {
            "id": "token_used123",
            "customer_id": CUSTOMER,
            "method": "card",
            "used": True,
            "card": {"network": "Visa", "last4": "1111", "expiry_month": 1, "expiry_year": 2031},
        },
        {"id": "token_upi123", "customer_id": CUSTOMER, "method": "upi"},
        {"id": "not_a_token", "method": "card"},
    ]

    cards = wallet.refresh_cards()

    assert {card["id"] for card in cards} == {
        "pm_rzpvisa4242",
        "pm_rzpmc4444",
    }
    assert transport.listings[0]["customer_id"] == CUSTOMER
    assert transport.listings[0]["key_secret"] == TEST_SECRET


# -- orders and charges ----------------------------------------------------


def test_propose_rejects_a_currency_razorpay_cannot_settle() -> None:
    wallet = make_wallet()
    assert "usd" in SUPPORTED_CURRENCIES
    with pytest.raises(WalletError) as caught:
        wallet.propose(1000, currency="jpy")
    assert caught.value.code == "invalid_currency"
    assert wallet.ledger.list_cards() == []


def test_approve_charges_once_with_the_order_key(transport: FakeTransport) -> None:
    wallet = make_wallet(transport=transport)
    add_card(wallet)
    proposal, _ = wallet.propose(1500, description="Coffee")

    paid, idempotent = wallet.approve(proposal.id)

    assert idempotent is False
    assert paid.status == "paid"
    assert paid.receipt is not None
    assert paid.receipt.payment_intent_id.startswith("pi_rzp")
    assert paid.receipt.amount_cents == 1500

    assert len(transport.orders) == 1
    assert transport.orders[0]["amount_cents"] == 1500
    assert transport.orders[0]["currency"] == "usd"
    assert transport.orders[0]["idempotency_key"] == f"kelp-order-{proposal.id}-order"
    assert transport.orders[0]["notes"]["source"] == "kelp-wallet"

    assert len(transport.payments) == 1
    payment = transport.payments[0]
    assert payment["idempotency_key"] == f"kelp-order-{proposal.id}"
    assert payment["token_id"] == TOKEN
    assert payment["customer_id"] == CUSTOMER
    assert payment["amount_cents"] == 1500
    assert payment["currency"] == "usd"
    assert payment["description"] == "Coffee"
    assert payment["key_id"] == KEY_ID
    assert payment["key_secret"] == TEST_SECRET
    assert wallet.limits("usd")["spent_cents"] == 1500


def test_repeat_approval_is_idempotent(transport: FakeTransport) -> None:
    wallet = make_wallet(transport=transport)
    add_card(wallet)
    proposal, _ = wallet.propose(1500)

    first, _ = wallet.approve(proposal.id)
    second, idempotent = wallet.approve(proposal.id)

    assert idempotent is True
    assert second.receipt == first.receipt
    assert len(transport.payments) == 1
    assert wallet.limits("usd")["spent_cents"] == 1500


def test_deny_never_reaches_razorpay(transport: FakeTransport) -> None:
    wallet = make_wallet(transport=transport)
    add_card(wallet)
    proposal, _ = wallet.propose(1500)

    denied, _ = wallet.approve(proposal.id, decision="deny")

    assert denied.status == "denied"
    assert transport.orders == []
    assert transport.payments == []


def test_retryable_transport_error_keeps_the_order_resumable(
    transport: FakeTransport,
) -> None:
    transport.payment_error = RazorpayGatewayError(
        "Can't reach the payment processor right now.", retryable=True
    )
    wallet = make_wallet(transport=transport)
    add_card(wallet)
    proposal, _ = wallet.propose(1000)

    with pytest.raises(WalletError) as caught:
        wallet.approve(proposal.id)
    assert caught.value.code == "payment_failed"
    assert caught.value.retryable is True
    assert caught.value.details["idempotency_key"] == f"kelp-order-{proposal.id}"

    stored = wallet.ledger.get_proposal(proposal.id)
    assert stored.status == "approved"  # outcome unknown, reservation kept
    assert stored.receipt is None
    limits = wallet.limits("usd")
    assert limits["spent_cents"] == 0
    assert limits["reserved_cents"] == 1000

    transport.payment_error = None
    paid, idempotent = wallet.approve(proposal.id)

    assert idempotent is True
    assert paid.status == "paid"
    assert len(transport.payments) == 2
    assert {call["idempotency_key"] for call in transport.payments} == {
        f"kelp-order-{proposal.id}"
    }
    assert len(transport._payments) == 1  # noqa: SLF001 - one processor payment
    assert wallet.limits("usd")["spent_cents"] == 1000


def test_definitive_gateway_rejection_fails_the_order(transport: FakeTransport) -> None:
    transport.payment_error = RazorpayGatewayError(
        "Razorpay rejected the request (400).", code="BAD_REQUEST_ERROR"
    )
    wallet = make_wallet(transport=transport)
    add_card(wallet)
    proposal, _ = wallet.propose(1000)

    with pytest.raises(WalletError) as caught:
        wallet.approve(proposal.id)

    assert caught.value.code == "payment_failed"
    assert caught.value.retryable is False
    stored = wallet.ledger.get_proposal(proposal.id)
    assert stored.status == "failed"
    assert stored.receipt is None
    assert wallet.limits("usd")["spent_cents"] == 0


def test_authorised_payment_is_captured_before_it_counts(
    transport: FakeTransport,
) -> None:
    transport.payment_status = "authorized"
    transport.capture_status = "captured"
    wallet = make_wallet(transport=transport)
    add_card(wallet)
    proposal, _ = wallet.propose(1200)

    paid, _ = wallet.approve(proposal.id)

    assert paid.status == "paid"
    assert len(transport.captures) == 1
    assert transport.captures[0]["amount_cents"] == 1200
    assert transport.captures[0]["currency"] == "usd"
    assert transport.captures[0]["key_secret"] == TEST_SECRET
    assert wallet.limits("usd")["spent_cents"] == 1200


def test_still_authorised_after_capture_is_not_a_receipt(
    transport: FakeTransport,
) -> None:
    transport.payment_status = "authorized"
    transport.capture_status = "authorized"
    wallet = make_wallet(transport=transport)
    add_card(wallet)
    proposal, _ = wallet.propose(1200)

    with pytest.raises(WalletError) as caught:
        wallet.approve(proposal.id)

    assert caught.value.code == "payment_failed"
    assert caught.value.retryable is True
    assert wallet.ledger.get_proposal(proposal.id).status == "approved"


def test_failed_processor_payment_is_not_a_receipt(transport: FakeTransport) -> None:
    transport.payment_status = "failed"
    wallet = make_wallet(transport=transport)
    add_card(wallet)
    proposal, _ = wallet.propose(1000)

    with pytest.raises(WalletError) as caught:
        wallet.approve(proposal.id)

    assert caught.value.code == "payment_failed"
    assert caught.value.retryable is False
    assert wallet.ledger.get_proposal(proposal.id).status == "failed"


def test_refunded_processor_payment_releases_the_reservation(
    transport: FakeTransport,
) -> None:
    transport.payment_status = "refunded"
    wallet = make_wallet(transport=transport)
    add_card(wallet)
    proposal, _ = wallet.propose(1000)

    with pytest.raises(WalletError) as caught:
        wallet.approve(proposal.id)

    assert caught.value.retryable is False
    assert wallet.ledger.get_proposal(proposal.id).status == "failed"
    assert wallet.limits("usd")["reserved_cents"] == 0


def test_unrecognised_processor_status_stays_resumable(
    transport: FakeTransport,
) -> None:
    transport.payment_status = "something_new"
    wallet = make_wallet(transport=transport)
    add_card(wallet)
    proposal, _ = wallet.propose(1000)

    with pytest.raises(WalletError) as caught:
        wallet.approve(proposal.id)

    assert caught.value.retryable is True
    assert wallet.ledger.get_proposal(proposal.id).status == "approved"


def test_mismatched_amount_is_not_a_receipt(transport: FakeTransport) -> None:
    transport.payment_amount = 999
    wallet = make_wallet(transport=transport)
    add_card(wallet)
    proposal, _ = wallet.propose(1000)

    with pytest.raises(WalletError) as caught:
        wallet.approve(proposal.id)

    assert caught.value.code == "payment_failed"
    assert caught.value.retryable is False
    assert wallet.ledger.get_proposal(proposal.id).status == "failed"
    assert wallet.limits("usd")["spent_cents"] == 0


def test_caps_are_the_wallet_caps_not_a_razorpay_copy(
    transport: FakeTransport,
) -> None:
    wallet = make_wallet(
        transport=transport, caps=SpendCaps(per_purchase_cents=1000, monthly_cents=1500)
    )

    with pytest.raises(WalletError) as caught:
        wallet.propose(1500)
    assert caught.value.code == "cap_exceeded"

    add_card(wallet)
    first, _ = wallet.propose(1000, idempotency_key="order-cap-1")
    wallet.approve(first.id)
    with pytest.raises(WalletError) as caught:
        wallet.propose(1000, idempotency_key="order-cap-2")
    assert caught.value.code == "monthly_cap_exceeded"
    assert len(transport.payments) == 1


def test_propose_is_idempotent_per_order_key() -> None:
    wallet = make_wallet()
    first, created = wallet.propose(1000, idempotency_key="order-same-1")
    again, created_again = wallet.propose(1000, idempotency_key="order-same-1")

    assert created is True
    assert created_again is False
    assert first.id == again.id


@pytest.mark.parametrize("amount", [0, -1, 1.5, True, "100"])
def test_gateway_rejects_bad_amounts(amount: Any) -> None:
    gateway = RazorpayGateway(key_id=KEY_ID, transport=FakeTransport())
    with pytest.raises(WalletError) as caught:
        gateway.create_payment_intent(
            secret_key=TEST_SECRET,
            amount_cents=amount,
            currency="usd",
            payment_method_id=wallet_payment_method_id(TOKEN),
            customer_id=CUSTOMER,
            description="x",
            idempotency_key="order-bad-amount",
        )
    assert caught.value.code == "invalid_amount"


def test_gateway_rejects_an_unsupported_currency() -> None:
    gateway = RazorpayGateway(key_id=KEY_ID, transport=FakeTransport())
    with pytest.raises(WalletError) as caught:
        gateway.create_payment_intent(
            secret_key=TEST_SECRET,
            amount_cents=100,
            currency="jpy",
            payment_method_id=wallet_payment_method_id(TOKEN),
            customer_id=CUSTOMER,
            description="x",
            idempotency_key="order-bad-currency",
        )
    assert caught.value.code == "invalid_currency"


def test_gateway_rejects_a_bad_idempotency_key() -> None:
    gateway = RazorpayGateway(key_id=KEY_ID, transport=FakeTransport())
    with pytest.raises(WalletError) as caught:
        gateway.create_payment_intent(
            secret_key=TEST_SECRET,
            amount_cents=100,
            currency="usd",
            payment_method_id=wallet_payment_method_id(TOKEN),
            customer_id=CUSTOMER,
            description="x",
            idempotency_key="short",
        )
    assert caught.value.code == "invalid_idempotency_key"


def test_order_key_is_never_the_payment_key(transport: FakeTransport) -> None:
    gateway = RazorpayGateway(key_id=KEY_ID, transport=transport)
    # A maximum-length key leaves no room for a "-order" suffix; the derived
    # key must still be distinct, or Razorpay would answer the payment request
    # with the order it already stored for that key.
    key = "k" * 80

    gateway.create_payment_intent(
        secret_key=TEST_SECRET,
        amount_cents=100,
        currency="usd",
        payment_method_id=wallet_payment_method_id(TOKEN),
        customer_id=CUSTOMER,
        description="x",
        idempotency_key=key,
    )

    assert transport.orders[0]["idempotency_key"] != key
    assert transport.payments[0]["idempotency_key"] == key
    for call in (transport.orders[0], transport.payments[0]):
        assert len(call["idempotency_key"]) <= 80


def test_gateway_rejects_a_malformed_customer_id() -> None:
    gateway = RazorpayGateway(key_id=KEY_ID, transport=FakeTransport())
    with pytest.raises(WalletError) as caught:
        gateway.create_payment_intent(
            secret_key=TEST_SECRET,
            amount_cents=100,
            currency="usd",
            payment_method_id=wallet_payment_method_id(TOKEN),
            customer_id="../../etc",
            description="x",
            idempotency_key="order-bad-customer",
        )
    assert caught.value.code == "invalid_config"


def test_long_description_is_redacted_and_truncated(transport: FakeTransport) -> None:
    gateway = RazorpayGateway(key_id=KEY_ID, transport=transport)
    description = "x" * 300 + " " + PAN

    gateway.create_payment_intent(
        secret_key=TEST_SECRET,
        amount_cents=1000,
        currency="usd",
        payment_method_id=wallet_payment_method_id(TOKEN),
        customer_id=CUSTOMER,
        description=description,
        idempotency_key="order-long-desc",
    )

    sent = transport.payments[0]["description"]
    assert len(sent) <= 200
    assert PAN not in sent
    assert PAN not in json.dumps(transport.payments)


def test_wallet_propose_redacts_a_card_number_in_the_description(
    transport: FakeTransport,
) -> None:
    wallet = make_wallet(transport=transport)
    add_card(wallet)
    proposal, _ = wallet.propose(1000, description=f"see {PAN}")

    wallet.approve(proposal.id)

    assert PAN not in transport.payments[0]["description"]
    assert PAN not in ledger_text(wallet)


# -- redaction -------------------------------------------------------------


def test_redact_razorpay_text_masks_keys_and_cards() -> None:
    cleaned = redact_razorpay_text(f"{KEY_ID} {TEST_SECRET} {PAN} sk_test_abc123")

    assert KEY_ID not in cleaned
    assert PAN not in cleaned
    assert "sk_test_abc123" not in cleaned
    assert "[redacted-card]" in cleaned


def test_redact_razorpay_payload_is_recursive() -> None:
    cleaned = redact_razorpay_payload(
        {
            "key_id": f"{KEY_ID}",
            "card": {"number": PAN, "last4": "4242"},
            "authorization": "Basic abc",
            "nested": [{"note": f"uses {KEY_ID}"}],
        }
    )

    dumped = json.dumps(cleaned)
    assert KEY_ID not in dumped
    assert PAN not in dumped
    assert "Basic abc" not in dumped
    assert cleaned["card"]["last4"] == "4242"


def test_error_text_never_carries_the_key_id() -> None:
    error = RazorpayGatewayError(f"failed with {KEY_ID}")
    assert KEY_ID not in str(error)
    # Credentials are never interpolated into transport messages, so there is
    # no secret to pattern-match here; only recognised key ids are masked.
    assert str(RazorpayGatewayError("Razorpay rejected the request (400).")) == (
        "Razorpay rejected the request (400)."
    )


# -- HTTP transport (stubbed client, still no network) ---------------------


class FakeResponse:
    def __init__(self, status_code: int, body: Mapping[str, Any]) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> Mapping[str, Any]:
        return self._body


class RecordingClient:
    def __init__(self, response: FakeResponse | Exception) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def request(
        self,
        method: str,
        url: str,
        *,
        json: Mapping[str, Any] | None = None,  # noqa: A002 - httpx kwarg
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> FakeResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "json": dict(json or {}),
                "params": dict(params or {}),
                "headers": dict(headers or {}),
            }
        )
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def close(self) -> None:
        self.closed = True


def test_http_transport_uses_basic_auth_and_the_order_key() -> None:
    client = RecordingClient(FakeResponse(200, {"id": "pay_test0001", "status": "captured"}))
    transport = HttpRazorpayTransport(client=client)

    transport.create_payment(
        key_id=KEY_ID,
        key_secret=TEST_SECRET,
        order_id="order_test0001",
        token_id=TOKEN,
        amount_cents=1500,
        currency="usd",
        description="Coffee",
        customer_id=CUSTOMER,
        idempotency_key="kelp-order-ord_123",
    )

    call = client.calls[0]
    expected = base64.b64encode(f"{KEY_ID}:{TEST_SECRET}".encode()).decode()
    assert call["method"] == "POST"
    assert call["url"] == "https://api.razorpay.com/v1/payments"
    assert call["headers"]["Authorization"] == f"Basic {expected}"
    assert call["headers"]["X-Razorpay-Idempotency-Key"] == "kelp-order-ord_123"
    assert call["json"]["amount"] == 1500
    assert call["json"]["token"] == TOKEN
    assert TEST_SECRET not in call["url"]
    assert KEY_ID not in call["url"]


def test_http_transport_uppercases_currency_on_the_wire() -> None:
    """Razorpay rejects the lowercase code: BAD_REQUEST_INVALID_CURRENCY.

    Verified against test mode: ``currency: "inr"`` is a 400, ``"INR"`` is a
    200. The wallet keeps lowercase; only the REST body is uppercased.
    """

    client = RecordingClient(
        FakeResponse(200, {"id": "order_test0001", "amount": 100, "currency": "INR"})
    )
    transport = HttpRazorpayTransport(client=client)

    transport.create_order(
        key_id=KEY_ID,
        key_secret=TEST_SECRET,
        amount_cents=100,
        currency="inr",
        notes={},
        idempotency_key="kelp-order-ord_1-order",
    )
    transport.create_payment(
        key_id=KEY_ID,
        key_secret=TEST_SECRET,
        order_id="order_test0001",
        token_id=TOKEN,
        amount_cents=100,
        currency="inr",
        description="",
        customer_id=CUSTOMER,
        idempotency_key="kelp-order-ord_1",
    )
    transport.capture_payment(
        key_id=KEY_ID,
        key_secret=TEST_SECRET,
        payment_id="pay_test0001",
        amount_cents=100,
        currency="inr",
    )

    assert [call["json"]["currency"] for call in client.calls] == ["INR", "INR", "INR"]
    assert [call["url"] for call in client.calls] == [
        "https://api.razorpay.com/v1/orders",
        "https://api.razorpay.com/v1/payments",
        "https://api.razorpay.com/v1/payments/pay_test0001/capture",
    ]


def test_uppercase_currency_echo_from_razorpay_is_accepted() -> None:
    class UpperCaseEchoTransport(FakeTransport):
        def create_order(self, **kwargs: Any) -> Mapping[str, Any]:
            payload = dict(super().create_order(**kwargs))
            payload["currency"] = str(payload["currency"]).upper()
            return payload

        def create_payment(self, **kwargs: Any) -> Mapping[str, Any]:
            payload = dict(super().create_payment(**kwargs))
            payload["currency"] = str(payload["currency"]).upper()
            return payload

    transport = UpperCaseEchoTransport()
    wallet = make_wallet(transport=transport)
    add_card(wallet)
    proposal, _ = wallet.propose(1500)

    paid, _ = wallet.approve(proposal.id)

    assert paid.status == "paid"
    assert paid.receipt is not None
    assert transport.orders[0]["currency"] == "usd"


def test_http_transport_maps_retryable_and_definitive_failures() -> None:
    error_body = {"error": {"code": "BAD_REQUEST_ERROR", "description": f"{KEY_ID}"}}
    rejected = HttpRazorpayTransport(client=RecordingClient(FakeResponse(400, error_body)))
    with pytest.raises(RazorpayGatewayError) as caught:
        rejected.create_order(
            key_id=KEY_ID,
            key_secret=TEST_SECRET,
            amount_cents=100,
            currency="usd",
            notes={},
            idempotency_key="kelp-order-ord_1-order",
        )
    assert caught.value.retryable is False
    assert caught.value.code == "BAD_REQUEST_ERROR"
    assert KEY_ID not in str(caught.value)

    overloaded = HttpRazorpayTransport(
        client=RecordingClient(FakeResponse(503, {"error": {"code": "SERVER_ERROR"}}))
    )
    with pytest.raises(RazorpayGatewayError) as caught:
        overloaded.fetch_token(key_id=KEY_ID, key_secret=TEST_SECRET, token_id=TOKEN)
    assert caught.value.retryable is True


def test_http_transport_turns_a_timeout_into_a_retryable_error() -> None:
    import httpx

    transport = HttpRazorpayTransport(
        client=RecordingClient(httpx.ConnectTimeout("timed out"))
    )
    with pytest.raises(RazorpayGatewayError) as caught:
        transport.list_tokens(
            key_id=KEY_ID, key_secret=TEST_SECRET, customer_id=CUSTOMER
        )

    assert caught.value.retryable is True
    assert TEST_SECRET not in str(caught.value)


def test_http_transport_validates_ids_before_building_a_path() -> None:
    client = RecordingClient(FakeResponse(200, {}))
    transport = HttpRazorpayTransport(client=client)

    with pytest.raises(RazorpayGatewayError):
        transport.fetch_token(
            key_id=KEY_ID, key_secret=TEST_SECRET, token_id="../../admin"
        )
    assert client.calls == []


def test_http_transport_refuses_half_a_credential_pair() -> None:
    client = RecordingClient(FakeResponse(200, {}))
    transport = HttpRazorpayTransport(client=client)

    with pytest.raises(RazorpayGatewayError):
        transport.fetch_token(key_id=KEY_ID, key_secret="", token_id=TOKEN)
    assert client.calls == []


def test_transport_close_releases_the_client() -> None:
    client = RecordingClient(FakeResponse(200, {}))
    transport = HttpRazorpayTransport(client=client)

    transport.close()

    assert client.closed is True
    assert transport._client is None  # noqa: SLF001


def test_gateway_never_creates_an_http_client_until_called() -> None:
    gateway = RazorpayGateway(key_id=KEY_ID)
    assert gateway._transport is None  # noqa: SLF001
    gateway.close()  # nothing to close, must not raise

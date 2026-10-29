"""KEL-10 hosted Checkout tests: fake transport only, no network, no real keys.

The fake models the Razorpay guarantees the checkout path relies on:

- an order idempotency key returns the same order instead of a second one;
- fetching a payment returns the provider's record, which is the source of
  truth for amount, currency, order, and status;
- a captured payment can only cover one order.

Credential-shaped literals live only in this file; the source tree is scanned
to make sure none leak into it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

import pytest

from payments.razorpay_checkout import (
    CHECKOUT_CALLBACK_ENV,
    CHECKOUT_NAME_ENV,
    CHECKOUT_PAYMENT_METHOD_ID,
    CHECKOUT_URL_ENV,
    WEBHOOK_SECRET_ENV,
    RazorpayCheckout,
    expected_payment_signature,
    expected_webhook_signature,
    redact_checkout_payload,
    redact_checkout_text,
    verify_payment_signature,
    verify_webhook_signature,
)
from payments.razorpay_gateway import (
    KEY_ID_ENV,
    KEY_SECRET_ENV,
    RazorpayConfig,
    RazorpayGateway,
    RazorpayGatewayError,
    RazorpayWallet,
)
from payments.stripe_wallet import WalletNotConfigured
from payments.wallet import Card, SpendCaps, WalletError, WalletLedger

KEY_ID = "rzp_test_abc123"
TEST_SECRET = "razorpaysecret123"
WEBHOOK_SECRET = "webhooksecret123"
SOURCE_ROOT = Path(__file__).resolve().parents[1]
_KEY_LITERAL = re.compile(r"rzp_(?:live|test)_[A-Za-z0-9]{8,}")


class CheckoutTransport:
    """A Razorpay-shaped fake. Records calls; never networks."""

    def __init__(self) -> None:
        self.orders: list[dict[str, Any]] = []
        self.payments: list[dict[str, Any]] = []
        self.fetch_calls: list[dict[str, Any]] = []
        self.captures: list[dict[str, Any]] = []
        self.order_error: Exception | None = None
        self.fetch_error: Exception | None = None
        self.capture_status = "captured"
        self.payment_status = "captured"
        self.payment_amount: int | None = None
        self.payment_currency: str | None = None
        self.payment_order_id: str | None = None
        self.payment_last4: str | None = "4242"
        # Optional interleaving hook: runs after the local reservation but
        # before the provider order exists, i.e. inside the race window.
        self.on_order: Any = None
        self._orders: dict[str, Mapping[str, Any]] = {}
        self._by_order: dict[str, Mapping[str, Any]] = {}
        self._seq = 0

    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}test{self._seq:04d}"

    def create_order(
        self,
        *,
        key_id: str,
        key_secret: str,
        amount_cents: int,
        currency: str,
        notes: Mapping[str, str],
        idempotency_key: str,
        receipt: str = "",
    ) -> Mapping[str, Any]:
        self.orders.append(
            {
                "key_id": key_id,
                "key_secret": key_secret,
                "amount_cents": amount_cents,
                "currency": currency,
                "notes": dict(notes),
                "idempotency_key": idempotency_key,
                "receipt": receipt,
            }
        )
        if self.on_order is not None:
            self.on_order()
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
            "receipt": receipt,
            "status": "created",
        }
        self._orders[idempotency_key] = order
        self._by_order[order["id"]] = order
        return order

    def fetch_payment(
        self, *, key_id: str, key_secret: str, payment_id: str
    ) -> Mapping[str, Any]:
        self.fetch_calls.append(
            {"key_id": key_id, "key_secret": key_secret, "payment_id": payment_id}
        )
        if self.fetch_error is not None:
            raise self.fetch_error
        order = next(reversed(self._by_order.values()), None)
        order_id = self.payment_order_id or (order["id"] if order else "order_x")
        amount = (
            self.payment_amount
            if self.payment_amount is not None
            else int(order["amount"]) if order else 0
        )
        currency = (
            self.payment_currency
            if self.payment_currency is not None
            else str(order["currency"]) if order else "usd"
        )
        payload: dict[str, Any] = {
            "id": payment_id,
            "entity": "payment",
            "order_id": order_id,
            "amount": amount,
            "currency": currency,
            "status": self.payment_status,
        }
        if self.payment_last4 is not None:
            payload["card"] = {"last4": self.payment_last4, "network": "Visa"}
        return payload

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
def transport() -> CheckoutTransport:
    return CheckoutTransport()


def make_wallet(
    *,
    transport: CheckoutTransport | None = None,
    config: RazorpayConfig | None = None,
    caps: SpendCaps | None = None,
    ledger: WalletLedger | None = None,
) -> RazorpayWallet:
    return RazorpayWallet(
        config=config or RazorpayConfig(key_id=KEY_ID, secret_key=TEST_SECRET),
        caps=caps or SpendCaps(per_purchase_cents=5000, monthly_cents=20_000),
        ledger=ledger or WalletLedger(":memory:"),
        gateway=RazorpayGateway(
            key_id=KEY_ID, transport=transport or CheckoutTransport()
        ),
        customer_id="",
    )


def make_checkout(
    *,
    transport: CheckoutTransport | None = None,
    webhook_secret: str = WEBHOOK_SECRET,
    wallet: RazorpayWallet | None = None,
) -> RazorpayCheckout:
    return RazorpayCheckout(
        wallet=wallet or make_wallet(transport=transport),
        webhook_secret=webhook_secret,
        checkout_name="Kelp",
        callback_url="https://example.test/wallet/return",
    )


def create_order(
    checkout: RazorpayCheckout,
    *,
    amount: int = 1500,
    key: str = "checkout-key-1",
    description: str = "Coffee",
) -> dict[str, Any]:
    return checkout.create(
        amount, description=description, idempotency_key=key
    )


def signature_for(order_id: str, payment_id: str) -> str:
    return expected_payment_signature(TEST_SECRET, order_id, payment_id)


def ledger_text(wallet: RazorpayWallet) -> str:
    return "\n".join(wallet.ledger._conn.iterdump())  # noqa: SLF001


def webhook_event(
    order_id: str,
    payment_id: str,
    *,
    amount: int = 1500,
    currency: str = "usd",
    status: str = "captured",
    event: str = "payment.captured",
) -> dict[str, Any]:
    return {
        "event": event,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "amount": amount,
                    "currency": currency,
                    "status": status,
                }
            }
        },
    }


# -- order creation --------------------------------------------------------


def test_create_sends_amount_currency_and_receipt(transport: CheckoutTransport) -> None:
    checkout = make_checkout(transport=transport)

    result = create_order(checkout, amount=1750)

    assert result["idempotent"] is False
    assert result["proposal"]["status"] == "approved"
    assert result["checkout"]["order_id"] == "order_test0001"
    assert result["checkout"]["amount_cents"] == 1750
    assert result["checkout"]["currency"] == "usd"
    assert result["checkout"]["receipt"] == result["proposal"]["id"]
    assert result["checkout"]["receipt"].startswith("ord_")
    assert result["checkout"]["key_id"] == KEY_ID
    assert result["checkout"]["callback_url"] == "https://example.test/wallet/return"
    assert result["checkout"]["checkout_url"] is None

    sent = transport.orders[0]
    assert sent["amount_cents"] == 1750
    assert sent["currency"] == "usd"
    assert sent["receipt"] == result["proposal"]["id"]
    assert sent["notes"]["source"] == "kelp-checkout"
    assert sent["idempotency_key"].startswith("kelp-order-")
    assert sent["idempotency_key"].endswith("-checkout")

    # Creating the order reserves the amount, so it cannot be spent twice.
    limits = checkout.wallet.limits("usd")
    assert limits["reserved_cents"] == 1750
    assert limits["spent_cents"] == 0


def test_create_is_idempotent_per_order_key(transport: CheckoutTransport) -> None:
    checkout = make_checkout(transport=transport)

    first = create_order(checkout, key="checkout-key-1")
    again = create_order(checkout, key="checkout-key-1")

    assert first["checkout"]["order_id"] == again["checkout"]["order_id"]
    assert again["idempotent"] is True
    assert len(transport.orders) == 1
    assert checkout.wallet.limits("usd")["reserved_cents"] == 1500


def test_create_never_leaks_the_key_secret(transport: CheckoutTransport) -> None:
    checkout = make_checkout(transport=transport)

    result = create_order(checkout)
    blobs = [
        json.dumps(result, default=str),
        repr(checkout),
        str(checkout),
        json.dumps(checkout.status()),
        ledger_text(checkout.wallet),
    ]

    for blob in blobs:
        assert TEST_SECRET not in blob
        assert WEBHOOK_SECRET not in blob


def test_unconfigured_checkout_cannot_create_or_reserve() -> None:
    wallet = RazorpayWallet.from_env({})
    checkout = RazorpayCheckout(wallet=wallet)

    assert checkout.status()["configured"] is False
    with pytest.raises(WalletNotConfigured):
        create_order(checkout)
    # Nothing was written: fail before touching the cap.
    assert wallet.ledger.find_checkout_for_proposal("ord_missing") is None
    assert checkout.wallet.limits("usd")["reserved_cents"] == 0


def test_unconfigured_checkout_has_no_key_in_status() -> None:
    checkout = RazorpayCheckout(wallet=RazorpayWallet.from_env({}))
    dumped = json.dumps(checkout.status())
    assert KEY_ID not in dumped
    assert TEST_SECRET not in dumped


def test_from_env_reads_checkout_configuration() -> None:
    checkout = RazorpayCheckout.from_env(
        {
            KEY_ID_ENV: KEY_ID,
            KEY_SECRET_ENV: TEST_SECRET,
            WEBHOOK_SECRET_ENV: WEBHOOK_SECRET,
            CHECKOUT_NAME_ENV: "Kelp Test",
            CHECKOUT_CALLBACK_ENV: "https://example.test/done",
            CHECKOUT_URL_ENV: "https://example.test/pay",
        }
    )

    assert checkout.status()["webhook_configured"] is True
    assert checkout.checkout_name == "Kelp Test"
    assert checkout.callback_url == "https://example.test/done"
    assert checkout.checkout_url == "https://example.test/pay"


@pytest.mark.parametrize(
    "value",
    ["http://example.test/return", "javascript:alert(1)", "not a url"],
)
def test_bad_callback_url_is_a_configuration_error(value: str) -> None:
    with pytest.raises(WalletError) as caught:
        RazorpayCheckout(wallet=make_wallet(), callback_url=value)
    assert caught.value.code == "invalid_config"


def test_retryable_order_error_keeps_the_reservation(transport: CheckoutTransport) -> None:
    transport.order_error = RazorpayGatewayError(
        "Can't reach the payment processor right now.", retryable=True
    )
    checkout = make_checkout(transport=transport)

    with pytest.raises(WalletError) as caught:
        create_order(checkout)
    assert caught.value.code == "payment_failed"
    assert caught.value.retryable is True
    stored = checkout.wallet.ledger.get_proposal(
        caught.value.details["proposal_id"]
    )
    assert stored is not None and stored.status == "approved"
    assert checkout.wallet.limits("usd")["reserved_cents"] == 1500

    transport.order_error = None
    result = create_order(checkout)
    assert result["checkout"]["order_id"] == "order_test0001"
    assert len(transport.orders) == 2


def test_definitive_order_error_fails_the_order(transport: CheckoutTransport) -> None:
    transport.order_error = RazorpayGatewayError(
        "Razorpay rejected the request (400).", code="BAD_REQUEST_ERROR"
    )
    checkout = make_checkout(transport=transport)

    with pytest.raises(WalletError) as caught:
        create_order(checkout)
    assert caught.value.code == "payment_failed"
    assert caught.value.retryable is False
    assert checkout.wallet.limits("usd")["reserved_cents"] == 0


def test_a_checkout_order_cannot_also_be_charged_to_a_saved_card(
    transport: CheckoutTransport,
) -> None:
    checkout = make_checkout(transport=transport)
    created = create_order(checkout)
    proposal_id = created["proposal"]["id"]
    checkout.wallet.ledger.add_card(
        Card("pm_rzpvisa4242", "visa", "4242", 12, 2030, True)
    )

    with pytest.raises(WalletError) as caught:
        checkout.wallet.approve(proposal_id, payment_method_id="pm_rzpvisa4242")

    assert caught.value.code == "already_decided"
    assert transport.payments == []


def test_the_reservation_is_the_checkout_claim(transport: CheckoutTransport) -> None:
    checkout = make_checkout(transport=transport)
    wallet = checkout.wallet
    claims: list[tuple[str | None, dict[str, Any] | None]] = []

    def inspect_claim() -> None:
        proposal = wallet.ledger.find_proposal_by_key("race-key-1")
        assert proposal is not None
        claims.append(
            (
                proposal.payment_method_id,
                wallet.ledger.find_checkout_for_proposal(proposal.id),
            )
        )

    transport.on_order = inspect_claim
    create_order(checkout, key="race-key-1")

    # The claim is written by the reservation transaction, before the provider
    # order is registered, and it is not a saved-card id.
    assert len(claims) == 1
    claimed_id, row = claims[0]
    assert claimed_id == CHECKOUT_PAYMENT_METHOD_ID
    assert row is None


def test_a_saved_card_approval_racing_a_checkout_create_is_refused(
    transport: CheckoutTransport,
) -> None:
    wallet = make_wallet(transport=transport)
    wallet.ledger.add_card(Card("pm_rzpvisa4242", "visa", "4242", 12, 2030, True))
    checkout = make_checkout(transport=transport, wallet=wallet)
    outcomes: list[str] = []

    def race_approval() -> None:
        # The checkout create has reserved and claimed the order but has not
        # registered the provider order yet: exactly the window where a
        # saved-card approval used to see a payable, unowned order.
        proposal = wallet.ledger.find_proposal_by_key("race-key-1")
        assert proposal is not None
        assert wallet.ledger.find_checkout_for_proposal(proposal.id) is None
        try:
            wallet.approve(proposal.id, payment_method_id="pm_rzpvisa4242")
        except WalletError as exc:
            outcomes.append(exc.code)
        else:  # pragma: no cover - the fix makes this unchargeable
            outcomes.append("charged")

    transport.on_order = race_approval
    created = create_order(checkout, key="race-key-1")

    assert outcomes == ["already_decided"]
    assert transport.payments == []
    assert created["proposal"]["status"] == "approved"
    assert created["proposal"].get("receipt") is None
    assert wallet.limits("usd")["reserved_cents"] == 1500


def test_an_order_reserved_for_a_card_cannot_start_a_checkout(
    transport: CheckoutTransport,
) -> None:
    wallet = make_wallet(transport=transport)
    checkout = make_checkout(transport=transport, wallet=wallet)
    proposal, _ = wallet.propose(1500, idempotency_key="card-key-1")
    since, reserved_since, sweep_before = wallet._windows()  # noqa: SLF001
    approved, outcome = wallet.ledger.decide(
        proposal.id,
        decision="approve",
        payment_method_id="pm_rzpvisa4242",
        monthly_cap_cents=wallet.caps.monthly_cents,
        monthly_since=since,
        approved_since=reserved_since,
        expire_before=sweep_before,
    )
    assert outcome == "approved" and approved is not None

    with pytest.raises(WalletError) as caught:
        checkout.create(1500, idempotency_key="card-key-1")

    assert caught.value.code == "already_decided"
    assert transport.orders == []


def test_a_definitive_error_does_not_fail_a_registered_checkout(
    transport: CheckoutTransport,
) -> None:
    wallet = make_wallet(transport=transport)
    checkout = make_checkout(transport=transport, wallet=wallet)

    def concurrent_registration() -> None:
        # A concurrent create registered its provider order first; this
        # attempt then got a definitive error from Razorpay.
        proposal = wallet.ledger.find_proposal_by_key("race-key-2")
        assert proposal is not None
        wallet.ledger.register_checkout_order(
            provider="razorpay",
            provider_order_id="order_race0001",
            proposal_id=proposal.id,
            receipt=proposal.id,
        )

    transport.on_order = concurrent_registration
    transport.order_error = RazorpayGatewayError(
        "Razorpay rejected the request (400).", code="BAD_REQUEST_ERROR"
    )

    with pytest.raises(WalletError) as caught:
        create_order(checkout, key="race-key-2")

    assert caught.value.code == "payment_failed"
    assert caught.value.retryable is False
    stored = wallet.ledger.find_proposal_by_key("race-key-2")
    assert stored is not None
    # The registered checkout order is still payable: its reservation and its
    # local row were not retired by this attempt's error.
    assert stored.status == "approved"
    assert wallet.limits("usd")["reserved_cents"] == 1500


def test_create_rejects_an_unsupported_currency(transport: CheckoutTransport) -> None:
    checkout = make_checkout(transport=transport)
    with pytest.raises(WalletError) as caught:
        checkout.create(1000, currency="jpy")
    assert caught.value.code == "invalid_currency"
    assert transport.orders == []


def test_create_redacts_a_card_number_in_the_description(
    transport: CheckoutTransport,
) -> None:
    checkout = make_checkout(transport=transport)
    pan = "4242424242424242"

    result = create_order(checkout, description=f"see {pan}")

    assert pan not in result["checkout"]["description"]
    assert pan not in json.dumps(transport.orders)
    assert pan not in ledger_text(checkout.wallet)


# -- signature verification ------------------------------------------------


def test_payment_signature_is_hmac_order_pipe_payment() -> None:
    signature = signature_for("order_test0001", "pay_test0002")
    assert re.fullmatch(r"[0-9a-f]{64}", signature)
    assert verify_payment_signature(
        TEST_SECRET, "order_test0001", "pay_test0002", signature
    )
    assert not verify_payment_signature(
        TEST_SECRET, "order_test0001", "pay_test0003", signature
    )
    # Hex comparison is case-insensitive; the digest is still constant-time.
    assert verify_payment_signature(
        TEST_SECRET, "order_test0001", "pay_test0002", signature.upper()
    )
    assert verify_payment_signature(
        TEST_SECRET, "order_test0001", "pay_test0002", "0" * 64
    ) is False


@pytest.mark.parametrize("value", ["ü" * 64, "abc", "", "0" * 63])
def test_signature_verification_rejects_non_hex_input(value: str) -> None:
    assert (
        verify_payment_signature(
            TEST_SECRET, "order_test0001", "pay_test0002", value
        )
        is False
    )
    assert (
        verify_webhook_signature(WEBHOOK_SECRET, b"{}", value) is False
    )


def test_webhook_signature_is_hmac_over_the_raw_body() -> None:
    body = b'{"event":"payment.captured"}'
    signature = expected_webhook_signature(WEBHOOK_SECRET, body)

    assert verify_webhook_signature(WEBHOOK_SECRET, body, signature)
    assert not verify_webhook_signature(WEBHOOK_SECRET, body + b" ", signature)
    assert not verify_webhook_signature("other", body, signature)
    assert not verify_webhook_signature(WEBHOOK_SECRET, body, None)


def test_checkout_redaction_masks_keys_and_signatures() -> None:
    cleaned = redact_checkout_payload(
        {
            "key_id": KEY_ID,
            "razorpay_signature": "a" * 64,
            "nested": {"note": f"uses {KEY_ID}"},
        }
    )

    dumped = json.dumps(cleaned)
    assert KEY_ID not in dumped
    assert "a" * 64 not in dumped
    assert redact_checkout_text(f"{KEY_ID} {TEST_SECRET}").find(KEY_ID) == -1


def test_no_hardcoded_razorpay_keys_in_checkout_source() -> None:
    offenders = [
        str(path)
        for path in (SOURCE_ROOT / "payments").glob("*.py")
        if _KEY_LITERAL.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


# -- confirmation ----------------------------------------------------------


def test_valid_signature_settles_once(transport: CheckoutTransport) -> None:
    checkout = make_checkout(transport=transport)
    created = create_order(checkout)
    order_id = created["checkout"]["order_id"]
    payment_id = "pay_test0001"

    result = checkout.confirm(
        razorpay_order_id=order_id,
        razorpay_payment_id=payment_id,
        razorpay_signature=signature_for(order_id, payment_id),
    )

    assert result["idempotent"] is False
    assert result["proposal"]["status"] == "paid"
    assert result["receipt"] is not None
    assert result["receipt"]["amount_cents"] == 1500
    assert result["receipt"]["currency"] == "usd"
    assert result["receipt"]["last4"] == "4242"
    assert result["receipt"]["order_id"] == created["proposal"]["id"]
    assert result["receipt"]["payment_intent_id"].startswith("pi_rzp")
    assert checkout.wallet.limits("usd")["spent_cents"] == 1500
    assert checkout.wallet.limits("usd")["reserved_cents"] == 0
    assert transport.fetch_calls[0]["key_secret"] == TEST_SECRET
    assert TEST_SECRET not in ledger_text(checkout.wallet)


def test_invalid_signature_rejects_before_fetching(
    transport: CheckoutTransport,
) -> None:
    checkout = make_checkout(transport=transport)
    created = create_order(checkout)
    order_id = created["checkout"]["order_id"]

    with pytest.raises(WalletError) as caught:
        checkout.confirm(
            razorpay_order_id=order_id,
            razorpay_payment_id="pay_test0001",
            razorpay_signature="0" * 64,
        )

    assert caught.value.code == "signature_invalid"
    assert caught.value.status == 401
    assert transport.fetch_calls == []
    stored = checkout.wallet.ledger.get_proposal(created["proposal"]["id"])
    assert stored.status == "approved"
    assert stored.receipt is None
    assert checkout.wallet.limits("usd")["spent_cents"] == 0
    assert checkout.wallet.limits("usd")["reserved_cents"] == 1500


def test_replayed_callback_returns_the_same_receipt(
    transport: CheckoutTransport,
) -> None:
    checkout = make_checkout(transport=transport)
    created = create_order(checkout)
    order_id = created["checkout"]["order_id"]
    payment_id = "pay_test0001"
    signature = signature_for(order_id, payment_id)

    first = checkout.confirm(
        razorpay_order_id=order_id,
        razorpay_payment_id=payment_id,
        razorpay_signature=signature,
    )
    second = checkout.confirm(
        razorpay_order_id=order_id,
        razorpay_payment_id=payment_id,
        razorpay_signature=signature,
    )

    assert second["idempotent"] is True
    assert second["receipt"] == first["receipt"]
    assert len(transport.fetch_calls) == 1
    assert checkout.wallet.limits("usd")["spent_cents"] == 1500


def test_a_different_payment_cannot_settle_a_paid_order(
    transport: CheckoutTransport,
) -> None:
    checkout = make_checkout(transport=transport)
    created = create_order(checkout)
    order_id = created["checkout"]["order_id"]
    first_payment = "pay_test0001"
    checkout.confirm(
        razorpay_order_id=order_id,
        razorpay_payment_id=first_payment,
        razorpay_signature=signature_for(order_id, first_payment),
    )

    with pytest.raises(WalletError) as caught:
        checkout.confirm(
            razorpay_order_id=order_id,
            razorpay_payment_id="pay_test0002",
            razorpay_signature=signature_for(order_id, "pay_test0002"),
        )

    assert caught.value.code == "already_decided"
    assert checkout.wallet.limits("usd")["spent_cents"] == 1500


def test_a_paid_order_without_a_checkout_payment_is_never_a_receipt(
    transport: CheckoutTransport,
) -> None:
    checkout = make_checkout(transport=transport)
    created = create_order(checkout)
    proposal_id = created["proposal"]["id"]
    order_id = created["checkout"]["order_id"]
    # The proposal was settled by some other payer (a saved card, or a row
    # written before a crash) while the checkout row still has no provider
    # payment id. That money does not belong to this callback.
    checkout.wallet.ledger.mark_paid(
        proposal_id, payment_intent_id="pi_rzp_other", card_last4="1111"
    )
    payment_id = "pay_test0001"

    with pytest.raises(WalletError) as caught:
        checkout.confirm(
            razorpay_order_id=order_id,
            razorpay_payment_id=payment_id,
            razorpay_signature=signature_for(order_id, payment_id),
        )

    assert caught.value.code == "already_decided"
    assert caught.value.status == 409
    assert transport.fetch_calls == []
    stored = checkout.wallet.ledger.get_proposal(proposal_id)
    assert stored is not None
    assert stored.receipt is not None  # the other settlement's receipt is untouched
    assert stored.receipt.payment_intent_id == "pi_rzp_other"
    assert checkout.wallet.limits("usd")["reserved_cents"] == 0


def test_amount_mismatch_is_not_a_receipt(transport: CheckoutTransport) -> None:
    transport.payment_amount = 999
    checkout = make_checkout(transport=transport)
    created = create_order(checkout)
    order_id = created["checkout"]["order_id"]
    payment_id = "pay_test0001"

    with pytest.raises(WalletError) as caught:
        checkout.confirm(
            razorpay_order_id=order_id,
            razorpay_payment_id=payment_id,
            razorpay_signature=signature_for(order_id, payment_id),
        )

    assert caught.value.code == "payment_mismatch"
    assert caught.value.retryable is False
    stored = checkout.wallet.ledger.get_proposal(created["proposal"]["id"])
    assert stored.status == "failed"
    assert stored.receipt is None
    assert checkout.wallet.limits("usd")["spent_cents"] == 0
    assert checkout.wallet.limits("usd")["reserved_cents"] == 0


@pytest.mark.parametrize(
    "attribute,value",
    [
        ("payment_order_id", "order_other9999"),
        ("payment_currency", "eur"),
        ("payment_last4", None),
    ],
)
def test_order_currency_and_card_metadata_are_checked(
    transport: CheckoutTransport, attribute: str, value: Any
) -> None:
    setattr(transport, attribute, value)
    checkout = make_checkout(transport=transport)
    created = create_order(checkout)
    order_id = created["checkout"]["order_id"]
    payment_id = "pay_test0001"

    if attribute == "payment_last4":
        # A payment without card metadata still settles; the receipt simply
        # carries no last4 instead of inventing one.
        result = checkout.confirm(
            razorpay_order_id=order_id,
            razorpay_payment_id=payment_id,
            razorpay_signature=signature_for(order_id, payment_id),
        )
        assert result["receipt"]["last4"] is None
        return

    with pytest.raises(WalletError) as caught:
        checkout.confirm(
            razorpay_order_id=order_id,
            razorpay_payment_id=payment_id,
            razorpay_signature=signature_for(order_id, payment_id),
        )
    assert caught.value.code == "payment_mismatch"
    assert checkout.wallet.ledger.get_proposal(created["proposal"]["id"]).status == (
        "failed"
    )


def test_authorized_payment_is_captured_before_it_counts(
    transport: CheckoutTransport,
) -> None:
    transport.payment_status = "authorized"
    transport.capture_status = "captured"
    checkout = make_checkout(transport=transport)
    created = create_order(checkout)
    order_id = created["checkout"]["order_id"]
    payment_id = "pay_test0001"

    result = checkout.confirm(
        razorpay_order_id=order_id,
        razorpay_payment_id=payment_id,
        razorpay_signature=signature_for(order_id, payment_id),
    )

    assert result["proposal"]["status"] == "paid"
    assert len(transport.captures) == 1
    assert transport.captures[0]["amount_cents"] == 1500
    assert transport.captures[0]["currency"] == "usd"


def test_a_late_payment_after_the_reservation_expired_still_settles(
    transport: CheckoutTransport,
) -> None:
    checkout = make_checkout(transport=transport)
    created = create_order(checkout)
    order_id = created["checkout"]["order_id"]
    payment_id = "pay_test0001"
    # Simulate the sweep: the approval window lapsed before the browser paid.
    checkout.wallet.ledger.expire_stale_approvals(
        before="9999-01-01T00:00:00+00:00"
    )
    assert checkout.wallet.ledger.get_proposal(created["proposal"]["id"]).status == (
        "failed"
    )

    result = checkout.confirm(
        razorpay_order_id=order_id,
        razorpay_payment_id=payment_id,
        razorpay_signature=signature_for(order_id, payment_id),
    )

    # The provider holds the money, so the receipt is recorded rather than
    # rejecting a real charge because a timer fired first.
    assert result["proposal"]["status"] == "paid"
    assert result["proposal"].get("reason") is None
    assert checkout.wallet.limits("usd")["spent_cents"] == 1500


def test_pending_payment_stays_resumable(transport: CheckoutTransport) -> None:
    transport.payment_status = "processing"
    checkout = make_checkout(transport=transport)
    created = create_order(checkout)
    order_id = created["checkout"]["order_id"]
    payment_id = "pay_test0001"

    with pytest.raises(WalletError) as caught:
        checkout.confirm(
            razorpay_order_id=order_id,
            razorpay_payment_id=payment_id,
            razorpay_signature=signature_for(order_id, payment_id),
        )

    assert caught.value.code == "payment_failed"
    assert caught.value.retryable is True
    stored = checkout.wallet.ledger.get_proposal(created["proposal"]["id"])
    assert stored.status == "approved"
    assert checkout.wallet.limits("usd")["reserved_cents"] == 1500


def test_failed_payment_releases_the_reservation(
    transport: CheckoutTransport,
) -> None:
    transport.payment_status = "failed"
    checkout = make_checkout(transport=transport)
    created = create_order(checkout)
    order_id = created["checkout"]["order_id"]
    payment_id = "pay_test0001"

    with pytest.raises(WalletError) as caught:
        checkout.confirm(
            razorpay_order_id=order_id,
            razorpay_payment_id=payment_id,
            razorpay_signature=signature_for(order_id, payment_id),
        )

    assert caught.value.code == "payment_failed"
    assert caught.value.retryable is False
    assert checkout.wallet.ledger.get_proposal(created["proposal"]["id"]).status == (
        "failed"
    )
    assert checkout.wallet.limits("usd")["reserved_cents"] == 0


def test_unknown_order_is_not_settled(transport: CheckoutTransport) -> None:
    checkout = make_checkout(transport=transport)
    with pytest.raises(WalletError) as caught:
        checkout.confirm(
            razorpay_order_id="order_unknown1",
            razorpay_payment_id="pay_test0001",
            razorpay_signature=signature_for("order_unknown1", "pay_test0001"),
        )
    assert caught.value.code == "unknown_proposal"
    assert transport.fetch_calls == []


# -- webhooks --------------------------------------------------------------


def sign_webhook(event: Mapping[str, Any], secret: str = WEBHOOK_SECRET) -> tuple[bytes, str]:
    body = json.dumps(event).encode("utf-8")
    return body, expected_webhook_signature(secret, body)


def test_webhook_settles_with_a_valid_body_signature(
    transport: CheckoutTransport,
) -> None:
    checkout = make_checkout(transport=transport)
    created = create_order(checkout)
    order_id = created["checkout"]["order_id"]
    payment_id = "pay_test0001"
    body, signature = sign_webhook(webhook_event(order_id, payment_id))

    result = checkout.handle_webhook(body, signature=signature)

    assert result["handled"] is True
    assert result["proposal"]["status"] == "paid"
    assert result["receipt"]["amount_cents"] == 1500
    assert checkout.wallet.limits("usd")["spent_cents"] == 1500


def test_webhook_replay_is_idempotent(transport: CheckoutTransport) -> None:
    checkout = make_checkout(transport=transport)
    created = create_order(checkout)
    order_id = created["checkout"]["order_id"]
    payment_id = "pay_test0001"
    body, signature = sign_webhook(webhook_event(order_id, payment_id))

    first = checkout.handle_webhook(body, signature=signature)
    second = checkout.handle_webhook(body, signature=signature)

    assert second["idempotent"] is True
    assert second["receipt"] == first["receipt"]
    assert len(transport.fetch_calls) == 1
    assert checkout.wallet.limits("usd")["spent_cents"] == 1500


def test_webhook_with_a_bad_signature_changes_nothing(
    transport: CheckoutTransport,
) -> None:
    checkout = make_checkout(transport=transport)
    created = create_order(checkout)
    order_id = created["checkout"]["order_id"]
    body, _ = sign_webhook(webhook_event(order_id, "pay_test0001"))

    with pytest.raises(WalletError) as caught:
        checkout.handle_webhook(body, signature="0" * 64)

    assert caught.value.code == "signature_invalid"
    assert caught.value.status == 401
    assert transport.fetch_calls == []
    assert checkout.wallet.ledger.get_proposal(created["proposal"]["id"]).status == (
        "approved"
    )


def test_webhook_without_a_configured_secret_is_refused(
    transport: CheckoutTransport,
) -> None:
    checkout = make_checkout(transport=transport, webhook_secret="")
    created = create_order(checkout)
    order_id = created["checkout"]["order_id"]
    body, signature = sign_webhook(webhook_event(order_id, "pay_test0001"))

    with pytest.raises(WalletError) as caught:
        checkout.handle_webhook(body, signature=signature)
    assert caught.value.code == "not_configured"
    assert caught.value.status == 503


def test_webhook_can_use_a_payment_signature_when_no_webhook_secret_is_set(
    transport: CheckoutTransport,
) -> None:
    checkout = make_checkout(transport=transport, webhook_secret="")
    created = create_order(checkout)
    order_id = created["checkout"]["order_id"]
    payment_id = "pay_test0001"
    event = webhook_event(order_id, payment_id)
    event["razorpay_signature"] = signature_for(order_id, payment_id)
    body = json.dumps(event).encode("utf-8")

    result = checkout.handle_webhook(body)

    assert result["handled"] is True
    assert result["proposal"]["status"] == "paid"


def test_webhook_without_any_signature_is_refused(
    transport: CheckoutTransport,
) -> None:
    checkout = make_checkout(transport=transport, webhook_secret="")
    created = create_order(checkout)
    order_id = created["checkout"]["order_id"]
    body = json.dumps(webhook_event(order_id, "pay_test0001")).encode("utf-8")

    with pytest.raises(WalletError) as caught:
        checkout.handle_webhook(body)
    assert caught.value.code == "signature_invalid"
    assert checkout.wallet.limits("usd")["spent_cents"] == 0


def test_webhook_with_a_configured_secret_ignores_a_body_signature(
    transport: CheckoutTransport,
) -> None:
    checkout = make_checkout(transport=transport)  # webhook secret configured
    created = create_order(checkout)
    order_id = created["checkout"]["order_id"]
    payment_id = "pay_test0001"
    event = webhook_event(order_id, payment_id)
    # A per-payment signature that would be accepted by the no-secret path.
    event["razorpay_signature"] = signature_for(order_id, payment_id)
    body = json.dumps(event).encode("utf-8")

    with pytest.raises(WalletError) as caught:
        checkout.handle_webhook(body)

    assert caught.value.code == "signature_invalid"
    assert caught.value.status == 401
    assert transport.fetch_calls == []
    assert checkout.wallet.limits("usd")["spent_cents"] == 0
    assert checkout.wallet.limits("usd")["reserved_cents"] == 1500


def test_webhook_with_an_empty_signature_header_is_refused(
    transport: CheckoutTransport,
) -> None:
    checkout = make_checkout(transport=transport)
    created = create_order(checkout)
    order_id = created["checkout"]["order_id"]
    payment_id = "pay_test0001"
    event = webhook_event(order_id, payment_id)
    event["razorpay_signature"] = signature_for(order_id, payment_id)
    body = json.dumps(event).encode("utf-8")

    # An empty header is the same as a missing one: the body signature is not
    # a fallback while a webhook secret is configured.
    with pytest.raises(WalletError) as caught:
        checkout.handle_webhook(body, signature="")

    assert caught.value.code == "signature_invalid"
    assert caught.value.status == 401
    assert transport.fetch_calls == []
    assert checkout.wallet.limits("usd")["spent_cents"] == 0


def test_webhook_never_returns_a_receipt_for_a_paid_order_without_a_payment(
    transport: CheckoutTransport,
) -> None:
    checkout = make_checkout(transport=transport)
    created = create_order(checkout)
    proposal_id = created["proposal"]["id"]
    order_id = created["checkout"]["order_id"]
    checkout.wallet.ledger.mark_paid(
        proposal_id, payment_intent_id="pi_rzp_other", card_last4="1111"
    )
    payment_id = "pay_test0001"
    body, signature = sign_webhook(webhook_event(order_id, payment_id))

    with pytest.raises(WalletError) as caught:
        checkout.handle_webhook(body, signature=signature)

    assert caught.value.code == "already_decided"
    assert caught.value.status == 409
    assert transport.fetch_calls == []
    stored = checkout.wallet.ledger.get_proposal(proposal_id)
    assert stored is not None and stored.receipt is not None
    assert stored.receipt.payment_intent_id == "pi_rzp_other"


def test_webhook_for_an_unknown_order_is_acknowledged_but_ignored(
    transport: CheckoutTransport,
) -> None:
    checkout = make_checkout(transport=transport)
    body, signature = sign_webhook(
        webhook_event("order_unknown1", "pay_test0001")
    )

    result = checkout.handle_webhook(body, signature=signature)

    assert result["handled"] is False
    assert result["reason"] == "unknown_order"
    assert transport.fetch_calls == []


def test_unrelated_webhook_is_acknowledged(transport: CheckoutTransport) -> None:
    checkout = make_checkout(transport=transport)
    event = {"event": "refund.created", "payload": {}}
    body, signature = sign_webhook(event)

    result = checkout.handle_webhook(body, signature=signature)

    assert result == {
        "handled": False,
        "event": "refund.created",
        "reason": "ignored",
    }


def test_webhook_amount_mismatch_marks_failed_without_raising(
    transport: CheckoutTransport,
) -> None:
    transport.payment_amount = 999
    checkout = make_checkout(transport=transport)
    created = create_order(checkout)
    order_id = created["checkout"]["order_id"]
    body, signature = sign_webhook(webhook_event(order_id, "pay_test0001"))

    result = checkout.handle_webhook(body, signature=signature)

    assert result["handled"] is True
    assert result["status"] == "failed"
    assert result["error"] == "payment_mismatch"
    assert checkout.wallet.limits("usd")["spent_cents"] == 0
    assert checkout.wallet.limits("usd")["reserved_cents"] == 0

"""KEL-10 wallet tests: local logic only, fake transport, no network.

The Stripe key is still absent, so every test injects ``FakeGateway``. Literal
keys and card numbers live only in this file; source files are scanned for
hardcoded keys by ``tests/test_payments.py`` as well.
"""

from __future__ import annotations

import json
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

from payments.stripe_wallet import STRIPE_SECRET_KEY_ENV, StripeConfig, WalletNotConfigured
from payments.wallet import (
    APPROVAL_EXPIRED_REASON,
    CUSTOMER_ID_ENV,
    DEFAULT_APPROVAL_TTL,
    MONTHLY_CAP_ENV,
    PER_PURCHASE_CAP_ENV,
    Card,
    SpendCaps,
    StripeGatewayError,
    Wallet,
    WalletError,
    WalletLedger,
    card_from_payment_method,
    contains_raw_card_data,
    redact_payload,
    redact_text,
)

TEST_KEY = "sk_test_abc123"
CUSTOMER = "cus_test1234"
PM_ID = "pm_testvisa"
PAN = "4242424242424242"
SOURCE_ROOT = Path(__file__).resolve().parents[1]
_KEY_LITERAL = re.compile(r"sk_(?:live|test)_[A-Za-z0-9]{8,}")


class Clock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class FakeGateway:
    """Records calls and returns canned Stripe-shaped payloads. Never networks.

    Payment intents are keyed by idempotency key, the way Stripe does it: a
    repeated key returns the *same* intent and never creates a second charge.
    ``payments`` keeps every call for assertions about retries.
    """

    def __init__(self) -> None:
        self.setup_intents: list[dict[str, Any]] = []
        self.attachments: list[dict[str, Any]] = []
        self.listings: list[dict[str, Any]] = []
        self.payments: list[dict[str, Any]] = []
        self.intents: dict[str, dict[str, Any]] = {}
        self.attach_result: Mapping[str, Any] | None = None
        self.list_result: list[Mapping[str, Any]] = []
        self.payment_result: Mapping[str, Any] = {"status": "succeeded"}
        self.payment_error: StripeGatewayError | None = None

    def create_setup_intent(
        self,
        *,
        secret_key: str,
        customer_id: str,
        metadata: Mapping[str, str],
    ) -> Mapping[str, Any]:
        self.setup_intents.append(
            {"secret_key": secret_key, "customer_id": customer_id, "metadata": dict(metadata)}
        )
        return {
            "id": "seti_test_123",
            "client_secret": "seti_test_123_secret_abc",
            "status": "requires_payment_method",
        }

    def attach_payment_method(
        self,
        *,
        secret_key: str,
        payment_method_id: str,
        customer_id: str,
    ) -> Mapping[str, Any]:
        self.attachments.append(
            {
                "secret_key": secret_key,
                "payment_method_id": payment_method_id,
                "customer_id": customer_id,
            }
        )
        if self.attach_result is not None:
            return self.attach_result
        return {
            "id": payment_method_id,
            "object": "payment_method",
            "type": "card",
            "card": {
                "brand": "visa",
                "last4": "4242",
                "exp_month": 12,
                "exp_year": 2030,
            },
            "billing_details": {"name": "Someone"},
        }

    def list_payment_methods(
        self,
        *,
        secret_key: str,
        customer_id: str,
    ) -> list[Mapping[str, Any]]:
        self.listings.append({"secret_key": secret_key, "customer_id": customer_id})
        return list(self.list_result)

    def create_payment_intent(
        self,
        *,
        secret_key: str,
        amount_cents: int,
        currency: str,
        payment_method_id: str,
        customer_id: str,
        description: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        self.payments.append(
            {
                "secret_key": secret_key,
                "amount_cents": amount_cents,
                "currency": currency,
                "payment_method_id": payment_method_id,
                "customer_id": customer_id,
                "description": description,
                "idempotency_key": idempotency_key,
            }
        )
        if self.payment_error is not None:
            raise self.payment_error
        existing = self.intents.get(idempotency_key)
        if existing is not None:
            return existing
        intent = {
            "id": f"pi_test_{123 + len(self.intents)}",
            "status": "succeeded",
            "amount": amount_cents,
            "currency": currency,
            **self.payment_result,
        }
        self.intents[idempotency_key] = intent
        return intent


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway()


def make_wallet(
    *,
    tmp_path: Path | None = None,
    gateway: FakeGateway | None = None,
    caps: SpendCaps | None = None,
    clock: Clock | None = None,
    customer_id: str = CUSTOMER,
    approval_ttl: timedelta | None = None,
) -> Wallet:
    config = StripeConfig.from_env({STRIPE_SECRET_KEY_ENV: TEST_KEY})
    ledger = WalletLedger(
        (tmp_path / "wallet.db") if tmp_path is not None else ":memory:",
        now=clock,
    )
    return Wallet(
        config=config,
        caps=caps or SpendCaps(per_purchase_cents=5000, monthly_cents=20_000),
        ledger=ledger,
        gateway=gateway or FakeGateway(),
        customer_id=customer_id,
        now_fn=clock,
        approval_ttl=approval_ttl,
    )


def add_card(wallet: Wallet, payment_method_id: str = PM_ID) -> dict[str, Any]:
    return wallet.attach(payment_method_id, make_default=True)


def ledger_text(wallet: Wallet) -> str:
    return "\n".join(wallet.ledger._conn.iterdump())  # noqa: SLF001 - test inspection


# -- configuration ---------------------------------------------------------


def test_unconfigured_wallet_is_visible_but_cannot_take_payments(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def _no_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a network connection was attempted")

    monkeypatch.setattr(socket.socket, "connect", _no_network)
    wallet = Wallet.from_env({})

    assert wallet.status()["configured"] is False
    assert wallet.status()["mode"] == "unconfigured"
    assert wallet.limits("usd")["per_purchase_cents"] == 5000
    with pytest.raises(WalletNotConfigured):
        wallet.create_setup_intent()
    with pytest.raises(WalletNotConfigured):
        wallet.attach(PM_ID)

    # Orders are local: they can be drafted without a key, but nothing can be
    # charged until the key exists.
    wallet.ledger.add_card(Card(PM_ID, "visa", "4242", 12, 2030, True))
    proposal, _ = wallet.propose(1000)
    with pytest.raises(WalletNotConfigured):
        wallet.approve(proposal.id)


def test_configured_wallet_never_exposes_the_key(gateway: FakeGateway) -> None:
    wallet = make_wallet(gateway=gateway)
    status = wallet.status()
    status_text = json.dumps(status, default=str)

    assert status["configured"] is True
    assert status["mode"] == "test"
    assert status["network_calls"] is False
    assert TEST_KEY not in repr(wallet)
    assert TEST_KEY not in str(wallet)
    assert TEST_KEY not in status_text
    assert TEST_KEY not in ledger_text(wallet)


def test_from_env_reads_only_server_environment() -> None:
    wallet = Wallet.from_env(
        {
            STRIPE_SECRET_KEY_ENV: TEST_KEY,
            CUSTOMER_ID_ENV: CUSTOMER,
            PER_PURCHASE_CAP_ENV: "1234",
            MONTHLY_CAP_ENV: "5678",
        }
    )

    assert wallet.configured is True
    assert wallet.customer_id == CUSTOMER
    assert wallet.caps == SpendCaps(1234, 5678)
    assert wallet.limits("usd")["remaining_cents"] == 5678


@pytest.mark.parametrize("value", ["0", "-5", "12x", "1.5"])
def test_invalid_cap_environment_fails_closed(value: str) -> None:
    with pytest.raises(WalletError) as caught:
        SpendCaps.from_env({PER_PURCHASE_CAP_ENV: value})
    assert caught.value.code == "invalid_config"


def test_publishable_key_is_refused_as_a_secret_key() -> None:
    wallet = Wallet(
        config=StripeConfig("pk_test_abc123"),
        ledger=WalletLedger(),
        gateway=FakeGateway(),
    )
    with pytest.raises(WalletError) as caught:
        wallet.create_setup_intent()
    assert caught.value.code == "invalid_config"


# -- cards -----------------------------------------------------------------


def test_setup_intent_returns_client_secret_but_no_key() -> None:
    gateway = FakeGateway()
    wallet = make_wallet(gateway=gateway)

    result = wallet.create_setup_intent()

    assert result["id"] == "seti_test_123"
    assert result["client_secret"].startswith("seti_")
    assert TEST_KEY not in json.dumps(result)
    assert gateway.setup_intents[0]["secret_key"] == TEST_KEY
    assert gateway.setup_intents[0]["metadata"] == {"source": "kelp-wallet"}


def test_attach_keeps_only_allowlisted_card_fields() -> None:
    gateway = FakeGateway()
    wallet = make_wallet(gateway=gateway)

    card = add_card(wallet)

    assert card == {
        "id": PM_ID,
        "brand": "visa",
        "last4": "4242",
        "exp_month": 12,
        "exp_year": 2030,
        "is_default": True,
    }
    assert gateway.attachments[0]["payment_method_id"] == PM_ID
    assert gateway.attachments[0]["secret_key"] == TEST_KEY
    assert "4242424242424242" not in ledger_text(wallet)
    assert TEST_KEY not in ledger_text(wallet)


def test_attach_rejects_raw_card_data_and_stores_nothing() -> None:
    gateway = FakeGateway()
    gateway.attach_result = {
        "id": PM_ID,
        "card": {
            "brand": "visa",
            "last4": "4242",
            "exp_month": 12,
            "exp_year": 2030,
            "number": PAN,
            "cvc": "123",
        },
    }
    wallet = make_wallet(gateway=gateway)

    with pytest.raises(WalletError) as caught:
        wallet.attach(PM_ID)

    assert caught.value.code == "unstorable_card"
    assert caught.value.status == 422
    assert PAN not in json.dumps(caught.value.to_body())
    assert wallet.list_cards() == []


def test_card_from_payment_method_rejects_bad_shapes() -> None:
    with pytest.raises(WalletError):
        card_from_payment_method({"id": "not-a-payment-method", "card": {}})
    with pytest.raises(WalletError):
        card_from_payment_method(
            {
                "id": PM_ID,
                "card": {"brand": "visa", "last4": "42", "exp_month": 12, "exp_year": 2030},
            }
        )
    with pytest.raises(WalletError):
        card_from_payment_method(
            {
                "id": PM_ID,
                "card": {"brand": "visa", "last4": "4242", "exp_month": 13, "exp_year": 2030},
            }
        )


def test_list_and_refresh_cards() -> None:
    gateway = FakeGateway()
    wallet = make_wallet(gateway=gateway)

    assert wallet.list_cards() == []
    add_card(wallet)
    assert [card["last4"] for card in wallet.list_cards()] == ["4242"]

    gateway.list_result = [
        {
            "id": "pm_testmastercard",
            "card": {
                "brand": "mastercard",
                "last4": "4444",
                "exp_month": 1,
                "exp_year": 2031,
            },
        }
    ]
    cards = wallet.refresh_cards()
    assert {card["id"] for card in cards} == {PM_ID, "pm_testmastercard"}
    assert gateway.listings[0]["customer_id"] == CUSTOMER


def test_first_saved_card_becomes_default_and_others_do_not() -> None:
    ledger = WalletLedger()
    first = ledger.add_card(Card(PM_ID, "visa", "4242", 12, 2030))
    second = ledger.add_card(Card("pm_testmastercard", "mastercard", "4444", 1, 2031))

    assert first.is_default is True
    assert second.is_default is False
    assert ledger.default_card() is not None
    assert ledger.default_card().id == PM_ID


def test_refresh_cards_never_steals_the_default_card() -> None:
    gateway = FakeGateway()
    wallet = make_wallet(gateway=gateway)
    add_card(wallet)  # PM_ID, the owner's default

    gateway.list_result = [
        {
            "id": "pm_testmastercard",
            "card": {
                "brand": "mastercard",
                "last4": "4444",
                "exp_month": 1,
                "exp_year": 2031,
            },
        }
    ]
    cards = wallet.refresh_cards()
    assert [card["id"] for card in cards if card["is_default"]] == [PM_ID]

    # Syncing a card the owner already has must not move the default either.
    gateway.list_result = [
        {
            "id": PM_ID,
            "card": {
                "brand": "visa",
                "last4": "4242",
                "exp_month": 12,
                "exp_year": 2030,
            },
        }
    ]
    cards = wallet.refresh_cards()
    assert [card["id"] for card in cards if card["is_default"]] == [PM_ID]

    # Only an explicit choice moves it.
    moved = wallet.ledger.add_card(
        Card("pm_testmastercard", "mastercard", "4444", 1, 2031),
        make_default=True,
    )
    assert moved.is_default is True
    assert [card["id"] for card in wallet.list_cards() if card["is_default"]] == [
        "pm_testmastercard"
    ]


def test_file_ledger_is_owner_only(tmp_path: Path) -> None:
    directory = tmp_path / "wallet"
    ledger = WalletLedger(directory / "wallet.db")
    # Force a write so the WAL and SHM siblings exist while it is open.
    ledger.add_card(Card(PM_ID, "visa", "4242", 12, 2030, True))
    ledger.create_proposal(
        amount_cents=100,
        currency="usd",
        description="one",
        idempotency_key="order-perm-1",
    )

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for name in ("wallet.db", "wallet.db-wal", "wallet.db-shm"):
        target = directory / name
        assert target.exists(), f"{name} should exist while the ledger is open"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600, name
    ledger.close()


def test_existing_loose_ledger_directory_is_tightened(tmp_path: Path) -> None:
    directory = tmp_path / "loose"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)

    ledger = WalletLedger(directory / "wallet.db")

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / "wallet.db").stat().st_mode) == 0o600
    ledger.close()


# -- orders and caps -------------------------------------------------------


@pytest.mark.parametrize("amount", [0, -1, True, 1.5, "100", None])
def test_propose_rejects_non_positive_and_non_integer_amounts(amount: Any) -> None:
    wallet = make_wallet()
    with pytest.raises(WalletError) as caught:
        wallet.propose(amount)
    assert caught.value.code == "invalid_amount"


def test_per_purchase_cap_is_enforced_at_propose() -> None:
    wallet = make_wallet(caps=SpendCaps(per_purchase_cents=5000, monthly_cents=20_000))
    with pytest.raises(WalletError) as caught:
        wallet.propose(5001)
    assert caught.value.code == "cap_exceeded"
    assert caught.value.details["per_purchase_cap_cents"] == 5000


def test_monthly_cap_is_enforced_at_propose() -> None:
    wallet = make_wallet(
        gateway=FakeGateway(),
        caps=SpendCaps(per_purchase_cents=5000, monthly_cents=6000),
    )
    add_card(wallet)
    first, _ = wallet.propose(5000, idempotency_key="order-month-1")
    wallet.approve(first.id)

    with pytest.raises(WalletError) as caught:
        wallet.propose(2000, idempotency_key="order-month-2")
    assert caught.value.code == "monthly_cap_exceeded"


def test_propose_is_idempotent_per_order_key() -> None:
    wallet = make_wallet()
    first, created = wallet.propose(1000, idempotency_key="order-abc-123")
    second, created_again = wallet.propose(1000, idempotency_key="order-abc-123")
    other, _ = wallet.propose(1000, idempotency_key="order-abc-124")

    assert created is True
    assert created_again is False
    assert first.id == second.id
    assert other.id != first.id
    rows = wallet.ledger._conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0]
    assert rows == 2


def test_propose_rejects_short_idempotency_key() -> None:
    wallet = make_wallet()
    with pytest.raises(WalletError) as caught:
        wallet.propose(1000, idempotency_key="short")
    assert caught.value.code == "invalid_idempotency_key"


def test_proposal_description_redacts_card_data() -> None:
    wallet = make_wallet()
    proposal, _ = wallet.propose(
        1000, description=f"pay with {PAN} please", idempotency_key="order-redact-1"
    )

    assert PAN not in proposal.description
    assert PAN not in proposal.to_public()["description"]
    assert PAN not in ledger_text(wallet)
    assert "[redacted-card]" in proposal.description


# -- approval and charging -------------------------------------------------


def test_approve_without_a_saved_card_fails_before_charging() -> None:
    gateway = FakeGateway()
    wallet = make_wallet(gateway=gateway)
    proposal, _ = wallet.propose(1000)

    with pytest.raises(WalletError) as caught:
        wallet.approve(proposal.id)

    assert caught.value.code == "no_saved_card"
    assert gateway.payments == []
    assert wallet.ledger.get_proposal(proposal.id).status == "pending"


def test_deny_charges_nothing() -> None:
    gateway = FakeGateway()
    wallet = make_wallet(gateway=gateway)
    add_card(wallet)
    proposal, _ = wallet.propose(1000)

    denied, idempotent = wallet.approve(proposal.id, decision="deny")

    assert idempotent is False
    assert denied.status == "denied"
    assert denied.receipt is None
    assert gateway.payments == []
    assert wallet.limits("usd")["spent_cents"] == 0

    repeated, again = wallet.approve(proposal.id, decision="deny")
    assert again is True
    assert repeated.status == "denied"


def test_approve_charges_once_and_repeats_are_idempotent() -> None:
    gateway = FakeGateway()
    wallet = make_wallet(gateway=gateway)
    add_card(wallet)
    proposal, _ = wallet.propose(2500, description="coffee", idempotency_key="order-pay-1")

    paid, idempotent = wallet.approve(proposal.id)

    assert idempotent is False
    assert paid.status == "paid"
    assert paid.receipt is not None
    assert paid.receipt.payment_intent_id == "pi_test_123"
    assert paid.receipt.amount_cents == 2500
    assert paid.receipt.last4 == "4242"
    assert len(gateway.payments) == 1
    assert gateway.payments[0]["idempotency_key"] == f"kelp-order-{proposal.id}"
    assert gateway.payments[0]["amount_cents"] == 2500
    assert TEST_KEY not in json.dumps(paid.to_public())
    assert PAN not in ledger_text(wallet)

    again, repeated = wallet.approve(proposal.id)
    assert repeated is True
    assert again.status == "paid"
    assert again.receipt == paid.receipt
    assert len(gateway.payments) == 1
    assert wallet.limits("usd")["spent_cents"] == 2500


def test_conflicting_decision_after_payment_is_rejected() -> None:
    wallet = make_wallet()
    add_card(wallet)
    proposal, _ = wallet.propose(1000)
    wallet.approve(proposal.id)

    with pytest.raises(WalletError) as caught:
        wallet.approve(proposal.id, decision="deny")
    assert caught.value.code == "already_decided"


def test_monthly_cap_is_rechecked_at_approval() -> None:
    gateway = FakeGateway()
    wallet = make_wallet(
        gateway=gateway, caps=SpendCaps(per_purchase_cents=5000, monthly_cents=6000)
    )
    add_card(wallet)
    first, _ = wallet.propose(5000, idempotency_key="order-cap-1")
    second, _ = wallet.propose(2000, idempotency_key="order-cap-2")

    wallet.approve(first.id)
    with pytest.raises(WalletError) as caught:
        wallet.approve(second.id)

    assert caught.value.code == "monthly_cap_exceeded"
    assert wallet.ledger.get_proposal(second.id).status == "blocked"
    assert len(gateway.payments) == 1
    assert wallet.limits("usd")["spent_cents"] == 5000


def test_gateway_failure_marks_failed_and_never_counts_as_spend() -> None:
    gateway = FakeGateway()
    gateway.payment_error = StripeGatewayError("Stripe rejected the request (402).")
    wallet = make_wallet(gateway=gateway)
    add_card(wallet)
    proposal, _ = wallet.propose(1000)

    with pytest.raises(WalletError) as caught:
        wallet.approve(proposal.id)

    assert caught.value.code == "payment_failed"
    assert caught.value.status == 502
    assert wallet.ledger.get_proposal(proposal.id).status == "failed"
    assert wallet.ledger.get_proposal(proposal.id).receipt is None
    assert wallet.limits("usd")["spent_cents"] == 0


@pytest.mark.parametrize(
    ("result", "status", "retryable"),
    [
        ({"amount": 999}, "failed", False),  # different amount than approved
        ({"currency": "eur"}, "failed", False),
        ({"id": "not_an_intent"}, "failed", False),
        # The processor may still be working: keep the order resumable.
        ({"status": "processing"}, "approved", True),
        ({"status": "requires_action"}, "approved", True),
    ],
)
def test_incomplete_or_mismatched_payment_is_not_a_receipt(
    result: dict[str, Any], status: str, retryable: bool
) -> None:
    gateway = FakeGateway()
    gateway.payment_result = result
    wallet = make_wallet(gateway=gateway)
    add_card(wallet)
    proposal, _ = wallet.propose(1000)

    with pytest.raises(WalletError) as caught:
        wallet.approve(proposal.id)

    assert caught.value.code == "payment_failed"
    assert caught.value.retryable is retryable
    stored = wallet.ledger.get_proposal(proposal.id)
    assert stored.status == status
    assert stored.receipt is None
    assert wallet.limits("usd")["spent_cents"] == 0


def test_resume_of_approved_order_reuses_the_same_charge_key() -> None:
    gateway = FakeGateway()
    wallet = make_wallet(gateway=gateway)
    card = Card(PM_ID, "visa", "4242", 12, 2030, True)
    wallet.ledger.add_card(card)
    proposal, _ = wallet.propose(1000)
    # Simulate a crash after the decision but before the receipt existed.
    wallet.ledger.decide(
        proposal.id,
        decision="approve",
        payment_method_id=PM_ID,
        monthly_cap_cents=20_000,
        monthly_since="1970-01-01T00:00:00+00:00",
    )

    paid, idempotent = wallet.approve(proposal.id)

    assert idempotent is True
    assert paid.status == "paid"
    assert gateway.payments[0]["idempotency_key"] == f"kelp-order-{proposal.id}"


def test_retryable_transport_error_keeps_the_order_approved_for_resume() -> None:
    gateway = FakeGateway()
    gateway.payment_error = StripeGatewayError(
        "Can't reach the card processor right now.", retryable=True
    )
    wallet = make_wallet(gateway=gateway)
    add_card(wallet)
    proposal, _ = wallet.propose(1000, idempotency_key="order-timeout-1")

    with pytest.raises(WalletError) as caught:
        wallet.approve(proposal.id)

    assert caught.value.code == "payment_failed"
    assert caught.value.status == 502
    assert caught.value.retryable is True
    stored = wallet.ledger.get_proposal(proposal.id)
    assert stored.status == "approved"  # not failed: the outcome is unknown
    assert stored.receipt is None
    limits = wallet.limits("usd")
    assert limits["spent_cents"] == 0
    assert limits["reserved_cents"] == 1000
    assert limits["remaining_cents"] == 19_000

    # The retry resumes the same order and reuses the same Stripe key.
    gateway.payment_error = None
    paid, idempotent = wallet.approve(proposal.id)

    assert idempotent is True
    assert paid.status == "paid"
    assert paid.receipt is not None
    assert paid.receipt.payment_intent_id == "pi_test_123"
    assert len(gateway.payments) == 2
    assert {call["idempotency_key"] for call in gateway.payments} == {
        f"kelp-order-{proposal.id}"
    }
    assert len(gateway.intents) == 1  # Stripe saw one intent for this order
    assert wallet.limits("usd")["spent_cents"] == 1000
    assert wallet.limits("usd")["reserved_cents"] == 0


def test_approval_reserves_the_amount_against_new_orders() -> None:
    gateway = FakeGateway()
    gateway.payment_error = StripeGatewayError("timeout", retryable=True)
    wallet = make_wallet(
        gateway=gateway, caps=SpendCaps(per_purchase_cents=5000, monthly_cents=6000)
    )
    add_card(wallet)
    first, _ = wallet.propose(5000, idempotency_key="order-reserve-1")
    with pytest.raises(WalletError):
        wallet.approve(first.id)

    # Nothing is paid yet, but the approval holds the money.
    with pytest.raises(WalletError) as caught:
        wallet.propose(2000, idempotency_key="order-reserve-2")

    assert caught.value.code == "monthly_cap_exceeded"
    assert caught.value.details["spent_cents"] == 0
    assert caught.value.details["reserved_cents"] == 5000
    assert caught.value.details["monthly_cap_cents"] == 6000


def test_two_approvals_cannot_both_fit_inside_the_cap() -> None:
    gateway = FakeGateway()
    gateway.payment_error = StripeGatewayError("timeout", retryable=True)
    wallet = make_wallet(
        gateway=gateway, caps=SpendCaps(per_purchase_cents=5000, monthly_cents=6000)
    )
    add_card(wallet)
    first, _ = wallet.propose(5000, idempotency_key="order-conc-1")
    second, _ = wallet.propose(2000, idempotency_key="order-conc-2")

    with pytest.raises(WalletError):
        wallet.approve(first.id)  # leaves first approved, 5000 reserved

    with pytest.raises(WalletError) as caught:
        wallet.approve(second.id)

    assert caught.value.code == "monthly_cap_exceeded"
    assert wallet.ledger.get_proposal(second.id).status == "blocked"
    assert gateway.intents == {}  # the second order never reached Stripe


def test_stale_approval_is_swept_and_frees_the_monthly_cap() -> None:
    clock = Clock(datetime(2026, 3, 1, 12, tzinfo=timezone.utc))
    gateway = FakeGateway()
    gateway.payment_error = StripeGatewayError("timeout", retryable=True)
    wallet = make_wallet(
        clock=clock,
        gateway=gateway,
        caps=SpendCaps(per_purchase_cents=5000, monthly_cents=6000),
    )
    add_card(wallet)
    first, _ = wallet.propose(5000, idempotency_key="order-stale-1")
    with pytest.raises(WalletError):
        wallet.approve(first.id)
    assert wallet.ledger.get_proposal(first.id).status == "approved"

    with pytest.raises(WalletError) as caught:
        wallet.propose(2000, idempotency_key="order-stale-2")
    assert caught.value.code == "monthly_cap_exceeded"

    clock.value = clock.value + DEFAULT_APPROVAL_TTL + timedelta(seconds=1)

    # The abandoned approval can no longer be charged...
    with pytest.raises(WalletError) as late:
        wallet.approve(first.id)
    assert late.value.code == "already_decided"

    # ...and no longer holds the budget.
    second, created = wallet.propose(2000, idempotency_key="order-stale-2")
    assert created is True
    swept = wallet.ledger.get_proposal(first.id)
    assert swept.status == "failed"
    assert swept.reason == APPROVAL_EXPIRED_REASON
    assert wallet.limits("usd")["reserved_cents"] == 0

    gateway.payment_error = None
    paid, _ = wallet.approve(second.id)
    assert paid.status == "paid"
    assert wallet.limits("usd")["spent_cents"] == 2000


def test_each_order_gets_its_own_intent_and_retries_share_one() -> None:
    gateway = FakeGateway()
    wallet = make_wallet(gateway=gateway)
    add_card(wallet)
    first, _ = wallet.propose(1000, idempotency_key="order-intent-a")
    second, _ = wallet.propose(1000, idempotency_key="order-intent-b")

    wallet.approve(first.id)
    wallet.approve(second.id)

    assert set(gateway.intents) == {
        f"kelp-order-{first.id}",
        f"kelp-order-{second.id}",
    }

    # A repeat approval of a settled order never calls Stripe again.
    wallet.approve(first.id)
    assert len(gateway.payments) == 2
    assert len(gateway.intents) == 2


def test_month_rollover_resets_the_monthly_total() -> None:
    clock = Clock(datetime(2026, 1, 31, 12, tzinfo=timezone.utc))
    wallet = make_wallet(
        clock=clock, caps=SpendCaps(per_purchase_cents=5000, monthly_cents=6000)
    )
    add_card(wallet)
    proposal, _ = wallet.propose(5000, idempotency_key="order-jan-1")
    wallet.approve(proposal.id)
    assert wallet.limits("usd")["spent_cents"] == 5000

    clock.value = datetime(2026, 2, 1, 0, 1, tzinfo=timezone.utc)
    limits = wallet.limits("usd")
    assert limits["spent_cents"] == 0
    assert limits["remaining_cents"] == 6000


def test_unknown_proposal_and_card_are_404s() -> None:
    wallet = make_wallet()
    with pytest.raises(WalletError) as caught:
        wallet.approve("ord_missing")
    assert caught.value.code == "unknown_proposal"
    assert caught.value.status == 404

    add_card(wallet)
    proposal, _ = wallet.propose(1000)
    with pytest.raises(WalletError) as caught:
        wallet.approve(proposal.id, payment_method_id="pm_testunknown")
    assert caught.value.code == "unknown_card"
    assert caught.value.status == 404


def test_duplicate_proposal_insert_is_guarded_by_the_unique_key() -> None:
    ledger = WalletLedger()
    first, created = ledger.create_proposal(
        amount_cents=100,
        currency="usd",
        description="one",
        idempotency_key="order-unique-1",
    )
    second, created_again = ledger.create_proposal(
        amount_cents=100,
        currency="usd",
        description="one",
        idempotency_key="order-unique-1",
    )
    assert created is True
    assert created_again is False
    assert first.id == second.id


# -- redaction -------------------------------------------------------------


def test_redact_text_masks_card_numbers_and_stripe_keys() -> None:
    text = f"card {PAN} and {TEST_KEY} and 4242 4242 4242 4242"
    cleaned = redact_text(text)
    assert PAN not in cleaned
    assert TEST_KEY not in cleaned
    assert cleaned.count("[redacted-card]") == 2


def test_redact_payload_drops_secret_keys_recursively() -> None:
    cleaned = redact_payload(
        {
            "card": {"number": PAN, "cvc": "123", "last4": "4242"},
            "client_secret": "seti_secret",
            "items": [{"token": "abc", "note": PAN}],
            "amount": 100,
        }
    )
    assert cleaned["card"]["number"] == "[redacted]"
    assert cleaned["card"]["cvc"] == "[redacted]"
    assert cleaned["card"]["last4"] == "4242"
    assert cleaned["client_secret"] == "[redacted]"
    assert cleaned["items"][0]["token"] == "[redacted]"
    assert PAN not in json.dumps(cleaned)
    assert cleaned["amount"] == 100


def test_contains_raw_card_data() -> None:
    assert contains_raw_card_data({"card": {"number": PAN}}) is True
    assert contains_raw_card_data({"note": f"see {PAN}"}) is True
    assert contains_raw_card_data({"note": "4242 4242 4242 4242"}) is True
    assert contains_raw_card_data({"card": {"last4": "4242"}}) is False
    assert contains_raw_card_data({"amount_cents": 1000}) is False


def test_identifiers_with_long_digit_runs_are_not_card_data() -> None:
    # A uuid4 hex id can contain 13+ digits in a row; treating that as a card
    # number rejected the wallet's own order ids about one run in fourteen.
    proposal_id = "ord_1761924561876a1c9d3fefff161680"
    assert contains_raw_card_data({"proposal_id": proposal_id}) is False
    assert contains_raw_card_data({"payment_intent_id": "pi_test_123"}) is False
    # Redaction is still allowed to be broader than rejection.
    assert PAN not in redact_text(f"card {PAN}")
    assert "[redacted-card]" in redact_text(f"x{PAN}")


def test_no_hardcoded_stripe_keys_in_the_new_wallet_module() -> None:
    offenders = [
        str(path)
        for path in (SOURCE_ROOT / "payments").glob("wallet*.py")
        if _KEY_LITERAL.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_http_gateway_uses_the_order_key_and_never_leaks_the_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from payments.wallet import HttpStripeGateway

    captured: dict[str, Any] = {}

    class FakeResponse:
        def __init__(self, status_code: int, body: Mapping[str, Any]) -> None:
            self.status_code = status_code
            self._body = body

        def json(self) -> Mapping[str, Any]:
            return self._body

    class FakeClient:
        def __init__(self, *, timeout: float | None = None) -> None:
            captured["timeout"] = timeout

        def request(
            self,
            method: str,
            url: str,
            *,
            data: Mapping[str, str] | None = None,
            params: Mapping[str, str] | None = None,
            headers: Mapping[str, str] | None = None,
        ) -> FakeResponse:
            captured.update(
                method=method, url=url, data=dict(data or {}),
                params=dict(params or {}), headers=dict(headers or {}),
            )
            if captured["url"].endswith("/payment_methods"):
                return FakeResponse(
                    200,
                    {
                        "data": [
                            {
                                "id": "pm_testvisa",
                                "card": {
                                    "brand": "visa",
                                    "last4": "4242",
                                    "exp_month": 12,
                                    "exp_year": 2030,
                                },
                            }
                        ]
                    },
                )
            if captured.get("fail"):
                return FakeResponse(
                    402, {"error": {"code": "card_declined", "message": f"{TEST_KEY}"}}
                )
            return FakeResponse(
                200,
                {
                    "id": "pi_test_123",
                    "status": "succeeded",
                    "amount": 100,
                    "currency": "usd",
                },
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr(httpx, "Client", FakeClient)
    gateway = HttpStripeGateway()
    intent = gateway.create_payment_intent(
        secret_key=TEST_KEY,
        amount_cents=100,
        currency="usd",
        payment_method_id=PM_ID,
        customer_id=CUSTOMER,
        description="test",
        idempotency_key="kelp-order-ord_123",
    )

    assert intent["id"] == "pi_test_123"
    assert captured["headers"]["Authorization"] == f"Bearer {TEST_KEY}"
    assert captured["headers"]["Idempotency-Key"] == "kelp-order-ord_123"
    assert captured["data"]["amount"] == "100"

    gateway.list_payment_methods(secret_key=TEST_KEY, customer_id=CUSTOMER)
    assert captured["params"]["type"] == "card"

    captured["fail"] = True
    with pytest.raises(StripeGatewayError) as caught:
        gateway.create_payment_intent(
            secret_key=TEST_KEY,
            amount_cents=100,
            currency="usd",
            payment_method_id=PM_ID,
            customer_id=CUSTOMER,
            description="test",
            idempotency_key="kelp-order-ord_123",
        )
    assert TEST_KEY not in str(caught.value)
    gateway.close()

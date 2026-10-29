"""KEL-10 Razorpay hosted Checkout: order, signature, and settlement.

This is the second way to pay through the same wallet. ``RazorpayWallet``
charges a card already on file; hosted Checkout lets the browser collect the
card on Razorpay's side. Both paths share one ledger, one set of spend caps, and
one reservation sweep, so a checkout order cannot spend budget the saved-card
flow has already committed.

The flow:

1. :meth:`RazorpayCheckout.create` proposes an order, reserves it against the
   monthly cap, creates a Razorpay *order* with ``receipt=<kelp order id>``
   (the local ``ord_...`` row; Razorpay caps receipts at 40 characters, so the
   longer ``kelp-order-...`` idempotency key is used on the request header
   instead), records the provider-order mapping, and returns the payload a
   hosted Checkout needs (``key_id``, ``order_id``, amount, currency, receipt).
2. Razorpay returns ``razorpay_order_id``, ``razorpay_payment_id``, and
   ``razorpay_signature`` to the browser.
3. :meth:`RazorpayCheckout.confirm` verifies
   ``HMAC-SHA256(order_id + "|" + payment_id, key_secret)`` in constant time,
   fetches the payment from Razorpay, checks that its order, amount, and
   currency match the reserved order, and only then writes ``paid`` plus a
   receipt in one transaction.
4. :meth:`RazorpayCheckout.handle_webhook` does the same settlement for a
   server-to-server event. A webhook is authenticated either by the
   ``X-Razorpay-Signature`` HMAC over the raw body (``RAZORPAY_WEBHOOK_SECRET``)
   or, when no webhook secret is set, by a per-payment ``razorpay_signature``
   checked with ``RAZORPAY_KEY_SECRET``.

Security boundaries, the same as the rest of the wallet:

- Keys are read from the server environment only. ``RAZORPAY_KEY_SECRET`` is
  never written to the ledger, a response, ``repr``, or an error message.
  ``RAZORPAY_KEY_ID`` is a publishable identifier and is the one value the
  hosted Checkout needs in the browser; it is still redacted wherever the
  wallet's redactor is applied.
- An order is settled only after a valid signature *and* a provider fetch whose
  order id, amount, and currency match the reserved order. A signature alone is
  not a receipt.
- Settlement is idempotent by receipt: replaying the same payment returns the
  same receipt, while a different payment for the same order, or one payment
  reused for another order, is a conflict.
- A definitive mismatch marks the order ``failed`` and releases its
  reservation. An unknown outcome stays ``approved`` so the caller can retry
  with the same provider order.

Configuration (server-only environment):

===================================  ==========================================
``RAZORPAY_KEY_ID``                  Test-mode key id (``rzp_test_...``).
``RAZORPAY_KEY_SECRET``              Matching key secret.
``RAZORPAY_WEBHOOK_SECRET``          Optional webhook signing secret.
``RAZORPAY_CHECKOUT_NAME``           Optional merchant name for Checkout.
``RAZORPAY_CHECKOUT_CALLBACK_URL``   Optional return URL after payment.
``RAZORPAY_CHECKOUT_URL``            Optional self-hosted page that opens
                                     Checkout; returned as ``checkout_url``.
===================================  ==========================================
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from .razorpay_gateway import (
    RazorpayConfig,
    RazorpayGatewayError,
    RazorpayTransport,
    RazorpayWallet,
    redact_razorpay_payload,
    redact_razorpay_text,
    wallet_payment_intent_id,
)
from .wallet import (
    APPROVAL_EXPIRED_REASON,
    Proposal,
    WalletError,
    redact_payload,
)

__all__ = [
    "CHECKOUT_CALLBACK_ENV",
    "CHECKOUT_NAME_ENV",
    "CHECKOUT_URL_ENV",
    "WEBHOOK_SECRET_ENV",
    "RazorpayCheckout",
    "expected_payment_signature",
    "expected_webhook_signature",
    "redact_checkout_payload",
    "redact_checkout_text",
    "verify_payment_signature",
    "verify_webhook_signature",
]

WEBHOOK_SECRET_ENV = "RAZORPAY_WEBHOOK_SECRET"
CHECKOUT_NAME_ENV = "RAZORPAY_CHECKOUT_NAME"
CHECKOUT_CALLBACK_ENV = "RAZORPAY_CHECKOUT_CALLBACK_URL"
CHECKOUT_URL_ENV = "RAZORPAY_CHECKOUT_URL"

PROVIDER = "razorpay"
DEFAULT_CHECKOUT_NAME = "Kelp"
MAX_JSON_BYTES = 256 * 1024

_ORDER_RE = re.compile(r"^order_[A-Za-z0-9]{4,}$")
_PAYMENT_RE = re.compile(r"^pay_[A-Za-z0-9]{4,}$")
_HEX_DIGEST_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_MAX_NAME_CHARS = 80

# Same rule as ``payments.wallet``: over-match when hiding text.
_RAZORPAY_KEY_RE = re.compile(r"\brzp_(?:live|test)_[A-Za-z0-9]{4,}")
_SECRET_KEY_RE = re.compile(
    r"(?i)(signature|secret|password|token|authorization|credential)"
)

_STATUS_MAP = {
    "captured": "captured",
    "authorized": "authorized",
    "created": "pending",
    "pending": "pending",
    "processing": "pending",
    "failed": "failed",
    "refunded": "refunded",
}

_WEBHOOK_EVENTS = frozenset(
    {
        "payment.captured",
        "payment.authorized",
        "payment.failed",
        "order.paid",
    }
)


def redact_checkout_text(value: Any) -> str:
    """Mask Razorpay keys and signature-like values in free text."""

    text = redact_razorpay_text(value if isinstance(value, str) else str(value))
    return _RAZORPAY_KEY_RE.sub("[redacted]", text)


def redact_checkout_payload(value: Any) -> Any:
    """Recursively redact a checkout payload, including signatures."""

    return _scrub(redact_razorpay_payload(redact_payload(value)))


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return _RAZORPAY_KEY_RE.sub("[redacted]", value)
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if _SECRET_KEY_RE.search(name):
                cleaned[name] = "[redacted]"
            else:
                cleaned[name] = _scrub(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_scrub(item) for item in value]
    return value


# -- signatures ------------------------------------------------------------


def expected_payment_signature(
    secret_key: str, order_id: str, payment_id: str
) -> str:
    """The value Razorpay Checkout returns as ``razorpay_signature``."""

    message = f"{order_id}|{payment_id}".encode("utf-8")
    return hmac.new(
        (secret_key or "").encode("utf-8"), message, hashlib.sha256
    ).hexdigest()


def verify_payment_signature(
    secret_key: str, order_id: str, payment_id: str, signature: str | None
) -> bool:
    """Constant-time comparison of the order/payment signature."""

    if not isinstance(signature, str):
        return False
    candidate = signature.strip()
    if _HEX_DIGEST_RE.match(candidate) is None:
        return False
    expected = expected_payment_signature(secret_key, order_id, payment_id)
    return hmac.compare_digest(expected, candidate.lower())


def expected_webhook_signature(secret: str, body: bytes) -> str:
    return hmac.new(
        (secret or "").encode("utf-8"), body, hashlib.sha256
    ).hexdigest()


def verify_webhook_signature(
    secret: str, body: bytes, signature: str | None
) -> bool:
    if not isinstance(signature, str):
        return False
    candidate = signature.strip()
    if _HEX_DIGEST_RE.match(candidate) is None:
        return False
    expected = expected_webhook_signature(secret, body)
    return hmac.compare_digest(expected, candidate.lower())


# -- service ---------------------------------------------------------------


class RazorpayCheckout:
    """Hosted Checkout on top of a configured :class:`RazorpayWallet`."""

    provider = PROVIDER

    def __init__(
        self,
        *,
        wallet: RazorpayWallet,
        webhook_secret: str = "",
        checkout_name: str = DEFAULT_CHECKOUT_NAME,
        callback_url: str = "",
        checkout_url: str = "",
    ) -> None:
        self.wallet = wallet
        self._webhook_secret = (webhook_secret or "").strip()
        self.checkout_name = _safe_name(checkout_name)
        self.callback_url = _safe_url(callback_url, field="callback_url")
        self.checkout_url = _safe_url(checkout_url, field="checkout_url")

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        ledger: Any = None,
        transport: RazorpayTransport | None = None,
        now_fn: Any = None,
    ) -> "RazorpayCheckout":
        source = os.environ if env is None else env
        wallet = RazorpayWallet.from_env(
            source, ledger=ledger, transport=transport, now_fn=now_fn
        )
        return cls(
            wallet=wallet,
            webhook_secret=(source.get(WEBHOOK_SECRET_ENV) or "").strip(),
            checkout_name=(source.get(CHECKOUT_NAME_ENV) or DEFAULT_CHECKOUT_NAME),
            callback_url=(source.get(CHECKOUT_CALLBACK_ENV) or "").strip(),
            checkout_url=(source.get(CHECKOUT_URL_ENV) or "").strip(),
        )

    def __repr__(self) -> str:
        mode = (
            self.wallet._config.mode  # noqa: SLF001 - same package, redacted
            if self.wallet._config is not None  # noqa: SLF001
            else "unconfigured"
        )
        return (
            f"RazorpayCheckout(mode={mode!r}, "
            f"webhooks={bool(self._webhook_secret)}, "
            f"configured={self.wallet.configured})"
        )

    __str__ = __repr__

    # -- introspection -----------------------------------------------------

    @property
    def webhook_configured(self) -> bool:
        return bool(self._webhook_secret)

    def status(self) -> dict[str, Any]:
        status = dict(self.wallet.status())
        status.update(
            {
                "checkout": "hosted",
                "webhook_configured": self.webhook_configured,
                "name": self.checkout_name,
            }
        )
        return status

    # -- order creation ----------------------------------------------------

    def create(
        self,
        amount_cents: int,
        *,
        currency: str | None = None,
        description: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Reserve an order and create the Razorpay order behind Checkout.

        Returns ``{"checkout": ..., "proposal": ..., "idempotent": bool}``.
        Repeating the call with the same ``idempotency_key`` returns the same
        provider order; it never creates a second charge opportunity.
        """

        # Fail before touching the ledger: an unconfigured checkout must not
        # reserve (and then abandon) part of the monthly cap.
        config = self._require_config()
        proposal, _ = self.wallet.propose(
            amount_cents,
            currency=currency,
            description=description,
            idempotency_key=idempotency_key,
        )
        if proposal.status == "paid":
            raise WalletError(
                "already_decided",
                message="That order was already paid.",
            )
        if proposal.status in {"denied", "blocked", "failed"}:
            raise WalletError(
                "already_decided",
                message="That order is no longer payable.",
            )
        existing = self.wallet.ledger.find_checkout_for_proposal(proposal.id)
        if existing is not None:
            return self._result(proposal, existing, created=False)

        if proposal.status == "approved" and proposal.payment_method_id:
            # Reserved by the saved-card flow: a hosted Checkout would be a
            # second payment for the same receipt.
            raise WalletError(
                "already_decided",
                message="That order is being paid by a saved card.",
            )

        if proposal.status == "pending":
            proposal = self._reserve(proposal)
        if proposal.status != "approved":
            raise WalletError(
                "already_decided",
                message="That order is no longer payable.",
            )

        order_idempotency_key = f"kelp-order-{proposal.id}-checkout"
        try:
            order = self.wallet._transport().create_hosted_order(  # noqa: SLF001
                secret_key=config.secret_key,
                amount_cents=proposal.amount_cents,
                currency=proposal.currency,
                receipt=proposal.id,
                description=proposal.description,
                idempotency_key=order_idempotency_key,
            )
        except RazorpayGatewayError as exc:
            reason = redact_checkout_text(exc.code or "payment_failed")[:200]
            if not exc.retryable:
                self.wallet.ledger.mark_failed(proposal.id, reason=reason)
                raise WalletError(
                    "payment_failed",
                    retryable=False,
                    message="The payment couldn't be started.",
                    details={
                        "reason": redact_checkout_text(str(exc))[:200],
                        "proposal_id": proposal.id,
                    },
                ) from None
            # The provider may or may not have created the order. Keep the
            # reservation and the same key so a retry is deduplicated.
            raise WalletError(
                "payment_failed",
                retryable=True,
                message="We couldn't start the payment. Retry this order.",
                details={
                    "reason": redact_checkout_text(str(exc))[:200],
                    "order_status": proposal.status,
                    "proposal_id": proposal.id,
                    "idempotency_key": order_idempotency_key,
                },
            ) from None

        provider_order_id = order.get("id")
        if (
            not isinstance(provider_order_id, str)
            or _ORDER_RE.match(provider_order_id) is None
        ):
            raise RazorpayGatewayError("Razorpay returned an unexpected order.")

        row, _ = self.wallet.ledger.register_checkout_order(
            provider=PROVIDER,
            provider_order_id=provider_order_id,
            proposal_id=proposal.id,
            receipt=proposal.id,
        )
        return self._result(proposal, row, created=True)

    def _reserve(self, proposal: Proposal) -> Proposal:
        """Move a pending order to ``approved``, reserving its amount."""

        # ``Wallet`` owns the reservation arithmetic; reuse its windows rather
        # than reimplementing cap dates here.
        since, reserved_since, sweep_before = self.wallet._windows()  # noqa: SLF001
        reserved, outcome = self.wallet.ledger.decide(
            proposal.id,
            decision="approve",
            payment_method_id=None,
            monthly_cap_cents=self.wallet.caps.monthly_cents,
            monthly_since=since,
            approved_since=reserved_since,
            expire_before=sweep_before,
        )
        if outcome == "missing":
            raise WalletError("unknown_proposal")
        if outcome == "blocked":
            paid, held = self.wallet.ledger.spend_breakdown(
                proposal.currency, since=since, approved_since=reserved_since
            )
            raise WalletError(
                "monthly_cap_exceeded",
                user_fixable=True,
                details={
                    "amount_cents": proposal.amount_cents,
                    "spent_cents": paid,
                    "reserved_cents": held,
                    "committed_cents": paid + held,
                    "monthly_cap_cents": self.wallet.caps.monthly_cents,
                },
            )
        assert reserved is not None
        return reserved

    def _result(
        self, proposal: Proposal, row: Mapping[str, Any], *, created: bool
    ) -> dict[str, Any]:
        return {
            "checkout": self._payload(proposal, row),
            "proposal": proposal.to_public(),
            "idempotent": not created,
        }

    def _payload(
        self, proposal: Proposal, row: Mapping[str, Any]
    ) -> dict[str, Any]:
        config = self._require_config()
        # Values are safe by construction: the description was redacted by
        # ``Wallet.propose``, the name and URLs were validated here, and the
        # secret is not in this dict. The generic redactor is deliberately not
        # applied to ``key_id``: it is the publishable identifier Checkout
        # needs, and masking it would make the payload unusable.
        return {
            "provider": PROVIDER,
            "mode": config.mode,
            "key_id": config.key_id,
            "order_id": row["provider_order_id"],
            "receipt": row["receipt"],
            "amount_cents": proposal.amount_cents,
            "currency": proposal.currency,
            "name": self.checkout_name,
            "description": proposal.description,
            "callback_url": self.callback_url or None,
            "checkout_url": self.checkout_url or None,
            "notes": {"receipt": row["receipt"]},
            "proposal": proposal.to_public(),
        }

    # -- return confirmation ----------------------------------------------

    def confirm(
        self,
        *,
        razorpay_order_id: str,
        razorpay_payment_id: str,
        razorpay_signature: str,
    ) -> dict[str, Any]:
        """Verify the browser callback and settle the order once."""

        config = self._require_config()
        order_id, payment_id, _ = _verified_ids(
            config.secret_key,
            razorpay_order_id,
            razorpay_payment_id,
            razorpay_signature,
        )
        row = self.wallet.ledger.find_checkout_order(
            provider=PROVIDER, provider_order_id=order_id
        )
        if row is None:
            raise WalletError("unknown_proposal")

        if row.get("provider_payment_id"):
            if row["provider_payment_id"] != payment_id:
                raise WalletError(
                    "already_decided",
                    message="That order was already paid by another payment.",
                )
            proposal = self.wallet.ledger.get_proposal(row["proposal_id"])
            if proposal is None:  # pragma: no cover - row is consistent
                raise WalletError("unknown_proposal")
            return self._settled_result(proposal, idempotent=True)

        proposal = self.wallet.ledger.get_proposal(row["proposal_id"])
        if proposal is None:  # pragma: no cover - row is consistent
            raise WalletError("unknown_proposal")
        if proposal.status == "paid":
            return self._settled_result(proposal, idempotent=True)
        if not _settleable(proposal):
            raise WalletError(
                "already_decided",
                message="That order is no longer payable.",
            )

        payment = self._fetch_payment(config, payment_id)
        settled, already = self._settle_payment(
            proposal, order_id, payment_id, payment
        )
        return self._settled_result(settled, idempotent=already)

    def _fetch_payment(
        self, config: RazorpayConfig, payment_id: str
    ) -> Mapping[str, Any]:
        try:
            return self.wallet._transport().fetch_payment(  # noqa: SLF001
                secret_key=config.secret_key, payment_id=payment_id
            )
        except RazorpayGatewayError as exc:
            raise WalletError(
                "payment_failed",
                retryable=exc.retryable,
                message=(
                    "We couldn't confirm the payment. Retry."
                    if exc.retryable
                    else "The payment wasn't confirmed."
                ),
                details={"reason": redact_checkout_text(str(exc))[:200]},
            ) from None

    def _settle_payment(
        self,
        proposal: Proposal,
        order_id: str,
        payment_id: str,
        payment: Mapping[str, Any],
    ) -> tuple[Proposal, bool]:
        """Check the provider payment against the reserved order, then settle."""

        if not isinstance(payment, Mapping):
            raise WalletError(
                "payment_failed",
                retryable=True,
                message="The payment processor returned an unexpected response.",
            )

        returned_id = payment.get("id")
        if (
            not isinstance(returned_id, str)
            or _PAYMENT_RE.match(returned_id) is None
        ):
            self._fail(proposal.id, "unexpected_payment")
            raise WalletError(
                "payment_mismatch",
                message="The processor returned an unexpected payment.",
            )
        if returned_id != payment_id:
            self._fail(proposal.id, "payment_id_mismatch")
            raise WalletError(
                "payment_mismatch",
                message="The processor returned a different payment.",
            )

        returned_order = payment.get("order_id")
        if returned_order != order_id:
            self._fail(proposal.id, "order_mismatch")
            raise WalletError(
                "payment_mismatch",
                message="The payment belongs to a different order.",
            )

        amount = _minor_units(payment.get("amount"))
        if amount is None or amount != proposal.amount_cents:
            self._fail(proposal.id, "amount_mismatch")
            raise WalletError(
                "payment_mismatch",
                message="The payment amount doesn't match this order.",
                details={
                    "expected_cents": proposal.amount_cents,
                    "received_cents": amount,
                },
            )

        currency = str(payment.get("currency") or "").strip().lower()
        if currency != proposal.currency:
            self._fail(proposal.id, "currency_mismatch")
            raise WalletError(
                "payment_mismatch",
                message="The payment currency doesn't match this order.",
            )

        status = _status(payment.get("status"))
        if status == "authorized":
            captured = self._capture(proposal, payment_id)
            status = _status(captured.get("status"))
            amount = _minor_units(captured.get("amount"))
            if amount is not None and amount != proposal.amount_cents:
                self._fail(proposal.id, "amount_mismatch")
                raise WalletError(
                    "payment_mismatch",
                    message="The captured amount doesn't match this order.",
                )
            captured_currency = (
                str(captured.get("currency") or "").strip().lower()
            )
            if (
                captured_currency
                and captured_currency != proposal.currency
            ):
                self._fail(proposal.id, "currency_mismatch")
                raise WalletError(
                    "payment_mismatch",
                    message="The captured currency doesn't match this order.",
                )

        if status == "failed":
            self._fail(proposal.id, _safe_code(payment.get("error_code")) or "failed")
            raise WalletError(
                "payment_failed",
                retryable=False,
                message="The payment failed. Nothing was charged.",
            )
        if status == "refunded":
            self._fail(proposal.id, "refunded")
            raise WalletError(
                "payment_failed",
                retryable=False,
                message="The payment was refunded. Nothing was kept.",
            )
        if status != "captured":
            raise WalletError(
                "payment_failed",
                retryable=True,
                message="The payment isn't complete yet. Retry.",
            )

        return self.wallet.ledger.settle_checkout(
            provider=PROVIDER,
            provider_order_id=order_id,
            provider_payment_id=payment_id,
            receipt_payment_id=wallet_payment_intent_id(payment_id),
            card_last4=_last4(payment),
        )

    def _capture(
        self, proposal: Proposal, payment_id: str
    ) -> Mapping[str, Any]:
        config = self._require_config()
        try:
            return self.wallet._transport().capture_payment(  # noqa: SLF001
                secret_key=config.secret_key,
                payment_id=payment_id,
                amount_cents=proposal.amount_cents,
                currency=proposal.currency,
            )
        except RazorpayGatewayError as exc:
            raise WalletError(
                "payment_failed",
                retryable=exc.retryable,
                message="The payment couldn't be captured. Retry.",
                details={"reason": redact_checkout_text(str(exc))[:200]},
            ) from None

    def _settled_result(
        self, proposal: Proposal, *, idempotent: bool
    ) -> dict[str, Any]:
        receipt = proposal.receipt
        return {
            "proposal": proposal.to_public(),
            "receipt": receipt.to_public() if receipt is not None else None,
            "idempotent": idempotent,
        }

    def _fail(self, proposal_id: str, reason: str) -> None:
        try:
            self.wallet.ledger.mark_failed(
                proposal_id, reason=redact_checkout_text(reason)[:200]
            )
        except WalletError:  # pragma: no cover - already failed
            pass

    # -- webhooks ----------------------------------------------------------

    def handle_webhook(
        self,
        body: bytes | str,
        *,
        signature: str | None = None,
    ) -> dict[str, Any]:
        """Verify a Razorpay webhook and settle its payment.

        The raw body is what the webhook signature covers, so it is verified
        before any field is trusted for settlement. If no webhook secret is
        configured, a per-payment ``razorpay_signature`` in the event may be
        used instead; in both cases the payment is fetched from Razorpay and
        compared with the reserved order before the ledger is written.
        """

        raw = body.encode("utf-8") if isinstance(body, str) else body
        if not isinstance(raw, bytes) or len(raw) > MAX_JSON_BYTES:
            raise WalletError(
                "invalid_request",
                message="The webhook body is too large.",
                user_fixable=True,
            )
        try:
            event = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise WalletError(
                "invalid_request",
                message="The webhook body is not valid JSON.",
                user_fixable=True,
            ) from None
        if not isinstance(event, Mapping):
            raise WalletError(
                "invalid_request",
                message="The webhook body must be an object.",
                user_fixable=True,
            )

        entity = _payment_entity(event)
        if signature:
            if not self._webhook_secret:
                raise WalletError(
                    "not_configured",
                    message="Razorpay webhooks aren't configured yet.",
                )
            if not verify_webhook_signature(self._webhook_secret, raw, signature):
                raise WalletError(
                    "signature_invalid",
                    status=401,
                    message="That webhook signature is not valid.",
                )
        else:
            config = self._require_config()
            if entity is None:
                raise WalletError(
                    "signature_invalid",
                    status=401,
                    message="That webhook signature is not valid.",
                )
            order_id = entity.get("order_id")
            payment_id = entity.get("id")
            supplied = event.get("razorpay_signature") or entity.get(
                "razorpay_signature"
            )
            if not _verify_pair(
                config.secret_key, order_id, payment_id, supplied
            ):
                raise WalletError(
                    "signature_invalid",
                    status=401,
                    message="That webhook signature is not valid.",
                )

        event_name = str(event.get("event") or "")
        if event_name not in _WEBHOOK_EVENTS:
            return {"handled": False, "event": event_name, "reason": "ignored"}
        if entity is None:
            return {"handled": False, "event": event_name, "reason": "no_payment"}

        order_id = entity.get("order_id")
        payment_id = entity.get("id")
        if (
            not isinstance(order_id, str)
            or _ORDER_RE.match(order_id) is None
            or not isinstance(payment_id, str)
            or _PAYMENT_RE.match(payment_id) is None
        ):
            raise WalletError(
                "invalid_request",
                message="The webhook payment ids aren't valid.",
                user_fixable=True,
            )

        row = self.wallet.ledger.find_checkout_order(
            provider=PROVIDER, provider_order_id=order_id
        )
        if row is None:
            # A signed event for an order this ledger never created is
            # acknowledged so Razorpay stops retrying, but nothing is settled.
            return {"handled": False, "event": event_name, "reason": "unknown_order"}

        if row.get("provider_payment_id"):
            if row["provider_payment_id"] != payment_id:
                return {
                    "handled": False,
                    "event": event_name,
                    "reason": "already_paid",
                }
            proposal = self.wallet.ledger.get_proposal(row["proposal_id"])
            if proposal is None:  # pragma: no cover
                return {"handled": False, "event": event_name, "reason": "unknown_order"}
            return {
                "handled": True,
                "event": event_name,
                **self._settled_result(proposal, idempotent=True),
            }

        proposal = self.wallet.ledger.get_proposal(row["proposal_id"])
        if proposal is None:  # pragma: no cover
            return {"handled": False, "event": event_name, "reason": "unknown_order"}
        if proposal.status == "paid":
            return {
                "handled": True,
                "event": event_name,
                **self._settled_result(proposal, idempotent=True),
            }
        if not _settleable(proposal):
            return {
                "handled": False,
                "event": event_name,
                "reason": "not_payable",
            }

        config = self._require_config()
        payment = self._fetch_payment(config, payment_id)
        try:
            settled, already = self._settle_payment(
                proposal, order_id, payment_id, payment
            )
        except WalletError as exc:
            if exc.retryable:
                # Let Razorpay retry; the reservation is still held.
                raise
            stored = self.wallet.ledger.get_proposal(proposal.id)
            return {
                "handled": True,
                "event": event_name,
                "status": stored.status if stored is not None else "failed",
                "error": exc.code,
                "idempotent": False,
            }
        return {
            "handled": True,
            "event": event_name,
            **self._settled_result(settled, idempotent=already),
        }

    # -- configuration -----------------------------------------------------

    def _require_config(self) -> RazorpayConfig:
        return self.wallet._require_config()  # noqa: SLF001


# -- helpers ---------------------------------------------------------------


def _verified_ids(
    secret_key: str,
    order_id: Any,
    payment_id: Any,
    signature: Any,
) -> tuple[str, str, str]:
    if not isinstance(order_id, str) or _ORDER_RE.match(order_id) is None:
        raise WalletError(
            "invalid_request",
            message="razorpay_order_id is required.",
            user_fixable=True,
            field="razorpay_order_id",
        )
    if not isinstance(payment_id, str) or _PAYMENT_RE.match(payment_id) is None:
        raise WalletError(
            "invalid_request",
            message="razorpay_payment_id is required.",
            user_fixable=True,
            field="razorpay_payment_id",
        )
    if not isinstance(signature, str) or not signature.strip():
        raise WalletError(
            "signature_invalid",
            status=401,
            message="That payment couldn't be verified.",
        )
    if not verify_payment_signature(secret_key, order_id, payment_id, signature):
        raise WalletError(
            "signature_invalid",
            status=401,
            message="That payment couldn't be verified.",
        )
    return order_id, payment_id, signature.strip()


def _settleable(proposal: Proposal) -> bool:
    """Approved, or a reservation that expired before a late hosted payment."""

    return proposal.status == "approved" or (
        proposal.status == "failed"
        and proposal.reason == APPROVAL_EXPIRED_REASON
    )


def _verify_pair(
    secret_key: str, order_id: Any, payment_id: Any, signature: Any
) -> bool:
    if (
        not isinstance(order_id, str)
        or _ORDER_RE.match(order_id) is None
        or not isinstance(payment_id, str)
        or _PAYMENT_RE.match(payment_id) is None
        or not isinstance(signature, str)
    ):
        return False
    return verify_payment_signature(secret_key, order_id, payment_id, signature)


def _payment_entity(event: Mapping[str, Any]) -> Mapping[str, Any] | None:
    payload = event.get("payload")
    if isinstance(payload, Mapping):
        payment = payload.get("payment")
        if isinstance(payment, Mapping):
            entity = payment.get("entity")
            if isinstance(entity, Mapping):
                return entity
    payment = event.get("payment")
    if isinstance(payment, Mapping):
        entity = payment.get("entity", payment)
        if isinstance(entity, Mapping):
            return entity
    if isinstance(event.get("id"), str) and _PAYMENT_RE.match(str(event["id"])):
        return event
    return None


def _minor_units(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _status(value: Any) -> str:
    return _STATUS_MAP.get(str(value or "").strip().lower(), "unknown")


def _safe_code(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return redact_checkout_text(value.strip())[:64]


def _last4(payment: Mapping[str, Any]) -> str | None:
    card = payment.get("card")
    if isinstance(card, Mapping):
        last4 = card.get("last4")
        if isinstance(last4, str) and re.fullmatch(r"\d{4}", last4.strip()):
            return last4.strip()
    return None


def _safe_name(value: Any) -> str:
    text = redact_checkout_text(value if isinstance(value, str) else "")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return DEFAULT_CHECKOUT_NAME
    return text[:_MAX_NAME_CHARS]


def _safe_url(value: Any, *, field: str) -> str:
    text = (value or "").strip() if isinstance(value, str) else ""
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
    except ValueError:
        raise WalletError(
            "invalid_config", message=f"{field} must be an https URL."
        ) from None
    if parsed.scheme != "https" or not parsed.netloc or len(text) > 2048:
        raise WalletError(
            "invalid_config", message=f"{field} must be an https URL."
        )
    return text

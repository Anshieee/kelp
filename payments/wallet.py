"""KEL-10 Stripe wallet scaffold: saved cards, proposals, approvals, spend caps.

The design rule is that **the Stripe test key is the only missing piece**. Every
other behaviour in this module is real, covered by tests, and runs without a
network: HTTP access sits behind the :class:`StripeGateway` protocol, tests
inject a fake, and :class:`HttpStripeGateway` is only constructed when a call is
actually made.

Security boundaries:

- ``STRIPE_SECRET_KEY`` is read from the server environment only. It is never
  accepted from a browser, never written to the ledger, and never appears in
  ``repr``, ``str``, status payloads, error text, or the SQLite file.
- Card details are never stored. A payment method is reduced to an allowlisted
  view (brand, last4, expiry); raw card numbers, CVCs, and other Stripe
  payload fields are dropped. Card-like input is rejected, not echoed.
- Pending orders are single-use. ``approve``/``deny`` is an atomic compare-and-
  set, and the monthly cap is checked again inside that transition, so two
  concurrent approvals cannot overspend.
- Every order carries its own idempotency key. The key is stored with the
  order and reused for the Stripe charge, so a retried request cannot create a
  second charge for the same order.

Configuration (server-only environment):

==============================  ==========================================
``STRIPE_SECRET_KEY``           Stripe *secret* test key. Empty = wallet is
                                visible but cannot take payments.
``STRIPE_CUSTOMER_ID``          Optional Stripe customer (``cus_...``).
``WALLET_PER_PURCHASE_CAP_CENTS``  Per-order cap; default ``5000`` ($50.00).
``WALLET_MONTHLY_CAP_CENTS``    Monthly cap per currency; default ``20000``.
==============================  ==========================================

The companion HTTP surface is ``chat.wallet``. For a durable wallet, pass
``ledger=WalletLedger("var/wallet/wallet.db")``; the default in-memory ledger
is for tests and one-off runs.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol

from .stripe_wallet import STRIPE_SECRET_KEY_ENV, StripeConfig, WalletNotConfigured

__all__ = [
    "CUSTOMER_ID_ENV",
    "DEFAULT_CURRENCY",
    "DEFAULT_MONTHLY_CAP_CENTS",
    "DEFAULT_PER_PURCHASE_CAP_CENTS",
    "MONTHLY_CAP_ENV",
    "PER_PURCHASE_CAP_ENV",
    "Card",
    "HttpStripeGateway",
    "Proposal",
    "Receipt",
    "SpendCaps",
    "StripeGateway",
    "StripeGatewayError",
    "Wallet",
    "WalletError",
    "WalletLedger",
    "WalletNotConfigured",
    "card_from_payment_method",
    "contains_raw_card_data",
    "normalize_currency",
    "redact_payload",
    "redact_text",
]

DEFAULT_CURRENCY = "usd"
PER_PURCHASE_CAP_ENV = "WALLET_PER_PURCHASE_CAP_CENTS"
MONTHLY_CAP_ENV = "WALLET_MONTHLY_CAP_CENTS"
CUSTOMER_ID_ENV = "STRIPE_CUSTOMER_ID"

DEFAULT_PER_PURCHASE_CAP_CENTS = 5_000
DEFAULT_MONTHLY_CAP_CENTS = 20_000
MAX_AMOUNT_CENTS = 99_999_999
MAX_DESCRIPTION_CHARS = 200
MAX_IDEMPOTENCY_CHARS = 80

_CURRENCY_RE = re.compile(r"^[a-z]{3}$")
_PAYMENT_METHOD_RE = re.compile(r"^pm_[A-Za-z0-9]{4,}$")
_CUSTOMER_RE = re.compile(r"^cus_[A-Za-z0-9]{4,}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,%d}$" % MAX_IDEMPOTENCY_CHARS)
_PAYMENT_INTENT_RE = re.compile(r"^pi_[A-Za-z0-9_]{4,}$")
_SETUP_INTENT_RE = re.compile(r"^seti_[A-Za-z0-9_]{4,}$")
_BRAND_RE = re.compile(r"^[a-z][a-z0-9 _-]{0,31}$")

# Card-like strings: 13-19 digits with optional single separators. Over-matches
# are acceptable; hiding a number is cheaper than leaking one.
_PAN_RE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_STRIPE_KEY_RE = re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{4,}")
_CARD_DATA_KEY_RE = re.compile(
    r"(?i)^(?:number|card_number|pan|cvc|cvv|cvc2|security_code|encrypted_number)$"
)
_REDACT_KEY_RE = re.compile(
    r"(?i)(number|pan|cvc|cvv|security_code|secret|password|token|authorization|credential)"
)

REDACTED_CARD = "[redacted-card]"
REDACTED_VALUE = "[redacted]"

_STATUS_BY_CODE = {
    "invalid_config": 500,
    "invalid_request": 400,
    "invalid_amount": 400,
    "invalid_currency": 400,
    "invalid_payment_method": 400,
    "invalid_idempotency_key": 400,
    "unstorable_card": 422,
    "cap_exceeded": 409,
    "monthly_cap_exceeded": 409,
    "unknown_proposal": 404,
    "unknown_card": 404,
    "no_saved_card": 409,
    "already_decided": 409,
    "payment_failed": 502,
    "gateway_error": 502,
    "not_configured": 503,
    "not_found": 404,
    "unauthorized": 401,
    "csrf_failed": 403,
}


# -- errors and redaction --------------------------------------------------


class WalletError(Exception):
    """A wallet failure with a safe, user-facing body.

    ``details`` must never contain card data or secret material; callers build
    it from already-redacted values.
    """

    def __init__(
        self,
        code: str,
        *,
        status: int | None = None,
        message: str | None = None,
        retryable: bool = False,
        user_fixable: bool = False,
        field: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.status = status if status is not None else _STATUS_BY_CODE.get(code, 400)
        self.message = message or _DEFAULT_MESSAGES.get(code, "That didn't work.")
        self.retryable = retryable
        self.user_fixable = user_fixable
        self.field = field
        self.details = dict(details) if details else {}
        super().__init__(self.message)

    def to_body(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "user_fixable": self.user_fixable,
        }
        if self.field is not None:
            error["field"] = self.field
        if self.details:
            error["details"] = dict(self.details)
        return {"ok": False, "error": error}


_DEFAULT_MESSAGES = {
    "invalid_config": "The wallet is misconfigured.",
    "invalid_request": "That request wasn't understood.",
    "invalid_amount": "Enter an amount greater than zero.",
    "invalid_currency": "That currency isn't supported.",
    "invalid_payment_method": "That card couldn't be read.",
    "invalid_idempotency_key": "That order key isn't valid.",
    "unstorable_card": "Card details can't be stored here. Use the card form.",
    "cap_exceeded": "That's above your per-purchase limit.",
    "monthly_cap_exceeded": "That would go over your monthly limit.",
    "unknown_proposal": "That order doesn't exist.",
    "unknown_card": "That card isn't saved.",
    "no_saved_card": "Add a card before paying.",
    "already_decided": "That order was already answered.",
    "payment_failed": "The card was not charged. Try again.",
    "gateway_error": "Can't reach the card processor right now.",
    "not_configured": "The wallet isn't connected yet.",
    "not_found": "Unknown endpoint.",
    "unauthorized": "Sign in to use the wallet.",
    "csrf_failed": "That request didn't come from the app.",
}


def redact_text(text: str) -> str:
    """Mask card numbers and Stripe keys in free text."""

    if not isinstance(text, str):
        text = str(text)
    text = _PAN_RE.sub(REDACTED_CARD, text)
    return _STRIPE_KEY_RE.sub(REDACTED_VALUE, text)


def redact_payload(value: Any) -> Any:
    """Recursively redact card- and secret-looking data in a JSON-like value."""

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if _REDACT_KEY_RE.search(name):
                cleaned[name] = REDACTED_VALUE
            else:
                cleaned[name] = redact_payload(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [redact_payload(item) for item in value]
    return value


def contains_raw_card_data(value: Any) -> bool:
    """True when a payload carries a PAN-like string or a card-secret key."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if _CARD_DATA_KEY_RE.match(str(key)) and item not in (None, "", False):
                return True
            if contains_raw_card_data(item):
                return True
        return False
    if isinstance(value, str):
        return bool(_PAN_RE.search(value))
    if isinstance(value, (list, tuple)):
        return any(contains_raw_card_data(item) for item in value)
    return False


# -- value objects ---------------------------------------------------------


def normalize_currency(value: str | None) -> str:
    if value is None or value == "":
        return DEFAULT_CURRENCY
    if not isinstance(value, str):
        raise WalletError("invalid_currency", user_fixable=True)
    currency = value.strip().lower()
    if not _CURRENCY_RE.match(currency):
        raise WalletError("invalid_currency", user_fixable=True)
    return currency


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _month_start(value: datetime) -> str:
    start = value.astimezone(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    return start.isoformat()


@dataclass(frozen=True)
class Card:
    """The only card fields this app keeps or shows."""

    id: str
    brand: str
    last4: str
    exp_month: int
    exp_year: int
    is_default: bool = False

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "brand": self.brand,
            "last4": self.last4,
            "exp_month": self.exp_month,
            "exp_year": self.exp_year,
            "is_default": self.is_default,
        }

    def __repr__(self) -> str:
        return f"Card(brand={self.brand!r}, last4={self.last4!r})"


@dataclass(frozen=True)
class Receipt:
    proposal_id: str
    payment_intent_id: str
    amount_cents: int
    currency: str
    last4: str | None
    paid_at: str

    def to_public(self) -> dict[str, Any]:
        return {
            "order_id": self.proposal_id,
            "payment_intent_id": self.payment_intent_id,
            "amount_cents": self.amount_cents,
            "currency": self.currency,
            "last4": self.last4,
            "paid_at": self.paid_at,
        }


@dataclass(frozen=True)
class Proposal:
    id: str
    amount_cents: int
    currency: str
    description: str
    status: str
    created_at: str
    idempotency_key: str = field(repr=False, default="")
    payment_method_id: str | None = None
    stripe_payment_intent_id: str | None = None
    card_last4: str | None = None
    decided_at: str | None = None
    paid_at: str | None = None
    reason: str | None = None

    @property
    def receipt(self) -> Receipt | None:
        if self.status != "paid" or not self.stripe_payment_intent_id or not self.paid_at:
            return None
        return Receipt(
            proposal_id=self.id,
            payment_intent_id=self.stripe_payment_intent_id,
            amount_cents=self.amount_cents,
            currency=self.currency,
            last4=self.card_last4,
            paid_at=self.paid_at,
        )

    def to_public(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": self.id,
            "amount_cents": self.amount_cents,
            "currency": self.currency,
            "description": self.description,
            "status": self.status,
            "created_at": self.created_at,
            "payment_method_id": self.payment_method_id,
            "decided_at": self.decided_at,
        }
        receipt = self.receipt
        if receipt is not None:
            body["receipt"] = receipt.to_public()
        if self.reason:
            body["reason"] = self.reason
        return body

    def __repr__(self) -> str:
        return (
            f"Proposal(id={self.id!r}, amount_cents={self.amount_cents}, "
            f"currency={self.currency!r}, status={self.status!r})"
        )


@dataclass(frozen=True)
class SpendCaps:
    per_purchase_cents: int = DEFAULT_PER_PURCHASE_CAP_CENTS
    monthly_cents: int = DEFAULT_MONTHLY_CAP_CENTS

    def __post_init__(self) -> None:
        for name, value in (
            ("per_purchase_cents", self.per_purchase_cents),
            ("monthly_cents", self.monthly_cents),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise WalletError(
                    "invalid_config",
                    message=f"{name} must be a positive integer number of cents.",
                )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SpendCaps":
        source = os.environ if env is None else env
        return cls(
            per_purchase_cents=_env_cents(
                source, PER_PURCHASE_CAP_ENV, DEFAULT_PER_PURCHASE_CAP_CENTS
            ),
            monthly_cents=_env_cents(
                source, MONTHLY_CAP_ENV, DEFAULT_MONTHLY_CAP_CENTS
            ),
        )

    def to_public(self, *, currency: str, spent_cents: int) -> dict[str, Any]:
        return {
            "currency": currency,
            "per_purchase_cents": self.per_purchase_cents,
            "monthly_cents": self.monthly_cents,
            "spent_cents": spent_cents,
            "remaining_cents": max(0, self.monthly_cents - spent_cents),
        }


def _env_cents(source: Mapping[str, str], name: str, default: int) -> int:
    raw = (source.get(name) or "").strip()
    if not raw:
        return default
    if not raw.isdigit() or int(raw) <= 0:
        raise WalletError(
            "invalid_config",
            message=f"{name} must be a positive integer number of cents.",
        )
    return int(raw)


def card_from_payment_method(payload: Mapping[str, Any]) -> Card:
    """Reduce a Stripe payment-method object to the fields we are allowed to keep.

    Anything outside the allowlist is discarded. A payload carrying raw card
    data is rejected outright so it can never reach the ledger or a response.
    """

    if not isinstance(payload, Mapping):
        raise WalletError("invalid_payment_method", user_fixable=True)
    if contains_raw_card_data(payload):
        raise WalletError("unstorable_card", user_fixable=True)

    payment_method_id = payload.get("id")
    if not isinstance(payment_method_id, str) or not _PAYMENT_METHOD_RE.match(
        payment_method_id
    ):
        raise WalletError("invalid_payment_method", user_fixable=True)

    card = payload.get("card")
    if not isinstance(card, Mapping):
        raise WalletError(
            "invalid_payment_method",
            message="That payment method isn't a card.",
            user_fixable=True,
        )

    brand = str(card.get("brand") or "").strip().lower()
    if not _BRAND_RE.match(brand):
        raise WalletError("invalid_payment_method", user_fixable=True)

    last4 = str(card.get("last4") or "").strip()
    if not re.fullmatch(r"\d{4}", last4):
        raise WalletError("invalid_payment_method", user_fixable=True)

    exp_month = card.get("exp_month")
    exp_year = card.get("exp_year")
    if (
        isinstance(exp_month, bool)
        or not isinstance(exp_month, int)
        or not 1 <= exp_month <= 12
    ):
        raise WalletError("invalid_payment_method", user_fixable=True)
    if (
        isinstance(exp_year, bool)
        or not isinstance(exp_year, int)
        or not 2000 <= exp_year <= 2100
    ):
        raise WalletError("invalid_payment_method", user_fixable=True)

    return Card(
        id=payment_method_id,
        brand=brand,
        last4=last4,
        exp_month=exp_month,
        exp_year=exp_year,
    )


# -- ledger ----------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL DEFAULT '',
    brand TEXT NOT NULL,
    last4 TEXT NOT NULL,
    exp_month INTEGER NOT NULL,
    exp_year INTEGER NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    currency TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'denied', 'blocked', 'approved', 'paid', 'failed')
    ),
    payment_method_id TEXT,
    stripe_payment_intent_id TEXT,
    card_last4 TEXT,
    reason TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    paid_at TEXT
);

CREATE INDEX IF NOT EXISTS proposals_spend
    ON proposals (status, currency, paid_at);
"""


class WalletLedger:
    """SQLite ledger for saved-card views, orders, and the spend total.

    One connection, one write lock, ``0600`` for on-disk ledgers. ``:memory:``
    is for tests and throwaway runs. No secret or card number is ever a column.
    """

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        raw = str(path)
        self.path = None if raw == ":memory:" else Path(path)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._now = now or _utc_now
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            ":memory:" if self.path is None else str(self.path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        if self.path is not None:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SCHEMA)
        if self.path is not None:
            try:
                os.chmod(self.path, 0o600)
            except OSError:  # pragma: no cover - platform dependent
                pass

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "WalletLedger":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- cards -------------------------------------------------------------

    def add_card(
        self,
        card: Card,
        *,
        customer_id: str = "",
        make_default: bool = False,
    ) -> Card:
        now = _iso(self._now())
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._conn.execute(
                    "SELECT is_default FROM cards WHERE id = ?", (card.id,)
                ).fetchone()
                is_default = bool(make_default) or existing is None or bool(
                    existing["is_default"]
                )
                if is_default:
                    self._conn.execute("UPDATE cards SET is_default = 0")
                self._conn.execute(
                    """
                    INSERT INTO cards
                        (id, customer_id, brand, last4, exp_month, exp_year,
                         is_default, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        customer_id = excluded.customer_id,
                        brand = excluded.brand,
                        last4 = excluded.last4,
                        exp_month = excluded.exp_month,
                        exp_year = excluded.exp_year,
                        is_default = excluded.is_default
                    """,
                    (
                        card.id,
                        customer_id,
                        card.brand,
                        card.last4,
                        card.exp_month,
                        card.exp_year,
                        1 if is_default else 0,
                        now,
                    ),
                )
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")
        stored = self.get_card(card.id)
        assert stored is not None  # just written
        return stored

    def list_cards(self) -> list[Card]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cards ORDER BY is_default DESC, created_at DESC, id DESC"
            ).fetchall()
        return [self._row_card(row) for row in rows]

    def get_card(self, payment_method_id: str) -> Card | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM cards WHERE id = ?", (payment_method_id,)
            ).fetchone()
        return None if row is None else self._row_card(row)

    def default_card(self) -> Card | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM cards
                ORDER BY is_default DESC, created_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
        return None if row is None else self._row_card(row)

    # -- proposals ---------------------------------------------------------

    def find_proposal_by_key(self, idempotency_key: str) -> Proposal | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM proposals WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return None if row is None else self._row_proposal(row)

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
        return None if row is None else self._row_proposal(row)

    def create_proposal(
        self,
        *,
        amount_cents: int,
        currency: str,
        description: str,
        idempotency_key: str,
    ) -> tuple[Proposal, bool]:
        """Insert an order, or return the existing order for that key.

        Returns ``(proposal, created)``. The unique key is what makes a retried
        ``propose`` safe.
        """

        now = _iso(self._now())
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM proposals WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                return self._row_proposal(row), False
            proposal_id = "ord_" + uuid.uuid4().hex
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """
                    INSERT INTO proposals
                        (id, idempotency_key, amount_cents, currency, description,
                         status, created_at)
                    VALUES (?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        proposal_id,
                        idempotency_key,
                        amount_cents,
                        currency,
                        description,
                        now,
                    ),
                )
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")
        created = self.get_proposal(proposal_id)
        assert created is not None  # just inserted
        return created, True

    def decide(
        self,
        proposal_id: str,
        *,
        decision: str,
        payment_method_id: str | None,
        monthly_cap_cents: int,
        monthly_since: str,
    ) -> tuple[Proposal | None, str]:
        """Atomically answer a pending order.

        Outcomes: ``approved``, ``denied``, ``blocked`` (cap), ``conflict``
        (already answered), ``missing``. The monthly total is read inside the
        same transaction as the state change.
        """

        now = _iso(self._now())
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
                ).fetchone()
                if row is None:
                    self._conn.execute("ROLLBACK")
                    return None, "missing"
                if row["status"] != "pending":
                    self._conn.execute("ROLLBACK")
                    return self._row_proposal(row), "conflict"

                if decision == "deny":
                    self._conn.execute(
                        """
                        UPDATE proposals
                        SET status = 'denied', decided_at = ?
                        WHERE id = ? AND status = 'pending'
                        """,
                        (now, proposal_id),
                    )
                    outcome = "denied"
                else:
                    spent = self._spend_in_transaction(
                        row["currency"], monthly_since
                    )
                    if spent + int(row["amount_cents"]) > monthly_cap_cents:
                        self._conn.execute(
                            """
                            UPDATE proposals
                            SET status = 'blocked', reason = ?,
                                payment_method_id = ?, decided_at = ?
                            WHERE id = ? AND status = 'pending'
                            """,
                            (
                                "monthly_cap_exceeded",
                                payment_method_id,
                                now,
                                proposal_id,
                            ),
                        )
                        outcome = "blocked"
                    else:
                        self._conn.execute(
                            """
                            UPDATE proposals
                            SET status = 'approved', payment_method_id = ?,
                                decided_at = ?
                            WHERE id = ? AND status = 'pending'
                            """,
                            (payment_method_id, now, proposal_id),
                        )
                        outcome = "approved"
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")
            final = self._conn.execute(
                "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
        assert final is not None
        return self._row_proposal(final), outcome

    def mark_paid(
        self,
        proposal_id: str,
        *,
        payment_intent_id: str,
        card_last4: str | None,
    ) -> Proposal:
        now = _iso(self._now())
        with self._lock:
            updated = self._conn.execute(
                """
                UPDATE proposals
                SET status = 'paid', stripe_payment_intent_id = ?,
                    card_last4 = ?, paid_at = ?
                WHERE id = ? AND status = 'approved'
                """,
                (payment_intent_id, card_last4, now, proposal_id),
            ).rowcount
        if updated != 1:  # pragma: no cover - guarded by decide()
            raise WalletError("already_decided", status=409)
        proposal = self.get_proposal(proposal_id)
        assert proposal is not None
        return proposal

    def mark_failed(self, proposal_id: str, *, reason: str) -> Proposal:
        safe_reason = redact_text(str(reason))[:200]
        with self._lock:
            self._conn.execute(
                """
                UPDATE proposals
                SET status = 'failed', reason = ?
                WHERE id = ? AND status = 'approved'
                """,
                (safe_reason, proposal_id),
            )
        proposal = self.get_proposal(proposal_id)
        if proposal is None:  # pragma: no cover - called with a live proposal
            raise WalletError("unknown_proposal")
        return proposal

    def spend_cents(self, currency: str, *, since: str) -> int:
        with self._lock:
            return self._spend_in_transaction(currency, since)

    def _spend_in_transaction(self, currency: str, since: str) -> int:
        row = self._conn.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS total
            FROM proposals
            WHERE status = 'paid' AND currency = ? AND paid_at >= ?
            """,
            (currency, since),
        ).fetchone()
        return int(row["total"])

    # -- rows --------------------------------------------------------------

    @staticmethod
    def _row_card(row: sqlite3.Row) -> Card:
        return Card(
            id=row["id"],
            brand=row["brand"],
            last4=row["last4"],
            exp_month=int(row["exp_month"]),
            exp_year=int(row["exp_year"]),
            is_default=bool(row["is_default"]),
        )

    @staticmethod
    def _row_proposal(row: sqlite3.Row) -> Proposal:
        return Proposal(
            id=row["id"],
            idempotency_key=row["idempotency_key"],
            amount_cents=int(row["amount_cents"]),
            currency=row["currency"],
            description=row["description"],
            status=row["status"],
            payment_method_id=row["payment_method_id"],
            stripe_payment_intent_id=row["stripe_payment_intent_id"],
            card_last4=row["card_last4"],
            decided_at=row["decided_at"],
            paid_at=row["paid_at"],
            reason=row["reason"],
            created_at=row["created_at"],
        )


# -- Stripe transport ------------------------------------------------------


class StripeGatewayError(RuntimeError):
    """A transport-level Stripe failure with a card-data-free message."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(redact_text(message))


class StripeGateway(Protocol):
    """The five network calls the wallet needs. Tests supply a fake."""

    def create_setup_intent(
        self,
        *,
        secret_key: str,
        customer_id: str,
        metadata: Mapping[str, str],
    ) -> Mapping[str, Any]: ...

    def attach_payment_method(
        self,
        *,
        secret_key: str,
        payment_method_id: str,
        customer_id: str,
    ) -> Mapping[str, Any]: ...

    def list_payment_methods(
        self,
        *,
        secret_key: str,
        customer_id: str,
    ) -> Iterable[Mapping[str, Any]]: ...

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
    ) -> Mapping[str, Any]: ...


class HttpStripeGateway:
    """Real Stripe transport. Never used by tests; only constructed on a call."""

    base_url = "https://api.stripe.com/v1"

    def __init__(self, *, timeout: float = 20.0) -> None:
        self.timeout = timeout
        self._client: Any = None
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    def _http(self) -> Any:
        # Imported lazily so importing the wallet never touches the network stack
        # beyond httpx's module import.
        import httpx

        with self._lock:
            if self._client is None:
                self._client = httpx.Client(timeout=self.timeout)
            return self._client

    def _request(
        self,
        method: str,
        path: str,
        *,
        secret_key: str,
        data: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> Mapping[str, Any]:
        import httpx

        headers = {
            "Authorization": f"Bearer {secret_key}",
            "Accept": "application/json",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = self._http().request(
                method,
                f"{self.base_url}{path}",
                data=dict(data or {}),
                params=dict(params or {}),
                headers=headers,
            )
        except httpx.HTTPError:
            raise StripeGatewayError(
                "Can't reach the card processor right now.", retryable=True
            ) from None

        if response.status_code >= 400:
            code: str | None = None
            try:
                body = response.json()
                code = str((body.get("error") or {}).get("code") or "") or None
            except (ValueError, AttributeError):  # pragma: no cover - odd body
                code = None
            raise StripeGatewayError(
                f"Stripe rejected the request ({response.status_code}).",
                code=code,
                retryable=response.status_code in {409, 429, 500, 502, 503, 504},
            )

        try:
            payload = response.json()
        except ValueError:  # pragma: no cover - Stripe always returns JSON
            raise StripeGatewayError("Stripe returned an unreadable response.") from None
        if not isinstance(payload, Mapping):
            raise StripeGatewayError("Stripe returned an unexpected response.")
        return payload

    def create_setup_intent(
        self,
        *,
        secret_key: str,
        customer_id: str,
        metadata: Mapping[str, str],
    ) -> Mapping[str, Any]:
        data = {"usage": "off_session", "payment_method_types[0]": "card"}
        if customer_id:
            data["customer"] = customer_id
        for key, value in metadata.items():
            data[f"metadata[{key}]"] = str(value)
        return self._request(
            "POST", "/setup_intents", secret_key=secret_key, data=data
        )

    def attach_payment_method(
        self,
        *,
        secret_key: str,
        payment_method_id: str,
        customer_id: str,
    ) -> Mapping[str, Any]:
        if not customer_id:
            raise WalletError(
                "invalid_config",
                message="Set STRIPE_CUSTOMER_ID before saving a card.",
            )
        return self._request(
            "POST",
            f"/payment_methods/{payment_method_id}/attach",
            secret_key=secret_key,
            data={"customer": customer_id},
        )

    def list_payment_methods(
        self,
        *,
        secret_key: str,
        customer_id: str,
    ) -> Iterable[Mapping[str, Any]]:
        if not customer_id:
            raise WalletError(
                "invalid_config",
                message="Set STRIPE_CUSTOMER_ID before listing cards.",
            )
        payload = self._request(
            "GET",
            "/payment_methods",
            secret_key=secret_key,
            params={"customer": customer_id, "type": "card", "limit": "20"},
        )
        items = payload.get("data")
        return [item for item in items or [] if isinstance(item, Mapping)]

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
        data = {
            "amount": str(amount_cents),
            "currency": currency,
            "payment_method": payment_method_id,
            "confirm": "true",
            "off_session": "true",
            "description": description,
        }
        if customer_id:
            data["customer"] = customer_id
        return self._request(
            "POST",
            "/payment_intents",
            secret_key=secret_key,
            data=data,
            idempotency_key=idempotency_key,
        )


# -- wallet service --------------------------------------------------------


class Wallet:
    """Saved cards, orders, approvals, and caps for one owner."""

    provider = "stripe"

    def __init__(
        self,
        *,
        config: StripeConfig | None,
        caps: SpendCaps | None = None,
        ledger: WalletLedger | None = None,
        gateway: StripeGateway | None = None,
        customer_id: str = "",
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self.caps = caps or SpendCaps()
        self.ledger = ledger or WalletLedger(":memory:", now=now_fn)
        self._gateway = gateway
        self.customer_id = customer_id.strip()
        if self.customer_id and not _CUSTOMER_RE.match(self.customer_id):
            raise WalletError(
                "invalid_config",
                message="STRIPE_CUSTOMER_ID must look like cus_….",
            )
        self._now = now_fn or _utc_now

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        ledger: WalletLedger | None = None,
        gateway: StripeGateway | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> "Wallet":
        source = os.environ if env is None else env
        key = (source.get(STRIPE_SECRET_KEY_ENV) or "").strip()
        config = StripeConfig(key) if key else None
        return cls(
            config=config,
            caps=SpendCaps.from_env(source),
            ledger=ledger,
            gateway=gateway,
            customer_id=(source.get(CUSTOMER_ID_ENV) or "").strip(),
            now_fn=now_fn,
        )

    # -- introspection -----------------------------------------------------

    @property
    def configured(self) -> bool:
        return self._config is not None

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "configured": self.configured,
            "mode": self._config.mode if self._config is not None else "unconfigured",
            "network_calls": False,
            "saved_cards": len(self.ledger.list_cards()),
            "per_purchase_cap_cents": self.caps.per_purchase_cents,
            "monthly_cap_cents": self.caps.monthly_cents,
        }

    def limits(self, currency: str | None = None) -> dict[str, Any]:
        code = normalize_currency(currency)
        spent = self.ledger.spend_cents(code, since=_month_start(self._now()))
        return self.caps.to_public(currency=code, spent_cents=spent)

    def list_cards(self) -> list[dict[str, Any]]:
        return [card.to_public() for card in self.ledger.list_cards()]

    def __repr__(self) -> str:
        mode = self._config.mode if self._config is not None else "unconfigured"
        return f"Wallet(provider='stripe', mode={mode!r}, configured={self.configured})"

    __str__ = __repr__

    # -- Stripe ------------------------------------------------------------

    def _require_config(self) -> StripeConfig:
        if self._config is None:
            raise WalletNotConfigured()
        if self._config.mode not in {"test", "live"}:
            # A publishable key in the secret slot must never be sent as a
            # bearer token; refuse loudly instead of trying to use it.
            raise WalletError(
                "invalid_config",
                message="STRIPE_SECRET_KEY must be a Stripe secret key.",
            )
        return self._config

    def _transport(self) -> StripeGateway:
        if self._gateway is None:
            self._gateway = HttpStripeGateway()
        return self._gateway

    def create_setup_intent(self) -> dict[str, Any]:
        config = self._require_config()
        payload = self._transport().create_setup_intent(
            secret_key=config.secret_key,
            customer_id=self.customer_id,
            metadata={"source": "kelp-wallet"},
        )
        intent_id = payload.get("id")
        client_secret = payload.get("client_secret")
        if (
            not isinstance(intent_id, str)
            or not _SETUP_INTENT_RE.match(intent_id)
            or not isinstance(client_secret, str)
            or not client_secret
        ):
            raise WalletError(
                "gateway_error",
                message="The card processor returned an unexpected response.",
                retryable=True,
            )
        return {
            "id": intent_id,
            "client_secret": client_secret,
            "customer_id": self.customer_id or None,
        }

    def attach(
        self,
        payment_method_id: str,
        *,
        make_default: bool = False,
    ) -> dict[str, Any]:
        config = self._require_config()
        if (
            not isinstance(payment_method_id, str)
            or not _PAYMENT_METHOD_RE.match(payment_method_id)
        ):
            raise WalletError("invalid_payment_method", user_fixable=True)
        payload = self._transport().attach_payment_method(
            secret_key=config.secret_key,
            payment_method_id=payment_method_id,
            customer_id=self.customer_id,
        )
        card = card_from_payment_method(payload)
        stored = self.ledger.add_card(
            card, customer_id=self.customer_id, make_default=make_default
        )
        return stored.to_public()

    def refresh_cards(self) -> list[dict[str, Any]]:
        config = self._require_config()
        payloads = self._transport().list_payment_methods(
            secret_key=config.secret_key, customer_id=self.customer_id
        )
        for payload in payloads:
            card = card_from_payment_method(payload)
            self.ledger.add_card(card, customer_id=self.customer_id)
        return self.list_cards()

    # -- orders ------------------------------------------------------------

    def propose(
        self,
        amount_cents: int,
        *,
        currency: str | None = None,
        description: str = "",
        idempotency_key: str | None = None,
    ) -> tuple[Proposal, bool]:
        """Create a pending order. Returns ``(proposal, created)``.

        The same ``idempotency_key`` always returns the same order, so a retried
        request cannot produce two approvals for one purchase.
        """

        amount = _validate_amount(amount_cents)
        code = normalize_currency(currency)
        clean_description = _clean_description(description)
        key = _validate_idempotency_key(idempotency_key)

        existing = self.ledger.find_proposal_by_key(key)
        if existing is not None:
            return existing, False

        spent = self.ledger.spend_cents(code, since=_month_start(self._now()))
        if amount > self.caps.per_purchase_cents:
            raise WalletError(
                "cap_exceeded",
                user_fixable=True,
                details={
                    "amount_cents": amount,
                    "per_purchase_cap_cents": self.caps.per_purchase_cents,
                },
            )
        if spent + amount > self.caps.monthly_cents:
            raise WalletError(
                "monthly_cap_exceeded",
                user_fixable=True,
                details={
                    "amount_cents": amount,
                    "spent_cents": spent,
                    "monthly_cap_cents": self.caps.monthly_cents,
                },
            )
        return self.ledger.create_proposal(
            amount_cents=amount,
            currency=code,
            description=clean_description,
            idempotency_key=key,
        )

    def approve(
        self,
        proposal_id: str,
        *,
        decision: str = "approve",
        payment_method_id: str | None = None,
    ) -> tuple[Proposal, bool]:
        """Answer an order exactly once. Returns ``(proposal, idempotent)``.

        ``approve`` charges the selected saved card through the gateway and
        records a receipt. ``deny`` charges nothing. Repeating the same answer
        is idempotent; a conflicting answer is a 409.
        """

        if decision not in {"approve", "deny"}:
            raise WalletError(
                "invalid_request",
                message="Decision must be 'approve' or 'deny'.",
                user_fixable=True,
            )
        if not isinstance(proposal_id, str) or not proposal_id:
            raise WalletError("unknown_proposal")

        current = self.ledger.get_proposal(proposal_id)
        if current is None:
            raise WalletError("unknown_proposal")

        if decision == "deny":
            proposal, outcome = self.ledger.decide(
                proposal_id,
                decision="deny",
                payment_method_id=None,
                monthly_cap_cents=self.caps.monthly_cents,
                monthly_since=_month_start(self._now()),
            )
            if outcome == "missing":  # pragma: no cover - checked above
                raise WalletError("unknown_proposal")
            if outcome == "conflict":
                if proposal is not None and proposal.status in {"denied", "blocked"}:
                    return proposal, True
                raise WalletError(
                    "already_decided",
                    message="That order was already approved.",
                )
            assert proposal is not None
            return proposal, False

        if current.status == "paid":
            return current, True
        if current.status == "denied":
            return current, True
        if current.status in {"blocked", "failed"}:
            raise WalletError(
                "already_decided",
                message="That order was already answered.",
            )

        if current.status == "approved":
            # A previous attempt answered the order but never produced a
            # receipt (for example, the process stopped). Resume the same
            # charge with the same per-order idempotency key.
            resume_card = self._select_card(
                current.payment_method_id or payment_method_id
            )
            return self._pay(current, resume_card), True

        card = self._select_card(payment_method_id)
        self._require_config()
        proposal, outcome = self.ledger.decide(
            proposal_id,
            decision="approve",
            payment_method_id=card.id,
            monthly_cap_cents=self.caps.monthly_cents,
            monthly_since=_month_start(self._now()),
        )
        if outcome == "missing":  # pragma: no cover - checked above
            raise WalletError("unknown_proposal")
        if outcome == "conflict":
            if proposal is not None and proposal.status == "paid":
                return proposal, True
            raise WalletError("already_decided")
        assert proposal is not None
        if outcome == "blocked":
            raise WalletError(
                "monthly_cap_exceeded",
                user_fixable=True,
                details={
                    "amount_cents": proposal.amount_cents,
                    "spent_cents": self.ledger.spend_cents(
                        proposal.currency, since=_month_start(self._now())
                    ),
                    "monthly_cap_cents": self.caps.monthly_cents,
                },
            )

        return self._pay(proposal, card), False

    def _pay(self, proposal: Proposal, card: Card) -> Proposal:
        config = self._require_config()
        try:
            intent = self._transport().create_payment_intent(
                secret_key=config.secret_key,
                amount_cents=proposal.amount_cents,
                currency=proposal.currency,
                payment_method_id=card.id,
                customer_id=self.customer_id,
                description=proposal.description,
                idempotency_key=self.gateway_idempotency_key(proposal.id),
            )
            intent_id = _validated_payment_intent(
                intent,
                amount_cents=proposal.amount_cents,
                currency=proposal.currency,
            )
        except StripeGatewayError as exc:
            reason = exc.code or "payment_failed"
            self.ledger.mark_failed(proposal.id, reason=reason)
            raise WalletError(
                "payment_failed",
                retryable=exc.retryable,
                message="The card was not charged. Try again.",
                details={"reason": redact_text(str(exc))[:200]},
            ) from None

        return self.ledger.mark_paid(
            proposal.id,
            payment_intent_id=intent_id,
            card_last4=card.last4,
        )

    @staticmethod
    def gateway_idempotency_key(proposal_id: str) -> str:
        """The one stable charge key for an order, reused on every attempt."""

        return f"kelp-order-{proposal_id}"

    def _select_card(self, payment_method_id: str | None) -> Card:
        if payment_method_id is not None:
            if not isinstance(payment_method_id, str) or not _PAYMENT_METHOD_RE.match(
                payment_method_id
            ):
                raise WalletError("invalid_payment_method", user_fixable=True)
            card = self.ledger.get_card(payment_method_id)
            if card is None:
                raise WalletError("unknown_card")
            return card
        card = self.ledger.default_card()
        if card is None:
            raise WalletError("no_saved_card", user_fixable=True)
        return card


def _validate_amount(amount_cents: int) -> int:
    if isinstance(amount_cents, bool) or not isinstance(amount_cents, int):
        raise WalletError(
            "invalid_amount",
            message="The amount must be a whole number of cents.",
            user_fixable=True,
        )
    if amount_cents <= 0:
        raise WalletError("invalid_amount", user_fixable=True)
    if amount_cents > MAX_AMOUNT_CENTS:
        raise WalletError(
            "invalid_amount",
            message="That amount is too large.",
            user_fixable=True,
        )
    return amount_cents


def _validate_idempotency_key(value: str | None) -> str:
    if value is None or value == "":
        return uuid.uuid4().hex
    if not isinstance(value, str):
        raise WalletError("invalid_idempotency_key", user_fixable=True)
    key = value.strip()
    if not _IDEMPOTENCY_RE.match(key):
        raise WalletError(
            "invalid_idempotency_key",
            message="The order key must be 8-80 letters, digits, or . _ : -",
            user_fixable=True,
        )
    return key


def _clean_description(value: str) -> str:
    if not isinstance(value, str):
        raise WalletError(
            "invalid_request",
            message="Description must be text.",
            user_fixable=True,
        )
    cleaned = redact_text(value.strip())
    if len(cleaned) > MAX_DESCRIPTION_CHARS:
        raise WalletError(
            "invalid_request",
            message="Description is too long.",
            user_fixable=True,
        )
    return cleaned


def _validated_payment_intent(
    payload: Mapping[str, Any],
    *,
    amount_cents: int,
    currency: str,
) -> str:
    """Accept only a complete, matching charge. Anything else is not a receipt."""

    if not isinstance(payload, Mapping):
        raise StripeGatewayError("Stripe returned an unexpected response.")
    intent_id = payload.get("id")
    if not isinstance(intent_id, str) or not _PAYMENT_INTENT_RE.match(intent_id):
        raise StripeGatewayError("Stripe returned an unexpected response.")
    status = payload.get("status")
    if status != "succeeded":
        raise StripeGatewayError(
            f"Payment is not complete ({redact_text(str(status))}).",
            retryable=status in {"processing", "requires_action"},
        )
    if "amount" in payload:
        try:
            returned_amount = int(payload["amount"])
        except (TypeError, ValueError):
            raise StripeGatewayError("Stripe returned an unexpected amount.") from None
        if returned_amount != amount_cents:
            raise StripeGatewayError(
                "The processor returned a different amount than approved."
            )
    if "currency" in payload and str(payload["currency"]).lower() != currency:
        raise StripeGatewayError(
            "The processor returned a different currency than approved."
        )
    return intent_id

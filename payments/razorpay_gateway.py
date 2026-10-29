"""KEL-10 Razorpay test-mode gateway: a second processor behind the wallet.

Razorpay is modelled as an *adapter* onto the same gateway protocol that
``payments.wallet.Wallet`` already drives for Stripe, so wallet behaviour is not
duplicated and not changed:

- ``Wallet`` still owns orders, approvals, spend caps, the reservation sweep,
  and the per-order idempotency key (``kelp-order-<id>``).
- :class:`RazorpayGateway` translates the four protocol calls into Razorpay
  REST calls: a *customer* plays the part of a SetupIntent, a *card token*
  plays the part of a saved payment method, and an *order* + *payment* pair
  plays the part of a PaymentIntent.
- :class:`RazorpayWallet` is ``Wallet`` with Razorpay configuration and the
  Razorpay transport wired in; caps, retries, idempotency, and the single-use
  approval rule are inherited unchanged.

Security boundaries (same rules as the Stripe wallet):

- ``RAZORPAY_KEY_ID`` and ``RAZORPAY_KEY_SECRET`` are read from the server
  environment only. They are never accepted from a browser, never written to
  the ledger, and never appear in ``repr``, ``str``, status payloads, error
  text, or the SQLite file. A half-configured pair (one key without the other)
  is a configuration error, not a silent fallback.
- Card details are never stored. A token payload is reduced to an allowlisted
  view (network, last4, expiry); the ``number`` field Razorpay returns is
  dropped before it can reach the ledger. A payload with a real PAN or a
  card-secret key is rejected by ``Wallet``'s existing checks.
- Amounts are validated against the same bounds, currencies against the set
  Razorpay actually settles with two-decimal minor units, and idempotency keys
  against the same 8-80 character alphabet rule. The charge key is stable per
  order and is sent as ``X-Razorpay-Idempotency-Key``, so a retried request
  cannot create a second payment.
- A retryable transport failure (timeout, 5xx, 429) raises a retryable error,
  so the wallet keeps the order ``approved`` for a resume with the same key. A
  definitive rejection marks the order ``failed``.
- HTTP happens only inside :class:`HttpRazorpayTransport`, which is constructed
  lazily on the first real call. Tests inject ``FakeTransport``; the network
  stack is never touched.

Configuration (server-only environment):

===============================  ==========================================
``RAZORPAY_KEY_ID``              Test-mode key id (``rzp_test_...``). Empty =
                                 wallet visible but unable to take payments.
``RAZORPAY_KEY_SECRET``          Matching key secret.
``RAZORPAY_CUSTOMER_ID``         Optional Razorpay customer (``cust_...``).
``WALLET_PER_PURCHASE_CAP_CENTS`` Shared with the Stripe wallet.
``WALLET_MONTHLY_CAP_CENTS``     Shared with the Stripe wallet.
===============================  ==========================================

Wallet-facing ids are derived from provider ids so a ledger row can be read
back after a restart without a lookup table:

- token ``token_AbC123``  -> payment method ``pm_rzpAbC123``
- payment ``pay_AbC123``  -> payment intent ``pi_rzpAbC123``

The encodings are validated on the way back in; a wallet-facing id that does
not decode to a Razorpay-shaped id is rejected, so nothing from a client can be
smuggled into a URL path.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol

from .stripe_wallet import WalletNotConfigured
from .wallet import (
    MAX_AMOUNT_CENTS,
    SpendCaps,
    StripeGatewayError,
    Wallet,
    WalletError,
    WalletLedger,
    normalize_currency,
    redact_payload,
    redact_text,
)

__all__ = [
    "CUSTOMER_ID_ENV",
    "KEY_ID_ENV",
    "KEY_SECRET_ENV",
    "MAX_AMOUNT_CENTS",
    "SUPPORTED_CURRENCIES",
    "HttpRazorpayTransport",
    "RazorpayConfig",
    "RazorpayGateway",
    "RazorpayGatewayError",
    "RazorpayNotConfigured",
    "RazorpayTransport",
    "RazorpayWallet",
    "redact_razorpay_payload",
    "redact_razorpay_text",
    "token_id_from_wallet_id",
    "wallet_payment_intent_id",
    "wallet_payment_method_id",
]

KEY_ID_ENV = "RAZORPAY_KEY_ID"
KEY_SECRET_ENV = "RAZORPAY_KEY_SECRET"
CUSTOMER_ID_ENV = "RAZORPAY_CUSTOMER_ID"

# Razorpay settles these currencies in two-decimal minor units, which is what
# the wallet means by "cents". Zero-decimal currencies (JPY, KRW, ...) are
# deliberately excluded: sending "100" would charge 100 yen, not 1.00.
SUPPORTED_CURRENCIES = frozenset(
    {"inr", "usd", "eur", "gbp", "aed", "sgd", "myr", "aud", "cad", "hkd", "nzd"}
)

REDACTED_VALUE = "[redacted]"

_TOKEN_RE = re.compile(r"^token_[A-Za-z0-9]{4,}$")
_ORDER_RE = re.compile(r"^order_[A-Za-z0-9]{4,}$")
_PAYMENT_RE = re.compile(r"^pay_[A-Za-z0-9]{4,}$")
_CUSTOMER_RE = re.compile(r"^cust_[A-Za-z0-9]{4,}$")

# Wallet-side shapes. ``payment_method`` mirrors ``payments.wallet``'s
# ``_PAYMENT_METHOD_RE`` (no underscores after the prefix), which is why the
# token mapping joins the suffix without a separator.
_WALLET_TOKEN_RE = re.compile(r"^pm_rzp([A-Za-z0-9]{4,})$")
_WALLET_PAYMENT_RE = re.compile(r"^pi_rzp([A-Za-z0-9]{4,})$")
_KEY_ID_RE = re.compile(r"^rzp_(?:test|live)_[A-Za-z0-9]{4,}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,80}$")
_MAX_IDEMPOTENCY_CHARS = 80
_MAX_NOTES_CHARS = 200

# Same masking rule as ``payments.wallet``: over-matching when hiding text is
# cheaper than leaking a credential.
_RAZORPAY_KEY_RE = re.compile(r"\brzp_(?:live|test)_[A-Za-z0-9]{4,}")
_SECRET_KEY_RE = re.compile(
    r"(?i)(number|pan|cvc|cvv|security_code|secret|password|token|authorization|credential)"
)

_BRANDS = {
    "visa": "visa",
    "mastercard": "mastercard",
    "master card": "mastercard",
    "maestro": "maestro",
    "amex": "amex",
    "american express": "amex",
    "rupay": "rupay",
    "diners": "diners",
    "diners club": "diners",
    "discover": "discover",
    "jcb": "jcb",
    "unionpay": "unionpay",
    "union pay": "unionpay",
}


# -- redaction -------------------------------------------------------------


def redact_razorpay_text(value: Any) -> str:
    """Mask card numbers, Stripe keys, and Razorpay key ids in free text."""

    text = redact_text(value if isinstance(value, str) else str(value))
    return _RAZORPAY_KEY_RE.sub(REDACTED_VALUE, text)


def redact_razorpay_payload(value: Any) -> Any:
    """Recursively redact a Razorpay-shaped JSON value.

    Reuses the wallet's card/secret-key masking and then removes Razorpay key
    ids from every string, so a debug dump of a processor response cannot carry
    a credential or a card number.
    """

    return _scrub_razorpay(redact_payload(value))


def _scrub_razorpay(value: Any) -> Any:
    if isinstance(value, str):
        return _RAZORPAY_KEY_RE.sub(REDACTED_VALUE, value)
    if isinstance(value, Mapping):
        return {str(key): _scrub_razorpay(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_razorpay(item) for item in value]
    return value


# -- configuration ---------------------------------------------------------


class RazorpayNotConfigured(WalletNotConfigured):
    """Raised when Razorpay work is attempted without both server keys.

    Subclasses :class:`WalletNotConfigured` so the existing wallet HTTP surface
    (``chat.wallet``) turns it into the same 503 ``not_configured`` body.
    """

    def __init__(self) -> None:
        RuntimeError.__init__(
            self,
            "Razorpay is not configured. Set RAZORPAY_KEY_ID and "
            "RAZORPAY_KEY_SECRET on the server (environment or secret "
            "manager); never put them in client code.",
        )


@dataclass(frozen=True)
class RazorpayConfig:
    """Server-side Razorpay credentials. Neither value is ever printed."""

    key_id: str = field(repr=False)
    secret_key: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.key_id, str) or not self.key_id.strip():
            raise RazorpayNotConfigured()
        if not isinstance(self.secret_key, str) or not self.secret_key.strip():
            raise RazorpayNotConfigured()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RazorpayConfig":
        source = os.environ if env is None else env
        key_id = (source.get(KEY_ID_ENV) or "").strip()
        secret = (source.get(KEY_SECRET_ENV) or "").strip()
        if not key_id or not secret:
            raise RazorpayNotConfigured()
        return cls(key_id=key_id, secret_key=secret)

    @property
    def mode(self) -> str:
        if self.key_id.startswith("rzp_test_"):
            return "test"
        if self.key_id.startswith("rzp_live_"):
            return "live"
        return "unknown"

    def __repr__(self) -> str:
        return f"RazorpayConfig(mode={self.mode!r}, key_id=<redacted>)"

    __str__ = __repr__


# -- errors ----------------------------------------------------------------


class RazorpayGatewayError(StripeGatewayError):
    """A Razorpay transport failure with a credential-free message.

    Subclasses :class:`StripeGatewayError` so ``Wallet._pay`` applies its
    existing retry policy unchanged: ``retryable`` keeps the order ``approved``
    for a same-key resume, everything else retires it as ``failed``.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(
            redact_razorpay_text(message), code=code, retryable=retryable
        )


# -- id mapping ------------------------------------------------------------


def wallet_payment_method_id(token_id: str) -> str:
    """``token_AbC123`` -> ``pm_rzpAbC123``, the id the wallet stores."""

    if not isinstance(token_id, str) or _TOKEN_RE.match(token_id) is None:
        raise WalletError("invalid_payment_method", user_fixable=True)
    return "pm_rzp" + token_id[len("token_") :]


def token_id_from_wallet_id(payment_method_id: str) -> str:
    """``pm_rzpAbC123`` -> ``token_AbC123``; rejects anything else."""

    if not isinstance(payment_method_id, str):
        raise WalletError("invalid_payment_method", user_fixable=True)
    matched = _WALLET_TOKEN_RE.match(payment_method_id)
    if matched is None:
        raise WalletError("invalid_payment_method", user_fixable=True)
    return "token_" + matched.group(1)


def wallet_payment_intent_id(payment_id: str) -> str:
    """``pay_AbC123`` -> ``pi_rzpAbC123``, the receipt id the wallet stores."""

    payment = _require_provider_id(payment_id, _PAYMENT_RE)
    return "pi_rzp" + payment[len("pay_") :]


def _require_provider_id(value: Any, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.match(value) is None:
        raise RazorpayGatewayError("Razorpay returned an unexpected payment.")
    return value


# -- transport -------------------------------------------------------------


class RazorpayTransport(Protocol):
    """The six Razorpay REST calls. Tests supply a fake; nothing else may."""

    def create_customer(
        self,
        *,
        key_id: str,
        key_secret: str,
        notes: Mapping[str, str],
    ) -> Mapping[str, Any]: ...

    def fetch_token(
        self,
        *,
        key_id: str,
        key_secret: str,
        token_id: str,
    ) -> Mapping[str, Any]: ...

    def list_tokens(
        self,
        *,
        key_id: str,
        key_secret: str,
        customer_id: str,
    ) -> Iterable[Mapping[str, Any]]: ...

    def create_order(
        self,
        *,
        key_id: str,
        key_secret: str,
        amount_cents: int,
        currency: str,
        notes: Mapping[str, str],
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

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
    ) -> Mapping[str, Any]: ...

    def capture_payment(
        self,
        *,
        key_id: str,
        key_secret: str,
        payment_id: str,
        amount_cents: int,
        currency: str,
    ) -> Mapping[str, Any]: ...


class HttpRazorpayTransport:
    """Real Razorpay transport. Constructed lazily; never used by tests.

    Authentication is HTTP Basic with ``key_id:key_secret``. The pair is only
    ever placed in the ``Authorization`` header -- never in a URL, query
    string, body, log line, or exception message.
    """

    base_url = "https://api.razorpay.com/v1"
    _RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
    _ID_PATTERNS = {
        "token": _TOKEN_RE,
        "order": _ORDER_RE,
        "payment": _PAYMENT_RE,
        "customer": _CUSTOMER_RE,
    }

    def __init__(self, *, timeout: float = 20.0, client: Any = None) -> None:
        self.timeout = timeout
        self._client: Any = client
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            client, self._client = self._client, None
        if client is not None and hasattr(client, "close"):
            client.close()

    def _http(self) -> Any:
        # Imported lazily so importing this module never builds a client.
        import httpx

        with self._lock:
            if self._client is None:
                self._client = httpx.Client(timeout=self.timeout)
            return self._client

    @staticmethod
    def _path_id(value: str, kind: str) -> str:
        if not isinstance(value, str) or HttpRazorpayTransport._ID_PATTERNS[
            kind
        ].match(value) is None:
            raise RazorpayGatewayError(
                f"Razorpay {kind} id is not valid.", retryable=False
            )
        return value

    def _request(
        self,
        method: str,
        path: str,
        *,
        key_id: str,
        key_secret: str,
        body: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> Mapping[str, Any]:
        import httpx

        if not key_id or not key_secret:
            # Never send half a credential pair.
            raise RazorpayGatewayError(
                "Razorpay credentials are missing.", code="invalid_config"
            )
        token = base64.b64encode(f"{key_id}:{key_secret}".encode("utf-8")).decode(
            "ascii"
        )
        headers = {
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if idempotency_key:
            headers["X-Razorpay-Idempotency-Key"] = idempotency_key
        try:
            response = self._http().request(
                method,
                f"{self.base_url}{path}",
                json=dict(body or {}),
                params=dict(params or {}),
                headers=headers,
            )
        except httpx.HTTPError:
            raise RazorpayGatewayError(
                "Can't reach the payment processor right now.", retryable=True
            ) from None

        if response.status_code >= 400:
            code: str | None = None
            try:
                payload = response.json()
                code = str((payload.get("error") or {}).get("code") or "") or None
            except (ValueError, AttributeError):  # pragma: no cover - odd body
                code = None
            raise RazorpayGatewayError(
                f"Razorpay rejected the request ({response.status_code}).",
                code=redact_razorpay_text(code) if code else None,
                retryable=response.status_code in self._RETRYABLE_STATUS,
            )

        try:
            payload = response.json()
        except ValueError:  # pragma: no cover - Razorpay always returns JSON
            raise RazorpayGatewayError(
                "Razorpay returned an unreadable response."
            ) from None
        if not isinstance(payload, Mapping):
            raise RazorpayGatewayError("Razorpay returned an unexpected response.")
        return payload

    def create_customer(
        self,
        *,
        key_id: str,
        key_secret: str,
        notes: Mapping[str, str],
    ) -> Mapping[str, Any]:
        return self._request(
            "POST",
            "/customers",
            key_id=key_id,
            key_secret=key_secret,
            body={"notes": {str(k): str(v)[:_MAX_NOTES_CHARS] for k, v in notes.items()}},
        )

    def fetch_token(
        self,
        *,
        key_id: str,
        key_secret: str,
        token_id: str,
    ) -> Mapping[str, Any]:
        return self._request(
            "GET",
            f"/tokens/{self._path_id(token_id, 'token')}",
            key_id=key_id,
            key_secret=key_secret,
        )

    def list_tokens(
        self,
        *,
        key_id: str,
        key_secret: str,
        customer_id: str,
    ) -> Iterable[Mapping[str, Any]]:
        payload = self._request(
            "GET",
            f"/customers/{self._path_id(customer_id, 'customer')}/tokens",
            key_id=key_id,
            key_secret=key_secret,
            params={"count": "20"},
        )
        items = payload.get("items", payload.get("data"))
        return [item for item in items or [] if isinstance(item, Mapping)]

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
        # ``payment_capture=1`` asks Razorpay to capture on authorisation; the
        # gateway still captures explicitly if the payment comes back
        # authorised, so a partial rollout of auto-capture cannot change the
        # wallet's meaning of "paid".
        return self._request(
            "POST",
            "/orders",
            key_id=key_id,
            key_secret=key_secret,
            body={
                "amount": amount_cents,
                "currency": currency,
                "payment_capture": 1,
                "notes": {
                    str(k): str(v)[:_MAX_NOTES_CHARS] for k, v in notes.items()
                },
            },
            idempotency_key=idempotency_key,
        )

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
        body: dict[str, Any] = {
            "amount": amount_cents,
            "currency": currency,
            "order_id": self._path_id(order_id, "order"),
            "token": self._path_id(token_id, "token"),
            "method": "card",
            "notes": {"description": description[:_MAX_NOTES_CHARS]},
        }
        if customer_id:
            body["customer_id"] = self._path_id(customer_id, "customer")
        return self._request(
            "POST",
            "/payments",
            key_id=key_id,
            key_secret=key_secret,
            body=body,
            idempotency_key=idempotency_key,
        )

    def capture_payment(
        self,
        *,
        key_id: str,
        key_secret: str,
        payment_id: str,
        amount_cents: int,
        currency: str,
    ) -> Mapping[str, Any]:
        return self._request(
            "POST",
            f"/payments/{self._path_id(payment_id, 'payment')}/capture",
            key_id=key_id,
            key_secret=key_secret,
            body={"amount": amount_cents, "currency": currency},
        )


# -- gateway ---------------------------------------------------------------


class RazorpayGateway:
    """Razorpay on the wallet's gateway protocol (structurally a StripeGateway).

    Inject into ``Wallet(gateway=...)`` or use :class:`RazorpayWallet`. The
    protocol's Stripe vocabulary is translated here, not in the wallet:

    =========================  =============================================
    ``create_setup_intent``    Razorpay customer (card-on-file registration)
    ``attach_payment_method``  fetch and allowlist a card ``token_...``
    ``list_payment_methods``   list the customer's tokens
    ``create_payment_intent``  order + token payment (capture if authorised)
    =========================  =============================================
    """

    def __init__(
        self,
        *,
        key_id: str,
        transport: RazorpayTransport | None = None,
        max_charge_cents: int = MAX_AMOUNT_CENTS,
    ) -> None:
        if not isinstance(key_id, str) or not key_id.strip():
            raise WalletError(
                "invalid_config",
                message="RAZORPAY_KEY_ID is not set.",
            )
        if (
            isinstance(max_charge_cents, bool)
            or not isinstance(max_charge_cents, int)
            or not 0 < max_charge_cents <= MAX_AMOUNT_CENTS
        ):
            raise WalletError(
                "invalid_config",
                message="max_charge_cents must be a positive number of cents.",
            )
        self.key_id = key_id.strip()
        self.max_charge_cents = max_charge_cents
        self._transport = transport

    def __repr__(self) -> str:
        return (
            f"RazorpayGateway(key_id=<redacted>, "
            f"transport={type(self._transport).__name__ if self._transport else 'lazy'!r})"
        )

    __str__ = __repr__

    def close(self) -> None:
        if self._transport is not None and hasattr(self._transport, "close"):
            self._transport.close()

    def _client(self) -> RazorpayTransport:
        if self._transport is None:
            self._transport = HttpRazorpayTransport()
        return self._transport

    # -- protocol: customers and cards -------------------------------------

    def create_setup_intent(
        self,
        *,
        secret_key: str,
        customer_id: str,
        metadata: Mapping[str, str],
    ) -> Mapping[str, Any]:
        if customer_id:
            if _CUSTOMER_RE.match(customer_id) is None:
                raise WalletError(
                    "invalid_config",
                    message="RAZORPAY_CUSTOMER_ID must look like cust_….",
                )
            customer = customer_id
        else:
            payload = self._client().create_customer(
                key_id=self.key_id,
                key_secret=secret_key,
                notes=dict(metadata),
            )
            raw = payload.get("id")
            if not isinstance(raw, str) or _CUSTOMER_RE.match(raw) is None:
                raise RazorpayGatewayError(
                    "Razorpay returned an unexpected customer."
                )
            customer = raw
        # Razorpay has no client_secret: the browser hands the customer handle
        # to Checkout. It is an identifier, not a credential, and carries no
        # key material.
        return {
            "id": "seti_rzp_" + customer[len("cust_") :],
            "client_secret": customer,
            "customer_id": customer,
        }

    def attach_payment_method(
        self,
        *,
        secret_key: str,
        payment_method_id: str,
        customer_id: str,
    ) -> Mapping[str, Any]:
        token_id = token_id_from_wallet_id(payment_method_id)
        if not customer_id:
            raise WalletError(
                "invalid_config",
                message="Set RAZORPAY_CUSTOMER_ID before saving a card.",
            )
        if _CUSTOMER_RE.match(customer_id) is None:
            raise WalletError(
                "invalid_config",
                message="RAZORPAY_CUSTOMER_ID must look like cust_….",
            )
        payload = self._client().fetch_token(
            key_id=self.key_id, key_secret=secret_key, token_id=token_id
        )
        return _payment_method_view(
            payload, token_id=token_id, customer_id=customer_id
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
                message="Set RAZORPAY_CUSTOMER_ID before listing cards.",
            )
        if _CUSTOMER_RE.match(customer_id) is None:
            raise WalletError(
                "invalid_config",
                message="RAZORPAY_CUSTOMER_ID must look like cust_….",
            )
        views: list[Mapping[str, Any]] = []
        for payload in self._client().list_tokens(
            key_id=self.key_id, key_secret=secret_key, customer_id=customer_id
        ):
            token_id = payload.get("id")
            if not isinstance(token_id, str) or _TOKEN_RE.match(token_id) is None:
                continue
            if payload.get("used") is True:
                continue
            if payload.get("method") not in (None, "card"):
                continue
            try:
                views.append(
                    _payment_method_view(
                        payload, token_id=token_id, customer_id=customer_id
                    )
                )
            except WalletError:
                # One malformed token must not hide the owner's other cards.
                continue
        return views

    # -- protocol: charges --------------------------------------------------

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
        amount = _validated_amount(amount_cents, self.max_charge_cents)
        code = _validated_currency(currency)
        key = _validated_idempotency_key(idempotency_key)
        token_id = token_id_from_wallet_id(payment_method_id)
        if customer_id and _CUSTOMER_RE.match(customer_id) is None:
            raise WalletError(
                "invalid_config",
                message="RAZORPAY_CUSTOMER_ID must look like cust_….",
            )
        safe_description = redact_razorpay_text(description or "")[:_MAX_NOTES_CHARS]

        transport = self._client()
        order = transport.create_order(
            key_id=self.key_id,
            key_secret=secret_key,
            amount_cents=amount,
            currency=code,
            notes={"source": "kelp-wallet", "description": safe_description},
            idempotency_key=_derived_key(key, "order"),
        )
        order_id = order.get("id")
        if not isinstance(order_id, str) or _ORDER_RE.match(order_id) is None:
            raise RazorpayGatewayError("Razorpay returned an unexpected order.")
        _check_echo(order, amount_cents=amount, currency=code)

        payment = transport.create_payment(
            key_id=self.key_id,
            key_secret=secret_key,
            order_id=order_id,
            token_id=token_id,
            amount_cents=amount,
            currency=code,
            description=safe_description,
            customer_id=customer_id if customer_id else "",
            idempotency_key=key,
        )
        payment_id, status = self._settle(
            payment, secret_key=secret_key, amount=amount, currency=code
        )
        _check_echo(payment, amount_cents=amount, currency=code)
        return {
            "id": wallet_payment_intent_id(payment_id),
            "status": status,
            "amount": amount,
            "currency": code,
        }

    def _settle(
        self,
        payment: Mapping[str, Any],
        *,
        secret_key: str,
        amount: int,
        currency: str,
    ) -> tuple[str, str]:
        """Return ``(payment_id, wallet_status)``, capturing when authorised."""

        payment_id = payment.get("id")
        if not isinstance(payment_id, str) or _PAYMENT_RE.match(payment_id) is None:
            raise RazorpayGatewayError("Razorpay returned an unexpected payment.")

        status = _map_status(payment.get("status"))
        if status == "requires_capture":
            captured = self._client().capture_payment(
                key_id=self.key_id,
                key_secret=secret_key,
                payment_id=payment_id,
                amount_cents=amount,
                currency=currency,
            )
            status = _map_status(captured.get("status"))
            if status == "succeeded":
                _check_echo(captured, amount_cents=amount, currency=currency)
        if status == "failed":
            raise RazorpayGatewayError(
                "Razorpay rejected the payment.",
                code=_safe_code(payment.get("error_code")),
                retryable=False,
            )
        if status == "refunded":
            # A refunded payment is definitive: nothing was kept, so the order
            # must release its reservation instead of waiting for a retry.
            raise RazorpayGatewayError(
                "Payment is not complete (refunded).", retryable=False
            )
        if status not in {"succeeded", "processing"}:
            # Authorised-but-not-captured or an unrecognised status: not a
            # receipt, but it may still settle. Reuse the same key.
            raise RazorpayGatewayError(
                f"Payment is not complete ({status}).", retryable=True
            )
        return payment_id, status


# -- wallet ----------------------------------------------------------------


class RazorpayWallet(Wallet):
    """``Wallet`` with Razorpay configuration. Caps and rules are inherited."""

    provider = "razorpay"

    def __init__(
        self,
        *,
        config: RazorpayConfig | None,
        caps: SpendCaps | None = None,
        ledger: WalletLedger | None = None,
        gateway: RazorpayGateway | None = None,
        customer_id: str = "",
        now_fn: Any = None,
        approval_ttl: Any = None,
        transport: RazorpayTransport | None = None,
    ) -> None:
        raw_customer = (customer_id or "").strip()
        if raw_customer and _CUSTOMER_RE.match(raw_customer) is None:
            raise WalletError(
                "invalid_config",
                message="RAZORPAY_CUSTOMER_ID must look like cust_….",
            )
        super().__init__(
            config=config,  # type: ignore[arg-type] - same shape, different processor
            caps=caps,
            ledger=ledger,
            gateway=gateway,  # type: ignore[arg-type] - structurally compatible
            customer_id="",
            now_fn=now_fn,
            approval_ttl=approval_ttl,
        )
        # ``Wallet`` validates ``cus_`` ids; Razorpay uses ``cust_``, so the
        # value is set after the base constructor has validated "".
        self.customer_id = raw_customer
        self._razorpay_transport = transport

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        ledger: WalletLedger | None = None,
        transport: RazorpayTransport | None = None,
        now_fn: Any = None,
    ) -> "RazorpayWallet":
        source = os.environ if env is None else env
        key_id = (source.get(KEY_ID_ENV) or "").strip()
        secret = (source.get(KEY_SECRET_ENV) or "").strip()
        config: RazorpayConfig | None
        if key_id and secret:
            config = RazorpayConfig(key_id=key_id, secret_key=secret)
        elif key_id or secret:
            # Half a credential pair is a misconfiguration, not "unconfigured".
            raise WalletError(
                "invalid_config",
                message="Set both RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET, or neither.",
            )
        else:
            config = None
        gateway = (
            None
            if config is None
            else RazorpayGateway(key_id=config.key_id, transport=transport)
        )
        return cls(
            config=config,
            caps=SpendCaps.from_env(source),
            ledger=ledger,
            gateway=gateway,
            customer_id=(source.get(CUSTOMER_ID_ENV) or "").strip(),
            now_fn=now_fn,
            transport=transport,
        )

    # -- configuration -----------------------------------------------------

    def _require_config(self) -> RazorpayConfig:  # type: ignore[override]
        if self._config is None:
            raise RazorpayNotConfigured()
        if self._config.mode not in {"test", "live"}:
            raise WalletError(
                "invalid_config",
                message=(
                    "RAZORPAY_KEY_ID must be a Razorpay key id "
                    "(rzp_test_… or rzp_live_…)."
                ),
            )
        return self._config  # type: ignore[return-value]

    def _transport(self) -> RazorpayGateway:  # type: ignore[override]
        if self._gateway is None:
            config = self._require_config()
            self._gateway = RazorpayGateway(
                key_id=config.key_id, transport=self._razorpay_transport
            )
        return self._gateway  # type: ignore[return-value]

    def __repr__(self) -> str:
        mode = self._config.mode if self._config is not None else "unconfigured"
        return f"RazorpayWallet(mode={mode!r}, configured={self.configured})"

    __str__ = __repr__

    # -- orders ------------------------------------------------------------

    def propose(
        self,
        amount_cents: int,
        *,
        currency: str | None = None,
        description: str = "",
        idempotency_key: str | None = None,
    ) -> tuple[Any, bool]:
        """Reject a currency Razorpay cannot settle before an order exists."""

        code = normalize_currency(currency)
        if code not in SUPPORTED_CURRENCIES:
            raise WalletError(
                "invalid_currency",
                message="Razorpay doesn't support that currency.",
                user_fixable=True,
            )
        return super().propose(
            amount_cents,
            currency=code,
            description=description,
            idempotency_key=idempotency_key,
        )


# -- validation and mapping helpers ---------------------------------------


def _validated_amount(amount_cents: Any, max_charge_cents: int) -> int:
    if isinstance(amount_cents, bool) or not isinstance(amount_cents, int):
        raise WalletError(
            "invalid_amount",
            message="The amount must be a whole number of cents.",
            user_fixable=True,
        )
    if amount_cents <= 0:
        raise WalletError("invalid_amount", user_fixable=True)
    if amount_cents > max_charge_cents:
        raise WalletError(
            "invalid_amount",
            message="That amount is too large.",
            user_fixable=True,
        )
    return amount_cents


def _validated_currency(currency: Any) -> str:
    code = normalize_currency(currency)
    if code not in SUPPORTED_CURRENCIES:
        raise WalletError(
            "invalid_currency",
            message="Razorpay doesn't support that currency.",
            user_fixable=True,
        )
    return code


def _validated_idempotency_key(value: Any) -> str:
    if not isinstance(value, str) or _IDEMPOTENCY_RE.match(value) is None:
        raise WalletError(
            "invalid_idempotency_key",
            message="The order key must be 8-80 letters, digits, or . _ : -",
            user_fixable=True,
        )
    return value


def _derived_key(idempotency_key: str, step: str) -> str:
    """A distinct idempotency key per REST call, bounded to Razorpay's 80.

    Truncation alone could make a long key collide with its own derived key
    (or with another key that shares a prefix), so an over-long key is replaced
    by a digest instead of being cut.
    """

    candidate = f"{idempotency_key}-{step}"
    if len(candidate) <= _MAX_IDEMPOTENCY_CHARS:
        return candidate
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:48]
    return f"{step}-{digest}"


def _safe_code(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return redact_razorpay_text(value.strip())[:64]


_STATUS_MAP = {
    "captured": "succeeded",
    "authorized": "requires_capture",
    "created": "processing",
    "pending": "processing",
    "processing": "processing",
    "failed": "failed",
    "refunded": "refunded",
}


def _map_status(raw: Any) -> str:
    return _STATUS_MAP.get(str(raw or "").lower(), "unknown")


def _check_echo(
    payload: Mapping[str, Any], *, amount_cents: int, currency: str
) -> None:
    """Refuse a response that charges a different amount or currency."""

    if not isinstance(payload, Mapping):
        raise RazorpayGatewayError("Razorpay returned an unexpected response.")
    if "amount" in payload:
        try:
            returned_amount = int(payload["amount"])
        except (TypeError, ValueError):
            raise RazorpayGatewayError(
                "Razorpay returned an unexpected amount."
            ) from None
        if returned_amount != amount_cents:
            raise RazorpayGatewayError(
                "The processor returned a different amount than approved."
            )
    if "currency" in payload and str(payload["currency"]).lower() != currency:
        raise RazorpayGatewayError(
            "The processor returned a different currency than approved."
        )


def _payment_method_view(
    payload: Mapping[str, Any],
    *,
    token_id: str,
    customer_id: str,
) -> Mapping[str, Any]:
    """Reduce a Razorpay token to the allowlisted card fields the wallet keeps.

    The token's ``card.number`` is never copied: only network, last4, and
    expiry leave this function. Anything else is dropped here, so a raw PAN in
    a processor response can never reach the ledger or a response body.
    """

    if not isinstance(payload, Mapping):
        raise WalletError("invalid_payment_method", user_fixable=True)
    stored_id = payload.get("id", token_id)
    if not isinstance(stored_id, str) or _TOKEN_RE.match(stored_id) is None:
        raise WalletError("invalid_payment_method", user_fixable=True)
    if payload.get("used") is True:
        raise WalletError(
            "invalid_payment_method",
            message="That saved card was already used. Add the card again.",
            user_fixable=True,
        )
    if payload.get("method") not in (None, "card"):
        raise WalletError(
            "invalid_payment_method",
            message="That payment method isn't a card.",
            user_fixable=True,
        )
    owner = payload.get("customer_id")
    if isinstance(owner, str) and owner and owner != customer_id:
        # A token from someone else's customer must never be attachable.
        raise WalletError(
            "invalid_payment_method",
            message="That card belongs to a different customer.",
            user_fixable=True,
        )

    card = payload.get("card")
    if not isinstance(card, Mapping):
        raise WalletError(
            "invalid_payment_method",
            message="That payment method isn't a card.",
            user_fixable=True,
        )

    brand = _brand(card.get("network"))
    last4 = _last4(card)
    exp_month = _positive_int(card.get("expiry_month", card.get("exp_month")))
    exp_year = _positive_int(card.get("expiry_year", card.get("exp_year")))
    if not 1 <= exp_month <= 12 or not 2000 <= exp_year <= 2100:
        raise WalletError("invalid_payment_method", user_fixable=True)

    return {
        "id": wallet_payment_method_id(stored_id),
        "object": "payment_method",
        "type": "card",
        "card": {
            "brand": brand,
            "last4": last4,
            "exp_month": exp_month,
            "exp_year": exp_year,
        },
    }


def _brand(raw: Any) -> str:
    key = re.sub(r"[^a-z0-9 ]", "", str(raw or "").strip().lower())
    key = re.sub(r"\s+", " ", key).strip()
    known = _BRANDS.get(key)
    if known:
        return known
    if re.fullmatch(r"[a-z][a-z0-9 _-]{0,31}", key):
        return key
    return "card"


def _last4(card: Mapping[str, Any]) -> str:
    explicit = card.get("last4")
    if isinstance(explicit, str) and re.fullmatch(r"\d{4}", explicit.strip()):
        return explicit.strip()
    masked = card.get("number")
    if isinstance(masked, str):
        matched = re.search(r"(\d{4})\s*$", masked)
        if matched is not None:
            return matched.group(1)
    raise WalletError("invalid_payment_method", user_fixable=True)


def _positive_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0

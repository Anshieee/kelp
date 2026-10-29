"""Razorpay hosted Checkout HTTP surface (KEL-10).

Framework-neutral like ``chat.wallet``: :meth:`CheckoutAPI.handle` takes a
plain :class:`Request` and returns a :class:`Response`. It is a separate class
so the saved-card surface does not grow a hosted-checkout dependency and so the
webhook route can be authenticated differently.

Endpoints (all under ``/api/wallet/checkout``):

=============================  ======  =========================================
``GET  /api/wallet/checkout``         checkout status (no secrets)
``POST /api/wallet/checkout``         create a provider order + Checkout payload
``POST /api/wallet/checkout/confirm`` verify the browser callback, settle
``POST /api/wallet/checkout/webhook`` verify a Razorpay webhook, settle
=============================  ======  =========================================

Browser calls use the same single-owner auth as the wallet: bearer token or
session cookie, plus a CSRF token (or same-origin check) on mutations. The
webhook is called by Razorpay, not a browser, so it is *not* session-authenticated;
its authentication is the HMAC signature over the raw request body, checked
inside :class:`payments.razorpay_checkout.RazorpayCheckout`.
"""

from __future__ import annotations

import hmac
import json
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from payments.razorpay_checkout import MAX_JSON_BYTES, RazorpayCheckout
from payments.stripe_wallet import WalletNotConfigured
from payments.wallet import MAX_AMOUNT_CENTS, WalletError, contains_raw_card_data

from .api import Request, Response

__all__ = ["CheckoutAPI"]

_JSON_LIMIT = 16 * 1024
_WEBHOOK_LIMIT = MAX_JSON_BYTES
_MUTATING = frozenset({"POST", "PATCH", "DELETE", "PUT"})
_CREATE_FIELDS = frozenset(
    {"amount_cents", "currency", "description", "idempotency_key"}
)
_CONFIRM_FIELDS = frozenset(
    {"razorpay_order_id", "razorpay_payment_id", "razorpay_signature"}
)


class CheckoutAPI:
    def __init__(
        self,
        checkout: RazorpayCheckout,
        *,
        auth_token: str | None = None,
        csrf_token: str | None = None,
    ) -> None:
        self.checkout = checkout
        self.auth_token = auth_token
        self.csrf_token = csrf_token

    # -- dispatch ----------------------------------------------------------

    def handle(self, request: Request) -> Response:
        try:
            path = unquote(urlsplit(request.path).path)
            segments = [segment for segment in path.split("/") if segment]
            if (
                len(segments) < 3
                or segments[0] != "api"
                or segments[1] != "wallet"
                or segments[2] != "checkout"
            ):
                raise WalletError("not_found")
            rest = segments[3:]
            if self._is_webhook(request, rest):
                return self._webhook(request)

            self._authorize(request)
            return self._route(request, request.method.upper(), rest)
        except WalletError as exc:
            return _json_response(exc.status, exc.to_body())
        except WalletNotConfigured:
            return _error(
                503,
                "not_configured",
                "Checkout isn't connected yet.",
                user_fixable=False,
            )
        except Exception:  # noqa: BLE001 - never leak a stack trace
            return _error(
                500,
                "internal_error",
                "Something went wrong.",
                retryable=True,
            )

    @staticmethod
    def _is_webhook(request: Request, rest: list[str]) -> bool:
        return (
            rest == ["webhook"]
            and request.method.upper() == "POST"
        )

    def _route(
        self, request: Request, method: str, rest: list[str]
    ) -> Response:
        if not rest:
            if method == "GET":
                return _json_response(
                    200, {"ok": True, "checkout": self.checkout.status()}
                )
            if method != "POST":
                raise WalletError("not_found")
            body = self._json_body(request, allowed=_CREATE_FIELDS)
            amount = body.get("amount_cents")
            if isinstance(amount, bool) or not isinstance(amount, int):
                raise WalletError(
                    "invalid_amount",
                    message="The amount must be a whole number of cents.",
                    user_fixable=True,
                )
            if amount > MAX_AMOUNT_CENTS:
                raise WalletError(
                    "invalid_amount",
                    message="That amount is too large.",
                    user_fixable=True,
                )
            currency = body.get("currency")
            if currency is not None and not isinstance(currency, str):
                raise WalletError("invalid_currency", user_fixable=True)
            description = body.get("description")
            if description is not None and not isinstance(description, str):
                raise WalletError(
                    "invalid_request",
                    message="Description must be text.",
                    user_fixable=True,
                )
            key = body.get("idempotency_key")
            if key is not None and not isinstance(key, str):
                raise WalletError("invalid_idempotency_key", user_fixable=True)
            result = self.checkout.create(
                amount,
                currency=currency,
                description=description or "",
                idempotency_key=key,
            )
            return _json_response(
                201 if not result["idempotent"] else 200,
                {"ok": True, **result},
            )

        if len(rest) != 1:
            raise WalletError("not_found")
        if rest[0] == "confirm":
            if method != "POST":
                raise WalletError("not_found")
            body = self._json_body(request, allowed=_CONFIRM_FIELDS)
            result = self.checkout.confirm(
                razorpay_order_id=_required_text(body, "razorpay_order_id"),
                razorpay_payment_id=_required_text(body, "razorpay_payment_id"),
                razorpay_signature=_required_text(body, "razorpay_signature"),
            )
            return _json_response(200, {"ok": True, **result})
        raise WalletError("not_found")

    def _webhook(self, request: Request) -> Response:
        if len(request.body) > _WEBHOOK_LIMIT:
            raise WalletError(
                "invalid_request",
                message="The webhook body is too large.",
                user_fixable=True,
            )
        # ``contains_raw_card_data`` is deliberately not applied to the webhook
        # body: it is a signed provider event, not user input, and the service
        # fetches the payment before trusting any amount in it.
        result = self.checkout.handle_webhook(
            request.body,
            signature=request.header("x-razorpay-signature"),
        )
        return _json_response(200, {"ok": True, **result})

    # -- auth --------------------------------------------------------------

    def _authorize(self, request: Request) -> None:
        if not self._authorized_for(request):
            raise WalletError("unauthorized", status=401)
        if request.method.upper() in _MUTATING and not self._csrf_ok(request):
            raise WalletError("csrf_failed", status=403)

    def _authorized_for(self, request: Request) -> bool:
        if not self.auth_token:
            return True
        header = request.header("authorization") or ""
        prefix = "Bearer "
        if header.startswith(prefix) and hmac.compare_digest(
            header[len(prefix) :], self.auth_token
        ):
            return True
        cookies = _parse_cookies(request.header("cookie"))
        session = cookies.get("session")
        return bool(session) and hmac.compare_digest(session, self.auth_token)

    def _csrf_ok(self, request: Request) -> bool:
        if self.csrf_token:
            supplied = request.header("x-csrf-token") or ""
            return hmac.compare_digest(supplied, self.csrf_token)
        origin = request.header("origin")
        if not origin:
            return True
        host = request.header("host") or ""
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return False
        return bool(host) and parsed.netloc == host

    # -- body parsing ------------------------------------------------------

    def _json_body(
        self, request: Request, *, allowed: frozenset[str]
    ) -> dict[str, Any]:
        if not request.body:
            body: dict[str, Any] = {}
        else:
            if len(request.body) > _JSON_LIMIT:
                raise WalletError(
                    "invalid_request",
                    message="The request body is too large.",
                    user_fixable=True,
                )
            try:
                body = json.loads(request.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise WalletError(
                    "invalid_request",
                    message="The request body is not valid JSON.",
                    user_fixable=True,
                ) from None
            if not isinstance(body, dict):
                raise WalletError(
                    "invalid_request",
                    message="The request body must be a JSON object.",
                    user_fixable=True,
                )
        if contains_raw_card_data(body):
            raise WalletError(
                "unstorable_card",
                message="Card details can't be sent here. Use Checkout.",
                user_fixable=True,
            )
        unknown = sorted(set(body) - set(allowed))
        if unknown:
            raise WalletError(
                "invalid_request",
                message="That request has fields this endpoint doesn't accept.",
                user_fixable=True,
                details={"fields": unknown},
            )
        return body


def _required_text(body: Mapping[str, Any], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise WalletError(
            "invalid_request",
            message=f"{name} is required.",
            user_fixable=True,
            field=name,
        )
    return value.strip()


def _parse_cookies(value: str | None) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in (value or "").split(";"):
        name, separator, raw = part.strip().partition("=")
        if separator and name:
            cookies[name] = raw
    return cookies


def _error(
    status: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    user_fixable: bool = False,
) -> Response:
    return _json_response(
        status,
        {
            "ok": False,
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
                "user_fixable": user_fixable,
            },
        },
    )


def _json_response(status: int, payload: Mapping[str, Any]) -> Response:
    body = json.dumps(payload, default=str, separators=(",", ":")).encode("utf-8")
    return Response(
        status=status,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(body)),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
        body=body,
    )

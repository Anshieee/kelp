"""Wallet HTTP surface for KEL-10.

Framework-neutral, like ``chat.api``: :meth:`WalletAPI.handle` takes a plain
``Request`` and returns a ``Response``. It is deliberately separate from
``ChatAPI`` so the wallet slice can be mounted on its own and so the chat
surface does not grow a payments dependency.

Endpoints (all under ``/api/wallet``):

===========================  ======  =========================================
``GET  /api/wallet``         read    wallet status (no secrets)
``GET  /api/wallet/cards``   read    saved cards (brand, last4, expiry only)
``POST /api/wallet/setup-intent``    start adding a card
``POST /api/wallet/attach``          save the card the form produced
``GET  /api/wallet/limits``          caps, spent, remaining
``POST /api/wallet/propose``         create a pending order
``POST /api/wallet/approve``         approve or deny an order (single use)
===========================  ======  =========================================

Auth is the single-owner minimum used by the chat API: bearer token or session
cookie on every call, and a CSRF token (or same-origin check) on mutations.
Requests carrying raw card data are rejected before any handler sees them, and
no handler echoes request values into an error body.
"""

from __future__ import annotations

import hmac
import json
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from payments.stripe_wallet import WalletNotConfigured
from payments.wallet import (
    MAX_AMOUNT_CENTS,
    Wallet,
    WalletError,
    contains_raw_card_data,
)

from .api import Request, Response

__all__ = ["WalletAPI"]

_JSON_LIMIT = 16 * 1024
_MUTATING = frozenset({"POST", "PATCH", "DELETE", "PUT"})
_ATTACH_FIELDS = frozenset({"payment_method_id", "make_default"})
_PROPOSE_FIELDS = frozenset(
    {"amount_cents", "currency", "description", "idempotency_key"}
)
_APPROVE_FIELDS = frozenset({"proposal_id", "decision", "payment_method_id"})


class WalletAPI:
    def __init__(
        self,
        wallet: Wallet,
        *,
        auth_token: str | None = None,
        csrf_token: str | None = None,
    ) -> None:
        self.wallet = wallet
        self.auth_token = auth_token
        self.csrf_token = csrf_token

    # -- dispatch ----------------------------------------------------------

    def handle(self, request: Request) -> Response:
        try:
            self._authorize(request)
            path = unquote(urlsplit(request.path).path)
            segments = [segment for segment in path.split("/") if segment]
            if (
                len(segments) < 2
                or segments[0] != "api"
                or segments[1] != "wallet"
            ):
                raise WalletError("not_found")
            return self._route(request, request.method.upper(), segments[2:])
        except WalletError as exc:
            return _json_response(exc.status, exc.to_body())
        except WalletNotConfigured:
            return _error(
                503,
                "not_configured",
                "The wallet isn't connected yet.",
                user_fixable=False,
            )
        except Exception:  # noqa: BLE001 - never leak a stack trace to the client
            return _error(
                500,
                "internal_error",
                "Something went wrong.",
                retryable=True,
            )

    def _route(
        self, request: Request, method: str, rest: list[str]
    ) -> Response:
        if not rest:
            if method == "GET":
                return _json_response(200, {"ok": True, "wallet": self.wallet.status()})
            raise WalletError("not_found")

        head = rest[0]
        if len(rest) != 1:
            raise WalletError("not_found")
        if head == "cards":
            if method != "GET":
                raise WalletError("not_found")
            return _json_response(200, {"ok": True, "cards": self.wallet.list_cards()})
        if head == "setup-intent":
            if method != "POST":
                raise WalletError("not_found")
            self._json_body(request, allowed=frozenset())
            return _json_response(
                200, {"ok": True, "setup_intent": self.wallet.create_setup_intent()}
            )
        if head == "attach":
            if method != "POST":
                raise WalletError("not_found")
            body = self._json_body(request, allowed=_ATTACH_FIELDS)
            payment_method_id = _required_text(body, "payment_method_id")
            make_default = _optional_bool(body, "make_default", default=False)
            card = self.wallet.attach(
                payment_method_id, make_default=make_default
            )
            return _json_response(200, {"ok": True, "card": card})
        if head == "limits":
            if method != "GET":
                raise WalletError("not_found")
            currency = request.query_one("currency")
            return _json_response(
                200, {"ok": True, "limits": self.wallet.limits(currency)}
            )
        if head == "propose":
            if method != "POST":
                raise WalletError("not_found")
            body = self._json_body(request, allowed=_PROPOSE_FIELDS)
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
            proposal, created = self.wallet.propose(
                amount,
                currency=currency,
                description=description or "",
                idempotency_key=key,
            )
            return _json_response(
                201 if created else 200,
                {"ok": True, "proposal": proposal.to_public(), "idempotent": not created},
            )
        if head == "approve":
            if method != "POST":
                raise WalletError("not_found")
            body = self._json_body(request, allowed=_APPROVE_FIELDS)
            proposal_id = _required_text(body, "proposal_id")
            decision = body.get("decision", "approve")
            if not isinstance(decision, str):
                raise WalletError(
                    "invalid_request",
                    message="Decision must be 'approve' or 'deny'.",
                    user_fixable=True,
                )
            payment_method_id = body.get("payment_method_id")
            if payment_method_id is not None and not isinstance(
                payment_method_id, str
            ):
                raise WalletError("invalid_payment_method", user_fixable=True)
            proposal, idempotent = self.wallet.approve(
                proposal_id,
                decision=decision,
                payment_method_id=payment_method_id,
            )
            return _json_response(
                200,
                {
                    "ok": True,
                    "proposal": proposal.to_public(),
                    "idempotent": idempotent,
                },
            )
        raise WalletError("not_found")

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
        self,
        request: Request,
        *,
        allowed: frozenset[str],
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
            # Card details belong in Stripe's form, never in this API. Do not
            # echo the value back.
            raise WalletError(
                "unstorable_card",
                message="Card details can't be sent here. Use the card form.",
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


def _optional_bool(body: Mapping[str, Any], name: str, *, default: bool) -> bool:
    value = body.get(name, default)
    if not isinstance(value, bool):
        raise WalletError(
            "invalid_request",
            message=f"{name} must be true or false.",
            user_fixable=True,
            field=name,
        )
    return value


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

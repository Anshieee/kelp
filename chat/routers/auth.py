"""Authentication, sessions, CSRF, and the ``/api/auth`` routes.

Every function takes the ``ChatAPI`` instance as ``api`` so the split keeps the
same object graph and the same per-request cache that the monolithic class had.
No status code, header, cookie, or error body may change here.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

from ..errors import ChatError
from ..logging import log
from ._util import (
    CSRF_COOKIE,
    _cookie_header,
    _clear_cookie_header,
    _DECISION_LIMIT,
    _json_response,
    _parse_cookies,
    _rate_limited_response,
    _secret_hash,
    SESSION_COOKIE,
    DEVICE_ID_SHORT_LENGTH,
    DEVICE_ONLINE_SECONDS,
    _MUTATING,
    OWNER_IDENTITY,
)

if TYPE_CHECKING:
    from ..api import ChatAPI, Request, Response, _Session

#: The app has exactly one owner, so every authenticated credential maps to
#: this one limiter key. Defined in ``_util`` for the router split.


class _AuthLocked(Exception):
    """Raised after too many failures; carries a usable ``Retry-After``."""

    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__("authentication locked out")


@dataclass(frozen=True)
class _AuthResult:
    ok: bool
    via: str = "none"
    session: "_Session | None" = None
    session_id: str | None = field(default=None, repr=False)


def _client_address(request: "Request") -> str:
    """Best-effort peer identity for an unauthenticated caller.

    The adapter records the socket peer in :attr:`Request.client`. Forwarded
    headers are only a fallback for tests and embedded servers that do not, and
    the first hop is taken: a proxy appends, it does not replace.
    """

    if request.client:
        return str(request.client)[:64]
    forwarded = request.header("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first[:64]
    for name in ("x-real-ip", "cf-connecting-ip"):
        value = request.header(name)
        if value and value.strip():
            return value.strip()[:64]
    return "unknown"


def _presented_token(request: "Request") -> str | None:
    header = request.header("authorization") or ""
    prefix = "Bearer "
    if header.startswith(prefix) and header[len(prefix) :]:
        return header[len(prefix) :]
    session = _parse_cookies(request.header("cookie")).get("session")
    return session or None


def _client_identity(request: "Request") -> str:
    """A stable, non-secret limiter key for an unauthenticated caller.

    Authorized traffic is keyed on the owner instead
    (:func:`_limiter_identity`), so this is the fallback for a deployment with
    auth disabled. A presented token is hashed, never retained: the map must
    not become a secondary store of the owner token.
    """

    token = _presented_token(request)
    if token:
        digest = hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()
        return f"owner:{digest[:24]}"
    return f"client:{_client_address(request)}"


def _classify_route(method: str, segments: list[str]) -> str:
    """Map a route to its rate-limit class; unknown routes are cheap reads."""

    parts = segments[1:] if segments and segments[0] == "api" else segments
    if not parts:
        return "read"
    head = parts[0]
    verb = method.upper()
    if verb == "GET":
        return "admin" if head in {"metrics", "readyz"} else "read"
    if head == "attachments":
        return "upload"
    if head == "wallet":
        return "read" if verb == "GET" else "chat"
    if head in {"threads", "turns", "approvals"}:
        return "chat"
    return "read"


def _auth_failure_identity(request: "Request") -> str:
    """A hashed failure key: credential digest when presented, else peer digest.

    The raw bearer token, cookie, and client address stay out of the tracker
    and out of the ``auth_failed`` record. Hashing the peer too keeps the log
    free of an address that could identify a person.
    """

    token = _presented_token(request)
    seed = f"credential:{token}" if token else f"peer:{_client_address(request)}"
    return _secret_hash(seed)[:32]


def _is_sign_in(request: "Request") -> bool:
    if request.method.upper() != "POST":
        return False
    try:
        path = unquote(urlsplit(request.path).path)
    except ValueError:  # pragma: no cover - urlsplit rarely raises here
        return False
    return [segment for segment in path.split("/") if segment] == [
        "api",
        "auth",
        "session",
    ]


def _same_origin(request: "Request") -> bool:
    """True only for a browser origin that matches the request ``Host``.

    A missing or unparsable ``Origin`` is not same-origin. The comparison is on
    ``scheme://host:port``; a reverse proxy must preserve the incoming ``Host``
    header for this to hold.
    """

    origin = request.header("origin")
    if not origin:
        return False
    host = request.header("host") or ""
    if not host:
        return False
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    return hmac.compare_digest(parsed.netloc, host)


# -- limiter and authorization --------------------------------------------


def _limit(
    api: "ChatAPI", request: "Request", segments: list[str]
) -> "Response | None":
    """Charge the request's limiter class, or return a typed 429.

    Unauthenticated calls are checked by the independent ``auth`` bucket,
    keyed by peer address, before :func:`_authorize` produces its 401. A valid
    credential is never charged against that bucket. Any limiter fault fails
    open: a broken bucket must not turn a request into a 500.
    """

    try:
        if not api.limiter.enabled:
            return None
        if _authorized_for(api, request):
            route = _classify_route(request.method, segments)
            allowed, retry_after = api.limiter.consume(
                route, _limiter_identity(api, request)
            )
            # A successful login clears accumulated failure debt for this
            # peer without touching the owner's route budget.
            api.limiter.forget_class("auth", _client_address(request))
            if not allowed:
                return _rate_limited_response(
                    retry_after,
                    class_name=route,
                    reason="rate",
                    metrics=api.metrics,
                )
            return None
        allowed, retry_after = api.limiter.consume("auth", _client_address(request))
        if not allowed:
            return _rate_limited_response(
                retry_after,
                class_name="auth",
                reason="failed_auth",
                metrics=api.metrics,
            )
        return None
    except Exception:  # noqa: BLE001 - observability/limits never break traffic
        log("rate_limit_error", level="warning", status="error")
        return None


def _limiter_identity(api: "ChatAPI", request: "Request") -> str:
    """The limiter key for an already-authorized request.

    This is a single-owner app, so the root bearer token and every session
    cookie minted from it are the same principal: all of them share one
    constant key, which is what makes the per-owner budget really per owner.
    With auth disabled there is no owner to name, so the peer address remains
    the key and distinct callers keep distinct buckets.
    """

    auth = _authenticate(api, request)
    if auth.ok and auth.via != "disabled":
        return OWNER_IDENTITY
    return _client_identity(request)


def _authorize(api: "ChatAPI", request: "Request") -> None:
    auth = _authenticate(api, request)
    if not auth.ok:
        _reject_auth(api, request)
        raise ChatError("unauthorized", status=401)
    if not _is_sign_in(request):
        # A sign-in authenticates its own bearer separately, so the cookie
        # that let the wrapper through must not erase the debt of the
        # credential the request actually presented.
        api.auth_failures.clear(_auth_failure_identity(request))
    if request.method.upper() in _MUTATING and not _csrf_ok(api, request, auth):
        raise ChatError("csrf_failed", status=403)


def _reject_auth(api: "ChatAPI", request: "Request") -> None:
    """Count one rejection and emit exactly one ``auth_failed`` record.

    The record carries a hashed identity, a reason code, and a count. It never
    carries the presented bearer token, cookie value, or IP.
    """

    identity = _auth_failure_identity(request)
    count, remaining = api.auth_failures.status(identity)
    if remaining is not None:
        log(
            "auth_failed",
            level="warning",
            outcome="locked",
            reason="lockout",
            count=count,
            status=429,
            identity_hash=identity,
        )
        raise _AuthLocked(remaining)
    count, remaining = api.auth_failures.record(identity)
    if remaining is not None:
        log(
            "auth_failed",
            level="warning",
            outcome="locked",
            reason="lockout",
            count=count,
            status=429,
            identity_hash=identity,
        )
        raise _AuthLocked(remaining)
    log(
        "auth_failed",
        level="warning",
        outcome="rejected",
        reason="bad_credentials",
        count=count,
        status=401,
        identity_hash=identity,
    )


def _authenticate(
    api: "ChatAPI", request: "Request", *, fresh: bool = False
) -> _AuthResult:
    """Resolve and cache the credential for one request.

    ``fresh`` forces a second store lookup, which the stream's auth check uses
    so a revoke or expiry closes an open SSE connection. The cached result
    keeps the limiter and dispatch from doing the same lookup twice.
    """

    cached = getattr(request, "_kelp_auth", None)
    if cached is not None and not fresh:
        return cached
    result = _authenticate_uncached(api, request)
    request._kelp_auth = result  # type: ignore[attr-defined]
    return result


def _authenticate_uncached(api: "ChatAPI", request: "Request") -> _AuthResult:
    if not api.auth_token:
        return _AuthResult(True, "disabled")
    if _bearer_authorized(api, request):
        return _AuthResult(True, "bearer")
    session_id = _parse_cookies(request.header("cookie")).get(SESSION_COOKIE) or ""
    if not session_id:
        return _AuthResult(False)
    record = api.sessions.lookup(session_id)
    if record is None:
        return _AuthResult(False)
    return _AuthResult(True, "cookie", record, session_id)


def _authorized_for(api: "ChatAPI", request: "Request") -> bool:
    return _authenticate(api, request, fresh=True).ok


def _bearer_authorized(api: "ChatAPI", request: "Request") -> bool:
    if not api.auth_token:
        return False
    header = request.header("authorization") or ""
    prefix = "Bearer "
    if not header.startswith(prefix):
        return False
    return hmac.compare_digest(header[len(prefix) :], api.auth_token)


def _csrf_ok(api: "ChatAPI", request: "Request", auth: _AuthResult) -> bool:
    """One mutation gate with different rules for ambient and explicit auth.

    A cookie is ambient: the browser attaches it to a cross-site request, so
    the cookie path requires a matching ``Origin`` **and** the per-session
    double-submit pair (``csrf`` cookie plus ``X-CSRF-Token``). An explicit
    bearer token is not ambient: it keeps the existing static-token or
    same-origin check, and a bearer sign-in needs neither because it already
    proves possession of the root token.
    """

    if request.method.upper() not in _MUTATING:
        return True
    if auth.via == "cookie":
        if not _same_origin(request):
            return False
        header = request.header("x-csrf-token") or ""
        cookie = _parse_cookies(request.header("cookie")).get(CSRF_COOKIE) or ""
        if not header or not cookie or not hmac.compare_digest(header, cookie):
            return False
        return auth.session is not None and api.sessions.csrf_matches(
            auth.session, header
        )
    if _is_sign_in(request) and auth.via == "bearer":
        return True
    if api.csrf_token:
        supplied = request.header("x-csrf-token") or ""
        return hmac.compare_digest(supplied, api.csrf_token)
    if not request.header("origin"):
        # Non-browser local calls with an explicit bearer token or with auth
        # disabled: there is no ambient credential to protect.
        return True
    return _same_origin(request)


def _roll_session_cookie(
    api: "ChatAPI", request: "Request", response: "Response"
) -> None:
    if not 200 <= response.status < 400:
        return
    auth = _authenticate(api, request)
    if auth.session is None or auth.session_id is None:
        return
    if any(
        cookie.split(";", 1)[0].startswith(f"{SESSION_COOKIE}=")
        for cookie in response.cookies
    ):
        return
    updated = api.sessions.touch(auth.session)
    if updated is None:
        return
    response.cookies.extend(
        _session_cookies(api, auth.session_id, api.sessions.csrf_token(updated))
    )


def _session_cookies(api: "ChatAPI", session_id: str, csrf_token: str) -> list[str]:
    ttl = api.sessions.ttl
    now = api.sessions.now()
    return [
        _cookie_header(
            SESSION_COOKIE,
            session_id,
            ttl=ttl,
            secure=api.cookie_secure,
            http_only=True,
            now=now,
        ),
        _cookie_header(
            CSRF_COOKIE,
            csrf_token,
            ttl=ttl,
            secure=api.cookie_secure,
            http_only=False,
            now=now,
        ),
    ]


def _clear_auth_cookies(api: "ChatAPI") -> list[str]:
    return [
        _clear_cookie_header(
            SESSION_COOKIE, secure=api.cookie_secure, http_only=True
        ),
        _clear_cookie_header(
            CSRF_COOKIE, secure=api.cookie_secure, http_only=False
        ),
    ]


# -- auth routes -----------------------------------------------------------


def _auth(
    api: "ChatAPI", request: "Request", method: str, rest: list[str]
) -> "Response":
    if len(rest) == 1 and rest[0] == "session":
        if method == "POST":
            return _sign_in(api, request)
        if method == "DELETE":
            return _revoke_current(api, request)
    if len(rest) == 1 and rest[0] == "sessions" and method == "DELETE":
        return _revoke_all(api)
    if len(rest) == 1 and rest[0] == "devices" and method == "GET":
        return _list_devices(api, request)
    if len(rest) == 2 and rest[0] == "devices" and method == "DELETE":
        return _revoke_device(api, request, rest[1])
    raise ChatError("not_found", status=404, message="Unknown endpoint.")


def _sign_in(api: "ChatAPI", request: "Request") -> "Response":
    """Mint a session from the root bearer token.

    Requiring the bearer here is the point: a cookie is never sufficient to
    create another cookie, so a stolen session cannot extend itself
    indefinitely or survive a root-token rotation by minting a replacement. A
    wrong bearer is a rejected credential like any other: it is logged,
    counted, and can lock the identity out, even when the request also carries
    a session cookie that authorized the route.

    An optional ``device_label`` names the device shown by
    ``GET /api/auth/devices``. It is scrubbed to 1-40 characters and is never
    written to a log or audit record.
    """

    from .body import _json_body

    if not _bearer_authorized(api, request):
        _reject_auth(api, request)
        raise ChatError("unauthorized", status=401)
    body = _json_body(api, request, optional=True, limit=_DECISION_LIMIT)
    label = body.get("device_label")
    session_id, csrf_token, record = api.sessions.create(label=label)
    response = _json_response(
        201,
        {
            "ok": True,
            "session": {
                "expires_at": int(record.expires_at),
                "csrf_token": csrf_token,
                "device": {
                    "id": record.id_hash[:DEVICE_ID_SHORT_LENGTH],
                    "label": record.label,
                },
            },
        },
    )
    response.cookies.extend(_session_cookies(api, session_id, csrf_token))
    return response


def _list_devices(api: "ChatAPI", request: "Request") -> "Response":
    auth = _authenticate(api, request)
    devices = api.sessions.devices(current_session_id=auth.session_id)
    return _json_response(
        200,
        {
            "ok": True,
            "devices": devices,
            "presence_seconds": int(DEVICE_ONLINE_SECONDS),
        },
    )


def _revoke_device(
    api: "ChatAPI", request: "Request", id_short: str
) -> "Response":
    """Revoke one device by its public short id.

    The route is authenticated and, for a cookie caller, CSRF-checked like
    every other mutation. Revoking the calling device clears its cookies;
    revoking another device leaves the caller's session intact and closes that
    device's open SSE stream on its next fresh auth check.
    """

    record = api.sessions.revoke_short(unquote(id_short))
    if record is None:
        raise ChatError("not_found", status=404, message="Unknown device.")
    response = _json_response(
        200,
        {
            "ok": True,
            "revoked": 1,
            "device": {"id": record.id_hash[:DEVICE_ID_SHORT_LENGTH]},
        },
    )
    auth = _authenticate(api, request)
    if auth.session_id and _secret_hash(auth.session_id) == record.id_hash:
        response.cookies.extend(_clear_auth_cookies(api))
    return response


def _revoke_current(api: "ChatAPI", request: "Request") -> "Response":
    auth = _authenticate(api, request)
    if auth.session_id is None:
        raise ChatError(
            "invalid_request",
            status=400,
            message="A session cookie is required.",
            user_fixable=True,
        )
    revoked = api.sessions.revoke(auth.session_id)
    response = _json_response(200, {"ok": True, "revoked": 1 if revoked else 0})
    response.cookies.extend(_clear_auth_cookies(api))
    return response


def _revoke_all(api: "ChatAPI") -> "Response":
    revoked = api.sessions.revoke_all()
    response = _json_response(200, {"ok": True, "revoked": revoked})
    response.cookies.extend(_clear_auth_cookies(api))
    return response


__all__ = [
    "_AuthLocked",
    "_AuthResult",
    "_auth",
    "_authenticate",
    "_authorize",
    "_limit",
    "_limiter_identity",
    "_roll_session_cookie",
]

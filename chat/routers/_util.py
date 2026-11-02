"""Small HTTP helpers shared by the route modules.

These are behaviour-neutral extractions of the support functions that used to
live beside ``ChatAPI``. Nothing here may change a status, a header, or an
error body: the route modules and ``chat.api`` both call these exact bodies.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from email.utils import formatdate
from typing import TYPE_CHECKING, Any, Callable, Iterator, Mapping
from urllib.parse import unquote, urlsplit

from ..errors import ChatError

if TYPE_CHECKING:
    from ..api import Response
    from ..metrics import Metrics

# -- request shapes --------------------------------------------------------

_THREAD_RE = re.compile(r"^[A-Za-z0-9:_-]{1,80}$")
_CLIENT_TURN_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_MUTATING = frozenset({"POST", "PATCH", "DELETE", "PUT"})
_JSON_LIMIT = 1024 * 1024
_DECISION_LIMIT = 1024
_UPLOAD_OVERHEAD = 64 * 1024
_RATE_LIMITED_FALLBACK_RETRY = 60.0
#: The app has exactly one owner, so every authenticated credential maps to
#: this one limiter key.
OWNER_IDENTITY = "owner"

# -- owner sessions --------------------------------------------------------

SESSION_COOKIE = "session"
CSRF_COOKIE = "csrf"
#: A device is "online" when its last authenticated request is this recent.
DEVICE_ONLINE_SECONDS = 60.0
_DEVICE_LABEL_MAX = 40
_DEFAULT_DEVICE_LABEL = "Unknown device"
#: 48 bits of the hashed id: enough to be unique, useless for replaying a login.
DEVICE_ID_SHORT_LENGTH = 12
_DEVICE_ID_SHORT_RE = re.compile(r"^[0-9a-f]{6,64}$")
#: Control, zero-width, and bidi characters are stripped from a device label
#: before it is stored, so a label can never smuggle a log line or terminal
#: escape into an operator view.
_LABEL_STRIP_RE = re.compile(
    "[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]"
)
_WHITESPACE_RE = re.compile(r"\s+")
_EPOCH_COOKIE_DATE = "Thu, 01 Jan 1970 00:00:00 GMT"

# -- route labels ----------------------------------------------------------

_DYNAMIC_ROUTE_HEADS = frozenset(
    {"threads", "turns", "approvals", "artifacts", "shared"}
)
_STATIC_ROUTE_HEADS = frozenset(
    {"health", "healthz", "readyz", "metrics", "attachments"}
)
_ROUTE_ACTIONS = frozenset(
    {
        "stream",
        "turns",
        "stop",
        "retry",
        "decision",
        "preview",
        "content",
    }
)


def _route_label(path: str) -> str:
    """A bounded route label for metrics: ids are replaced, not recorded."""

    try:
        clean = unquote(urlsplit(path).path)
    except ValueError:  # pragma: no cover - urlsplit rarely raises here
        return "/api/other"
    segments = [segment for segment in clean.split("/") if segment]
    if not segments:
        return "/"
    if segments[0] != "api":
        return "/other"
    if len(segments) == 1:
        return "/api"
    head = segments[1]
    if head in _STATIC_ROUTE_HEADS:
        return f"/api/{head}" if len(segments) == 2 else "/api/other"
    if head in _DYNAMIC_ROUTE_HEADS:
        if len(segments) == 2:
            return f"/api/{head}"
        label = f"/api/{head}/{{id}}"
        if len(segments) == 4 and segments[3] in _ROUTE_ACTIONS:
            label = f"{label}/{segments[3]}"
        elif len(segments) > 4:
            return "/api/other"
        return label
    return "/api/other"


def _gauge(value: float) -> str:
    """Prometheus number formatting without importing the metrics internals."""

    number = float(value)
    if not math.isfinite(number):
        number = 0.0
    if number.is_integer():
        return str(int(number))
    return repr(round(number, 9))


# -- response builders -----------------------------------------------------


def _json_response(status: int, payload: Mapping[str, Any]) -> "Response":
    from ..api import Response

    body = json.dumps(payload, default=str, separators=(",", ":")).encode("utf-8")
    return Response(
        status=status,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(body)),
            "Cache-Control": "no-store",
        },
        body=body,
    )


def _method_not_allowed_response(allow: str) -> "Response":
    """The one 405 builder: a typed envelope plus the ``Allow`` it proves."""

    error = ChatError(
        "method_not_allowed",
        status=405,
        message="That method is not supported here.",
    )
    response = _json_response(405, error.to_body())
    response.headers["Allow"] = allow
    return response


def _rate_limited_response(
    retry_after: float,
    *,
    class_name: str,
    reason: str,
    limit: int | None = None,
    metrics: "Metrics | None" = None,
) -> "Response":
    """Build the one 429 shape: ``rate_limited`` plus a usable Retry-After.

    The status is always 429 and the body is always the ordinary error
    envelope, so the client's existing rate-limited state renders it without a
    special case.

    This is the only place a 429 is built, so it is also the only place the
    refusal counter is incremented: one call means exactly one refusal for the
    limiter class named in the error details. Counting is best effort - a
    broken registry must not turn a refusal into a 500.
    """

    try:
        seconds = float(retry_after)
    except (TypeError, ValueError):
        seconds = _RATE_LIMITED_FALLBACK_RETRY
    if not math.isfinite(seconds) or seconds <= 0:
        seconds = _RATE_LIMITED_FALLBACK_RETRY
    header = str(max(1, int(math.ceil(seconds))))
    details: dict[str, Any] = {"class": class_name, "reason": reason}
    if limit is not None:
        details["limit"] = max(0, int(limit))
    if metrics is not None:
        try:
            metrics.inc_rate_limit_refusal(class_name)
        except Exception:  # noqa: BLE001 - observability never breaks a refusal
            pass
    error = ChatError(
        "rate_limited",
        status=429,
        retryable=True,
        details=details,
    )
    response = _json_response(429, error.to_body())
    response.headers["Retry-After"] = header
    return response


def _busy_response(
    *,
    class_name: str,
    reason: str,
    limit: int,
    retry_after: float = 1.0,
    metrics: "Metrics | None" = None,
) -> "Response":
    return _rate_limited_response(
        retry_after,
        class_name=class_name,
        reason=reason,
        limit=limit,
        metrics=metrics,
    )


# -- request helpers -------------------------------------------------------


def _segment(value: str) -> str:
    value = unquote(value)
    if not _THREAD_RE.match(value) or value in {".", ".."}:
        raise ChatError("not_found", status=404, message="Unknown endpoint.")
    return value


def _int_param(request: Any, name: str, default: int, low: int, high: int) -> int:
    raw = request.query_one(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ChatError(
            "invalid_request",
            status=400,
            message=f"{name} must be an integer.",
            user_fixable=True,
        ) from None
    return max(low, min(value, high))


def _after_param(request: Any) -> int:
    raw = request.header("last-event-id") or request.query_one("after") or "0"
    try:
        value = int(raw)
    except ValueError:
        raise ChatError(
            "invalid_request",
            status=400,
            message="after must be a sequence number.",
            user_fixable=True,
        ) from None
    return max(0, value)


def _parse_cookies(value: str | None) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in (value or "").split(";"):
        if "=" in part:
            name, _, raw = part.partition("=")
            cookies[name.strip()] = raw.strip()
    return cookies


def _multipart_file(request: Any) -> tuple[str, bytes]:
    content_type = request.header("content-type") or ""
    if not content_type.lower().startswith("multipart/form-data"):
        raise ChatError(
            "invalid_request",
            status=400,
            message="Uploads must be multipart/form-data.",
            user_fixable=True,
        )
    match = re.search(r'boundary="?([^";]+)"?', content_type)
    if match is None:
        raise ChatError(
            "invalid_request",
            status=400,
            message="The multipart boundary is missing.",
            user_fixable=True,
        )
    boundary = b"--" + match.group(1).encode("ascii", "ignore")
    for chunk in request.body.split(boundary):
        if not chunk or chunk.startswith(b"--"):
            continue
        chunk = chunk.lstrip(b"\r\n")
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        head, separator, data = chunk.partition(b"\r\n\r\n")
        if not separator:
            continue
        disposition = ""
        for line in head.split(b"\r\n"):
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-disposition":
                disposition = value.decode("utf-8", "replace")
        if 'name="file"' not in disposition:
            continue
        file_match = re.search(r'filename="([^"]*)"', disposition)
        filename = file_match.group(1) if file_match else "upload"
        return filename, data
    raise ChatError(
        "invalid_request",
        status=400,
        message="No file field in the upload.",
        user_fixable=True,
    )


# -- credentials and cookies ----------------------------------------------


def _secret_hash(value: str) -> str:
    """One-way stable key for a credential or peer identity."""

    import hashlib

    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def scrub_device_label(value: Any) -> str:
    """Return a safe 1-40 character device label.

    Control, zero-width, and bidi characters are removed and whitespace is
    collapsed. An absent or empty label becomes ``Unknown device``. A label
    that is still too long raises :class:`ChatError`, so it cannot become a
    storage or log hazard.
    """

    if value is None:
        return _DEFAULT_DEVICE_LABEL
    if not isinstance(value, str):
        raise ChatError(
            "invalid_request",
            status=400,
            message="device_label must be a string.",
            user_fixable=True,
        )
    clean = _WHITESPACE_RE.sub(" ", _LABEL_STRIP_RE.sub("", value)).strip()
    if not clean:
        return _DEFAULT_DEVICE_LABEL
    if len(clean) > _DEVICE_LABEL_MAX:
        raise ChatError(
            "invalid_request",
            status=400,
            message="device_label must be 1-40 characters.",
            user_fixable=True,
        )
    return clean


def _cookie_header(
    name: str,
    value: str,
    *,
    ttl: float,
    secure: bool,
    http_only: bool,
    now: float | None = None,
) -> str:
    """Build one RFC 6265 ``Set-Cookie`` value.

    ``Max-Age`` is authoritative for modern clients; ``Expires`` keeps older
    ones aligned. ``Secure`` is on by default and can be disabled only for a
    loopback HTTP development run.
    """

    seconds = max(0, int(ttl))
    moment = time.time() if now is None else now
    expires = formatdate(moment + seconds, usegmt=True)
    parts = [
        f"{name}={value}",
        "Path=/",
        f"Max-Age={seconds}",
        f"Expires={expires}",
        "SameSite=Lax",
    ]
    if secure:
        parts.append("Secure")
    if http_only:
        parts.append("HttpOnly")
    return "; ".join(parts)


def _clear_cookie_header(name: str, *, secure: bool, http_only: bool) -> str:
    parts = [f"{name}=", "Path=/", "Max-Age=0", f"Expires={_EPOCH_COOKIE_DATE}", "SameSite=Lax"]
    if secure:
        parts.append("Secure")
    if http_only:
        parts.append("HttpOnly")
    return "; ".join(parts)


# -- stream leases ---------------------------------------------------------


class _Lease:
    """Idempotent release for a stream slot, including a never-started stream."""

    def __init__(self, release: Callable[[], None]):
        self._release = release
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        try:
            self._release()
        except Exception:  # noqa: BLE001 - a lease must never raise
            pass

    def __del__(self) -> None:  # pragma: no cover - timing is interpreter-owned
        try:
            self.release()
        except Exception:  # noqa: BLE001
            pass


def guarded_stream(
    frames: Iterator[str], release: Callable[[], None]
) -> Iterator[str]:
    """Bind one idempotent release to the lifetime of a stream body."""

    lease = _Lease(release)

    def guarded() -> Iterator[str]:
        try:
            yield from frames
        finally:
            lease.release()

    return guarded()


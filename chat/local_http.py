"""Pure L1 header policy for the loopback sidecar.

The validator takes the parsed request headers (``http.client.HTTPMessage`` is
an ``email.message.Message``), the actual bound port, and the expected owner
bearer. It returns one fixed :class:`LocalHttpRejection` member or ``None`` and
does nothing else: no HTTP status, rendering, logging, CORS, or socket access
belongs here. A caller may act on the result only after this function returns.

The accepted request carries exactly one canonical loopback ``Host``, exactly
one ``Origin`` equal to ``http://`` plus that same Host, and exactly one
``Authorization: Bearer <owner_bearer>``. Duplicates, absent values, commas,
suffixes, whitespace, wrong casing, wrong ports, and an empty expected bearer
all fail closed. A cookie is never consulted.
"""

from __future__ import annotations

import hmac
from email.message import Message
from enum import Enum

__all__ = ["LocalHttpRejection", "check_local_headers"]


class LocalHttpRejection(Enum):
    """Fixed refusal reasons; no member ever carries a secret."""

    INVALID_HOST = "invalid_host"
    INVALID_ORIGIN = "invalid_origin"
    UNAUTHORIZED = "unauthorized"


def _valid_port(port: object) -> bool:
    """An actual bound loopback port is a non-bool int in 1..65535."""

    return (
        isinstance(port, int)
        and not isinstance(port, bool)
        and 1 <= port <= 65535
    )


def _single(headers: Message, name: str) -> str | None:
    """Return the sole string value of ``name``, else ``None``."""

    values = headers.get_all(name)
    if values is None or len(values) != 1:
        return None
    value = values[0]
    return value if isinstance(value, str) else None


def check_local_headers(
    headers: Message, *, port: int, owner_bearer: str
) -> LocalHttpRejection | None:
    """Validate the L1 Host, Origin, and bearer headers.

    Returns ``None`` only for exactly one canonical loopback ``Host``, exactly
    one ``Origin`` of ``http://`` plus that Host, and exactly one
    ``Authorization: Bearer <owner_bearer>``. Every other shape fails closed.
    """

    if not _valid_port(port):
        return LocalHttpRejection.INVALID_HOST

    host = _single(headers, "Host")
    if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
        return LocalHttpRejection.INVALID_HOST

    origin = _single(headers, "Origin")
    if origin != f"http://{host}":
        return LocalHttpRejection.INVALID_ORIGIN

    if not isinstance(owner_bearer, str) or not owner_bearer:
        return LocalHttpRejection.UNAUTHORIZED

    authorization = _single(headers, "Authorization")
    if authorization is None:
        return LocalHttpRejection.UNAUTHORIZED

    expected = f"Bearer {owner_bearer}".encode("utf-8", "surrogatepass")
    presented = authorization.encode("utf-8", "surrogatepass")
    if not hmac.compare_digest(presented, expected):
        return LocalHttpRejection.UNAUTHORIZED
    return None

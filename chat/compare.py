"""Constant-time comparison of credential strings.

``hmac.compare_digest`` accepts two ``str`` arguments only when both are
ASCII-only; any other character raises ``TypeError``. HTTP header values are
decoded as latin-1 by ``http.client``/``http.server``, so a hostile
``Authorization: Bearer café`` would otherwise surface as a 500 and skip the
auth-failure accounting. Comparing the UTF-8 bytes keeps the comparison
constant-time and total over every ``str``.
"""

from __future__ import annotations

import hmac

__all__ = ["constant_time_equals"]


def constant_time_equals(a: str, b: str) -> bool:
    """Compare two strings in constant time without raising on non-ASCII.

    Args:
        a: The first string, for example a presented credential.
        b: The second string, for example the expected credential.

    Returns:
        ``True`` when both strings encode to identical UTF-8 bytes.
        Lone surrogates are encoded with ``surrogatepass``, so any ``str`` is
        accepted.
    """

    return hmac.compare_digest(
        a.encode("utf-8", "surrogatepass"), b.encode("utf-8", "surrogatepass")
    )

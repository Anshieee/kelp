"""Per-recipient egress ledger: who received how much, and why (KEL-22).

The policy gate decides *whether* a call may leave the process; the ledger
answers the other question the privacy review owes the owner: for every byte
that did leave, **which recipient** got it, **for what purpose**, and **how
much**. It stores counts and fixed labels, never content, and it cannot be
made to store content:

* the public API has no field that can carry a body, query, path, header, file
  name, or prompt — only a recipient id, a purpose token, and integers;
* ``recipient`` must be a known id from :data:`RECIPIENTS`;
* ``purpose`` must be a short lowercase token (a skill name or a call kind);
* sizes are validated non-negative integers (``bool`` is rejected).

Entries are written through :class:`openshell.audit.AuditLog` as ``egress``
records. That is deliberate: the ledger inherits the trail's ``0600`` mode,
mandatory secret scrubbing, rotation, and the retention row the runbook already
states (audit records: 30 days, 40 segments — ``docs/DEPLOY.md`` §8.1). There is
no second file to forget, and no second clock to drift.

Byte counts are of the request body as serialized and the response body as read;
headers, including the credential, are not counted and are never recorded. A
caller that gates on a denial writes zero response bytes, which is how a refused
attempt stays visible without pretending traffic happened.

``docs/PRIVACY.md`` lists every recipient. A test checks that the two stay in
step in both directions: a recipient in code that the review does not name
fails, and a recipient in the review that code does not know fails.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from .audit import AuditLog

__all__ = [
    "EGRESS_EVENT",
    "PURPOSE_RE",
    "RECIPIENTS",
    "EgressLedger",
    "recipient_for_host",
]

#: Audit event name. One line per completed, denied, or failed egress attempt.
EGRESS_EVENT = "egress"

#: Recipient id -> the hosts that belong to it. ``unlisted`` is the fail-visible
#: bucket for an operator-supplied base URL; the review names it too, because an
#: override is a real recipient even when this repo did not choose it.
RECIPIENTS: Mapping[str, tuple[str, ...]] = {
    "tokenfactory": ("api.tokenfactory.nebius.com",),
    "tavily": ("api.tavily.com",),
    "github": ("api.github.com",),
    "stripe": ("api.stripe.com",),
    "razorpay": ("api.razorpay.com",),
    "unlisted": (),
}

#: A purpose is a skill name (``web.search``) or a call kind
#: (``chat.completions``), never free text and never a URL.
PURPOSE_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")

_MAX_BYTES = 1 << 40  # 1 TiB: a sanity ceiling, not a limit on traffic


def recipient_for_host(host: str | None) -> str:
    """Map an outbound host to its recipient id, or ``"unlisted"``.

    Exact match only: a ``*.`` wildcard or a suffix match would let an attacker
    registered domain be reported under a trusted name.
    """

    candidate = (host or "").strip().lower().rstrip(".")
    if candidate:
        for recipient, hosts in RECIPIENTS.items():
            if candidate in hosts:
                return recipient
    return "unlisted"


def _count(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer byte count")
    if value < 0 or value > _MAX_BYTES:
        raise ValueError(f"{name} is out of range")
    return value


def _check_recipient(recipient: Any) -> str:
    if not isinstance(recipient, str) or recipient not in RECIPIENTS:
        raise ValueError(f"unknown egress recipient: {recipient!r}")
    return recipient


def _check_purpose(purpose: Any) -> str:
    if not isinstance(purpose, str) or PURPOSE_RE.match(purpose) is None:
        raise ValueError(f"invalid egress purpose: {purpose!r}")
    return purpose


class EgressLedger:
    """Writes one content-free ``egress`` line per outbound call.

    A ledger with no audit log is a no-op, so a caller can construct the
    instrumented object unconditionally; ``record`` returns ``None`` and
    nothing is written.
    """

    def __init__(self, audit: AuditLog | None = None):
        self._audit = audit

    @property
    def audit(self) -> AuditLog | None:
        return self._audit

    def record(
        self,
        *,
        recipient: str,
        purpose: str,
        request_bytes: int,
        response_bytes: int | None = None,
        outcome: str = "completed",
        method: str | None = None,
        host: str | None = None,
        turn_id: str | None = None,
        tool: str | None = None,
        status: int | None = None,
    ) -> dict[str, Any] | None:
        """Append one ledger entry; there is no parameter that can hold content."""

        target = _check_recipient(recipient)
        label = _check_purpose(purpose)
        extra: dict[str, Any] = {
            "recipient": target,
            "purpose": label,
            "request_bytes": _count("request_bytes", request_bytes),
            "response_bytes": _count(
                "response_bytes", 0 if response_bytes is None else response_bytes
            ),
            "outcome": _check_purpose(outcome),
        }
        if host:
            extra["host"] = str(host)
        if method:
            extra["method"] = _check_purpose(str(method).lower())
        if status is not None:
            extra["status"] = _count("status", int(status))
        if self._audit is None:
            return None
        return self._audit.record(
            EGRESS_EVENT,
            turn_id=turn_id,
            tool=tool,
            outcome=outcome,
            extra=extra,
        )

    def record_host(
        self,
        host: str | None,
        *,
        purpose: str,
        request_bytes: int,
        response_bytes: int | None = None,
        outcome: str = "completed",
        method: str | None = None,
        turn_id: str | None = None,
        tool: str | None = None,
        status: int | None = None,
    ) -> dict[str, Any] | None:
        """Same as :meth:`record`, resolving the recipient from the host."""

        return self.record(
            recipient=recipient_for_host(host),
            purpose=purpose,
            request_bytes=request_bytes,
            response_bytes=response_bytes,
            outcome=outcome,
            method=method,
            host=host,
            turn_id=turn_id,
            tool=tool,
            status=status,
        )

    def entries(self, **filters: Any) -> list[dict[str, Any]]:
        """Ledger entries, optionally filtered like :meth:`AuditLog.entries`."""

        if self._audit is None:
            return []
        return self._audit.entries(event=EGRESS_EVENT, **filters)

    def __repr__(self) -> str:
        return f"EgressLedger(audit={'yes' if self._audit else 'no'})"

    __str__ = __repr__

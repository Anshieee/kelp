"""Typed errors for the OpenShell approval gate.

Denials are values, not silent no-ops: every path records an audit entry and
returns or raises a code the caller can map to UI copy.
"""

from __future__ import annotations

__all__ = [
    "ApprovalAlreadyUsed",
    "ApprovalDenied",
    "ApprovalError",
    "ApprovalExpired",
    "ApprovalNotGranted",
    "ApprovalNotFound",
    "OpenShellError",
    "PolicyDenied",
]


class OpenShellError(Exception):
    """Base class for gate failures."""


class PolicyDenied(OpenShellError):
    """The policy engine refused the action; it never reaches approval."""

    def __init__(self, reason: str, blast_radius: tuple[str, ...] = ()):
        self.reason = reason
        self.blast_radius = tuple(blast_radius)
        super().__init__(reason)


class ApprovalError(OpenShellError):
    code = "approval_error"

    def __init__(self, message: str, *, request_id: str | None = None):
        self.message = message
        self.request_id = request_id
        super().__init__(message)


class ApprovalNotFound(ApprovalError):
    code = "unknown_approval"


class ApprovalExpired(ApprovalError):
    code = "expired"


class ApprovalDenied(ApprovalError):
    code = "denied"


class ApprovalAlreadyUsed(ApprovalError):
    code = "already_used"


class ApprovalNotGranted(ApprovalError):
    code = "not_approved"

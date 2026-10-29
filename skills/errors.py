"""Typed errors for the skill runtime.

Every failure crossing the skill boundary is machine-readable. See
``docs/SKILL_SPEC.md`` §8 for the envelope contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

__all__ = [
    "ERROR_CODES",
    "SkillError",
    "SkillHandlerError",
    "SkillInvocationError",
    "SkillManifestError",
    "SkillNotImplementedError",
    "Violation",
]

#: Codes allowed in an invocation error envelope. Adding one is a spec change.
ERROR_CODES = frozenset(
    {
        "unknown_skill",
        "invalid_arguments",
        "invalid_output",
        "approval_required",
        "approval_denied",
        "policy_denied",
        "timeout",
        "handler_error",
        "missing_secret",
        "not_implemented",
    }
)


class SkillError(Exception):
    """Base class for every error raised by the skill runtime."""


@dataclass(frozen=True)
class Violation:
    """One startup validation failure, addressed by a JSON Pointer."""

    skill: str
    field: str  # JSON Pointer, e.g. "/permissions/scopes/hosts/0"
    message: str

    def __str__(self) -> str:
        return f"{self.skill}: {self.field or '/'}: {self.message}"


class SkillManifestError(SkillError):
    """Raised at load time. All-or-nothing: no registry is built on failure."""

    def __init__(self, root: Any, violations: list[Violation] | tuple[Violation, ...]):
        self.root = str(root)
        self.violations: tuple[Violation, ...] = tuple(
            sorted(violations, key=lambda v: (v.skill, v.field))
        )
        header = f"{len(self.violations)} invalid skill manifest(s) under {self.root}"
        body = "\n".join(f"  - {v}" for v in self.violations)
        super().__init__(header + ("\n" + body if body else ""))

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": "skill_manifest_invalid",
            "root": self.root,
            "violations": [
                {"skill": v.skill, "field": v.field, "message": v.message}
                for v in self.violations
            ],
        }


class SkillInvocationError(SkillError):
    """Optional exception form of an error envelope (loaders normally return dicts)."""

    def __init__(self, envelope: Mapping[str, Any]):
        self.envelope = dict(envelope)
        super().__init__(self.envelope["error"]["message"])

    def to_envelope(self) -> dict[str, Any]:
        return dict(self.envelope)


class SkillHandlerError(Exception):
    """Raised by a handler to choose its own error code and retry semantics."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        user_fixable: bool = False,
        details: Mapping[str, Any] | None = None,
    ):
        if code not in ERROR_CODES:
            raise ValueError(f"unknown skill error code {code!r}")
        self.code = code
        self.message = message
        self.retryable = retryable
        self.user_fixable = user_fixable
        self.details = dict(details) if details else None
        super().__init__(message)


class SkillNotImplementedError(SkillHandlerError):
    """Placeholder used by prototype stubs until the real handler lands."""

    def __init__(self, skill: str):
        super().__init__(
            "not_implemented",
            f"Skill {skill!r} is not implemented in this build.",
            retryable=False,
            user_fixable=False,
            details={"stub": True},
        )

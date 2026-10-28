"""Execution context handed to every skill handler."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import SkillHandlerError

__all__ = ["ApprovalRequest", "SkillContext"]

_REDACTED = "[redacted]"
# Values shorter than this are not scrubbed: replacing a 3-char string would
# corrupt unrelated output. The primary rule is that handlers never log secrets.
_MIN_SCRUB_LEN = 4


@dataclass(frozen=True)
class ApprovalRequest:
    """Single-use request for the P0-3 policy gate.

    ``arguments`` is available to the policy/approval UI but is deliberately
    omitted from the envelope copy so model output never echoes raw arguments.
    """

    skill: str
    tool: str
    arguments: Mapping[str, Any]
    argument_hash: str
    effects: tuple[str, ...]
    scopes: Mapping[str, tuple[str, ...]]
    manifest_version: str
    requested_at: str
    expires_at: str

    def to_dict(self, *, include_arguments: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "skill": self.skill,
            "tool": self.tool,
            "argument_hash": self.argument_hash,
            "effects": list(self.effects),
            "scopes": {name: list(values) for name, values in self.scopes.items()},
            "manifest_version": self.manifest_version,
            "requested_at": self.requested_at,
            "expires_at": self.expires_at,
            "single_use": True,
        }
        if include_arguments:
            payload["arguments"] = dict(self.arguments)
        return payload


class SkillContext:
    """Per-invocation context. Secrets are resolved by name, never by value."""

    __slots__ = ("skill", "workspace_root", "_secrets")

    def __init__(
        self,
        skill: str,
        workspace_root: str | Path,
        secrets: Mapping[str, str] | None = None,
    ):
        self.skill = skill
        self.workspace_root = Path(workspace_root)
        self._secrets = dict(secrets or {})

    def secret(self, name: str) -> str:
        try:
            return self._secrets[name]
        except KeyError:
            raise SkillHandlerError(
                "missing_secret",
                f"Required secret {name!r} is not configured.",
                retryable=False,
                user_fixable=False,
                details={"secret": name},
            ) from None

    def has_secret(self, name: str) -> bool:
        return name in self._secrets

    def scrub(self, text: str) -> str:
        """Best-effort removal of known secret values from telemetry/errors."""

        if not isinstance(text, str):
            text = str(text)
        for value in self._secrets.values():
            if isinstance(value, str) and len(value) >= _MIN_SCRUB_LEN:
                text = text.replace(value, _REDACTED)
        return text

    def __repr__(self) -> str:
        return (
            f"SkillContext(skill={self.skill!r}, "
            f"workspace_root={str(self.workspace_root)!r}, secrets=<redacted>)"
        )

    __str__ = __repr__

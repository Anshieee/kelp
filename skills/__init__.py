"""Skill contracts: manifests, fail-fast loader, typed invocation envelopes.

Public surface (see ``docs/SKILL_SPEC.md``)::

    from skills import SkillRegistry, SkillContext, SkillManifestError

    registry = SkillRegistry.load()          # raises SkillManifestError on a bad manifest
    result = await registry.invoke("web.search", {"query": "nebius gpu"})
"""

from .context import ApprovalRequest, SkillContext
from .errors import (
    ERROR_CODES,
    SkillError,
    SkillHandlerError,
    SkillInvocationError,
    SkillManifestError,
    SkillNotImplementedError,
    Violation,
)
from .loader import (
    APPROVAL_TTL_SECONDS,
    DEFAULT_SKILL_ROOT,
    LoadedSkill,
    SkillRegistry,
    load_registry,
)

__all__ = [
    "APPROVAL_TTL_SECONDS",
    "DEFAULT_SKILL_ROOT",
    "ERROR_CODES",
    "ApprovalRequest",
    "LoadedSkill",
    "SkillContext",
    "SkillError",
    "SkillHandlerError",
    "SkillInvocationError",
    "SkillManifestError",
    "SkillNotImplementedError",
    "SkillRegistry",
    "Violation",
    "load_registry",
]

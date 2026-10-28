"""`github.read` handler stub — read-only GitHub API access."""

from __future__ import annotations

from typing import Any, Mapping

from skills.context import SkillContext
from skills.errors import SkillNotImplementedError


async def run(arguments: Mapping[str, Any], context: SkillContext) -> Mapping[str, Any]:
    """Read repo / issue / pull request / file data.

    Contract:
      - GET requests only; no writes, comments, labels, merges, or pushes
      - hosts limited to api.github.com, repos limited to the manifest allowlist
      - ``GITHUB_TOKEN`` read via ``context.secret``; never log or echo it
    """

    raise SkillNotImplementedError("github.read")

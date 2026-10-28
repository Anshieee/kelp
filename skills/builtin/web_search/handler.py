"""`web.search` handler stub — KEL-5 defines the contract; execution lands in P0-1/M1."""

from __future__ import annotations

from typing import Any, Mapping

from skills.context import SkillContext
from skills.errors import SkillNotImplementedError


async def run(arguments: Mapping[str, Any], context: SkillContext) -> Mapping[str, Any]:
    """Read-only web search. Must never mutate remote state.

    Contract:
      - network egress only to the hosts allowlisted in the manifest
      - ``TAVILY_API_KEY`` read via ``context.secret`` and never logged
    """

    raise SkillNotImplementedError("web.search")

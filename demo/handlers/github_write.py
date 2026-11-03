"""Demo fixture: never perform remote writes from the demo runtime."""

from __future__ import annotations

from typing import Any, Mapping

from skills.context import SkillContext
from skills.errors import SkillNotImplementedError


async def run(_arguments: Mapping[str, Any], _context: SkillContext) -> Mapping[str, Any]:
    raise SkillNotImplementedError("github.write")

"""Offline contract fixture. Never attach to a real browser from a replay."""
from typing import Any, Mapping

from skills.errors import SkillHandlerError


async def run(arguments: Mapping[str, Any], context: Any) -> dict[str, Any]:
    raise SkillHandlerError("not_implemented", "Browser actions need the live demo server.",
                            retryable=False, user_fixable=False)

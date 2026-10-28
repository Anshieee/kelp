"""`schedule.create` handler stub — the only write skill in the prototype cut."""

from __future__ import annotations

from typing import Any, Mapping

from skills.context import SkillContext
from skills.errors import SkillNotImplementedError


async def run(arguments: Mapping[str, Any], context: SkillContext) -> Mapping[str, Any]:
    """Persist a scheduled task and return its id and next run time.

    Contract:
      - only reachable after the P0-3 approval gate allows the call
      - writes only under ``${WORKSPACE}/var/scheduler``
      - non-idempotent: retries can create duplicates, so callers must not
        auto-retry without an approval decision
    """

    raise SkillNotImplementedError("schedule.create")

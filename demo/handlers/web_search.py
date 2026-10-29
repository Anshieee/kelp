"""Fixture ``web.search`` handler.

Returns the recorded result set in ``demo/fixtures/assets/web_results.json``.
No network call, no key: the response is a saved, make-believe search result so
the demo is deterministic and offline-safe.
"""

from __future__ import annotations

from typing import Any, Mapping

from demo.fixtures import assets
from skills.context import SkillContext

__all__ = ["run"]


async def run(arguments: Mapping[str, Any], context: SkillContext) -> Mapping[str, Any]:
    recorded = assets.web_results()
    limit = int(arguments.get("max_results") or 5)
    return {
        "query": str(arguments["query"]),
        "backend": str(recorded.get("backend") or "fixture"),
        "results": [dict(item) for item in recorded["results"][:limit]],
    }

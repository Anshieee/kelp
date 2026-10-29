"""Fixture ``github.read`` handler.

Serves the recorded GitHub payloads in ``demo/fixtures/assets/github.json``.
Read-only by construction: there is no request path here that can write.
"""

from __future__ import annotations

from typing import Any, Mapping

from demo.fixtures import assets
from skills.context import SkillContext
from skills.errors import SkillHandlerError

__all__ = ["run"]


async def run(arguments: Mapping[str, Any], context: SkillContext) -> Mapping[str, Any]:
    kind = str(arguments["kind"])
    repo = str(arguments["repo"])
    recorded = assets.github()
    known = str((recorded.get("repo") or {}).get("full_name") or "")
    if repo != known:
        raise SkillHandlerError(
            "handler_error",
            f"Repository {repo!r} is not part of the demo fixture.",
            retryable=False,
            user_fixable=True,
        )
    if kind not in recorded:
        raise SkillHandlerError(
            "handler_error",
            f"No recorded {kind!r} response in the demo fixture.",
            retryable=False,
            user_fixable=True,
        )
    item = recorded[kind]
    number = arguments.get("number")
    if number is not None and isinstance(item, Mapping) and item.get("number") != number:
        raise SkillHandlerError(
            "handler_error",
            f"The fixture has no {kind} numbered {number}.",
            retryable=False,
            user_fixable=True,
        )
    return {"kind": kind, kind: dict(item)}

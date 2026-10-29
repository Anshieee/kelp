"""Handler fixture module used by the contract tests.

These handlers are intentionally tiny; they exercise the loader/runtime, not
real skill behaviour.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

from skills.context import SkillContext

CALLS: list[dict[str, Any]] = []


def reset() -> None:
    CALLS.clear()


async def echo(arguments: Mapping[str, Any], context: SkillContext) -> Mapping[str, Any]:
    text = arguments["text"]
    return {"echo": text, "chars": len(text)}


async def side_effect(arguments: Mapping[str, Any], context: SkillContext) -> Mapping[str, Any]:
    CALLS.append(dict(arguments))
    return {"created": True, "id": f"task_{len(CALLS)}"}


async def slow(arguments: Mapping[str, Any], context: SkillContext) -> Mapping[str, Any]:
    await asyncio.sleep(0.3)
    return {"echo": "late", "chars": 4}


async def leaky(arguments: Mapping[str, Any], context: SkillContext) -> Mapping[str, Any]:
    raise RuntimeError("provider rejected token=" + context.secret("DEMO_TOKEN"))


async def raises(arguments: Mapping[str, Any], context: SkillContext) -> Mapping[str, Any]:
    raise ValueError("boom")


async def bad_output(arguments: Mapping[str, Any], context: SkillContext) -> Mapping[str, Any]:
    return {"echo": 1, "chars": "many"}


async def not_mapping(arguments: Mapping[str, Any], context: SkillContext) -> Any:
    return ["not", "an", "object"]


def sync_only(arguments: Mapping[str, Any], context: SkillContext) -> Mapping[str, Any]:
    return {"echo": "no", "chars": 2}

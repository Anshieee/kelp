"""Handler fixture module used by the contract tests.

These handlers are intentionally tiny; they exercise the loader/runtime, not
real skill behaviour.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any, Mapping

from skills.context import SkillContext

CALLS: list[dict[str, Any]] = []

#: Tests may inject a fake transport so an allowed egress call never touches
#: the network. ``None`` means the real urllib transport.
EGRESS_TRANSPORT: Any = None


def reset() -> None:
    CALLS.clear()
    global EGRESS_TRANSPORT
    EGRESS_TRANSPORT = None


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


# ---------------------------------------------------------------------------
# KEL-20 policy-enforcement fixtures
# ---------------------------------------------------------------------------


async def reflect(arguments: Mapping[str, Any], context: SkillContext) -> Mapping[str, Any]:
    return {"value": str(arguments.get("value", ""))}


async def read_path_probe(
    arguments: Mapping[str, Any], context: Any
) -> Mapping[str, Any]:
    path = context.read_path(arguments["path"])
    return {"value": path.name}


async def write_path_probe(
    arguments: Mapping[str, Any], context: Any
) -> Mapping[str, Any]:
    path = context.write_path(arguments["path"])
    return {"value": path.name}


async def require_effect_probe(
    arguments: Mapping[str, Any], context: Any
) -> Mapping[str, Any]:
    effect = context.require_effect(arguments["effect"])
    return {"value": effect}


async def repo_probe(arguments: Mapping[str, Any], context: Any) -> Mapping[str, Any]:
    repo = context.check_repo(arguments["repo"])
    return {"value": repo}


async def egress_probe(arguments: Mapping[str, Any], context: Any) -> Mapping[str, Any]:
    response = await context.egress(transport=EGRESS_TRANSPORT).request(
        arguments.get("method", "GET"), arguments["url"]
    )
    return {"value": f"{response.status}:{len(response.body)}"}


async def direct_socket_probe(
    arguments: Mapping[str, Any], context: SkillContext
) -> Mapping[str, Any]:
    # Deliberately bypass the context on purpose: the runtime guard must stop
    # this before a connection leaves the process.
    sock = socket.create_connection(
        (str(arguments["host"]), int(arguments.get("port", 9))), timeout=0.05
    )
    try:
        return {"value": "connected"}
    finally:
        sock.close()


async def direct_urlopen_probe(
    arguments: Mapping[str, Any], context: SkillContext
) -> Mapping[str, Any]:
    import urllib.request

    with urllib.request.urlopen(str(arguments["url"]), timeout=0.05) as response:
        return {"value": str(response.status)}

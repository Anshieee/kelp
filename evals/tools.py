"""Small, dependency-free helpers shared by eval scenarios.

The tool executor is deliberately not a live tool runtime: it returns recorded
or locally-computed outputs so an offline run cannot reach GitHub, Tavily, or
any other third party. ``docs.answer`` is the exception and is real, but it is
local-only by design.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from chat.service import ChatService
from chat.store import ChatStore
from kernel.provider import ToolCall
from skills import ApprovalRequest, SkillContext, SkillRegistry, load_registry
from skills.builtin.docs_answer.handler import run as docs_answer_run

__all__ = [
    "EvalToolExecutor",
    "ToolRecorder",
    "assistant_text",
    "build_tool_specs",
    "registry",
    "seed_interrupted",
    "send_and_wait",
    "wait_for_pending_approval",
    "wait_for_turn",
]


@lru_cache(maxsize=1)
def registry() -> SkillRegistry:
    return load_registry()


def build_tool_specs(a_registry: SkillRegistry | None = None) -> tuple[dict[str, Any], ...]:
    """Model-facing tool list derived from manifests, in stable order."""

    source = a_registry or registry()
    specs: list[dict[str, Any]] = []
    for skill_name in source.names():
        skill = source.get(skill_name)
        parameters = dict(skill.manifest.get("input_schema") or {})
        parameters.pop("$schema", None)
        for tool in skill.tool_names:
            specs.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool,
                        "description": skill.description,
                        "parameters": parameters,
                    },
                }
            )
    return tuple(specs)


@dataclass
class ToolRecorder:
    calls: list[dict[str, Any]] = field(default_factory=list)
    executions: list[str] = field(default_factory=list)
    outputs: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_copy(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _argument_hash(name: str, arguments: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        {"tool": name, "arguments": dict(arguments)},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


class EvalToolExecutor:
    """A deterministic tool executor that still exercises approval plumbing."""

    def __init__(
        self,
        workspace: str | Path,
        recorder: ToolRecorder,
        *,
        secrets: Mapping[str, str] | None = None,
    ):
        self._workspace = Path(workspace)
        self._recorder = recorder
        self._secrets = dict(secrets or {})
        self._hook: Any = None

    def bind(self, hook: Any) -> "EvalToolExecutor":
        self._hook = hook
        return self

    async def execute(self, call: ToolCall) -> Mapping[str, Any]:
        arguments = _copy(dict(call.arguments))
        self._recorder.calls.append(
            {"name": str(call.name), "arguments": arguments, "call_id": str(call.call_id)}
        )

        if call.name == "schedule.create":
            return await self._schedule(call, arguments)
        if call.name == "docs.answer":
            return await self._docs(arguments)
        if call.name == "web.search":
            return self._web(arguments)
        if call.name == "github.read":
            return self._github(arguments)

        self._recorder.errors.append(f"unknown tool {call.name!r}")
        return {
            "ok": False,
            "error": {
                "code": "unknown_tool",
                "message": f"No eval tool is registered for {call.name!r}.",
            },
        }

    async def _schedule(
        self, call: ToolCall, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if self._hook is None:
            self._recorder.errors.append("approval hook missing")
            return {
                "ok": False,
                "error": {"code": "approval_required", "message": "Approval is required."},
            }
        now = datetime.now(timezone.utc)
        request = ApprovalRequest(
            skill="schedule.create",
            tool="schedule.create",
            arguments=dict(arguments),
            argument_hash=_argument_hash("schedule.create", arguments),
            effects=("write",),
            scopes={
                "paths": ("${WORKSPACE}/var/scheduler/**",),
                "hosts": (),
                "repos": (),
            },
            manifest_version="1.0",
            requested_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=600)).isoformat(),
        )
        allowed = await self._hook(request)
        if not allowed:
            return {
                "ok": False,
                "error": {"code": "approval_denied", "message": "The approval was denied."},
            }
        self._recorder.executions.append("schedule.create")
        output = {
            "task_id": "task_eval_1",
            "status": "scheduled",
            "next_run_at": "2026-11-02T08:00:00Z",
        }
        self._recorder.outputs.append(dict(output))
        return {"ok": True, "output": output}

    async def _docs(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        context = SkillContext("docs.answer", self._workspace, self._secrets)
        try:
            result = await docs_answer_run(arguments, context)
        except Exception as exc:  # noqa: BLE001 - tool failures are observations
            self._recorder.errors.append(type(exc).__name__)
            return {
                "ok": False,
                "error": {
                    "code": "handler_error",
                    "message": f"docs.answer failed ({type(exc).__name__}).",
                },
            }
        output = _copy(dict(result))
        self._recorder.outputs.append(output)
        return {"ok": True, "output": output}

    def _web(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        query = str(arguments.get("query") or "")
        max_results = arguments.get("max_results", 5)
        try:
            limit = max(1, min(int(max_results), 10))
        except (TypeError, ValueError):
            limit = 5
        results = [
            {
                "title": f"Recorded result {index + 1} for {query}",
                "url": f"https://example.invalid/recorded/{index + 1}",
                "snippet": f"Offline recorded snippet about {query}.",
            }
            for index in range(limit)
        ]
        output = {"query": query, "backend": "recorded", "results": results}
        self._recorder.outputs.append(output)
        return {"ok": True, "output": output}

    def _github(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        kind = str(arguments.get("kind") or "")
        if kind == "issue":
            output = {
                "kind": "issue",
                "issue": {
                    "number": int(arguments.get("number") or 0),
                    "title": "Recorded issue",
                    "state": "open",
                    "html_url": "https://github.com/Anshieee/kelp/issues/15",
                },
            }
        elif kind == "file":
            output = {
                "kind": "file",
                "file": {
                    "path": str(arguments.get("path") or ""),
                    "ref": str(arguments.get("ref") or "main"),
                    "content": "# Recorded README\n",
                    "encoding": "utf-8",
                    "size": 18,
                },
            }
        else:
            output = {
                "kind": kind or "repo",
                "repo": {
                    "full_name": str(arguments.get("repo") or "Anshieee/kelp"),
                    "description": "Recorded repository",
                    "default_branch": "main",
                    "private": False,
                    "html_url": "https://github.com/Anshieee/kelp",
                    "open_issues_count": 1,
                },
            }
        self._recorder.outputs.append(output)
        return {"ok": True, "output": output}


async def send_and_wait(
    service: ChatService,
    thread_id: str,
    text: str,
    *,
    client_turn_id: str,
    timeout: float = 120.0,
) -> dict[str, Any]:
    payload, _created = await service.send(
        thread_id, text=text, client_turn_id=client_turn_id
    )
    await wait_for_turn(service, payload["turn_id"], timeout=timeout)
    return payload


async def wait_for_turn(
    service: ChatService, turn_id: str, *, timeout: float = 120.0
) -> None:
    await service.wait_for_turn(turn_id, timeout=timeout)


async def wait_for_pending_approval(
    store: ChatStore,
    thread_id: str,
    turn_id: str,
    *,
    timeout: float = 60.0,
) -> dict[str, Any] | None:
    """Wait for a pending card or for the turn to finish without one."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = store.list_approvals(status="pending", thread_id=thread_id, limit=10)
        if pending:
            return pending[0]
        turn = store.get_turn(turn_id)
        if turn is not None and turn["state"] == "finished":
            return None
        await asyncio.sleep(0.01)
    return None


def assistant_text(store: ChatStore, message_id: str | None) -> str:
    if not message_id:
        return ""
    message = store.get_message(message_id)
    if message is None:
        return ""
    parts = [
        str(block.get("text") or "")
        for block in message.get("blocks") or []
        if block.get("type") == "text"
    ]
    return " ".join(part for part in parts if part).strip()


def seed_interrupted(
    store: ChatStore,
    thread_id: str,
    *,
    tool: bool = False,
    approval: bool = False,
) -> dict[str, Any]:
    """Write a turn that looks exactly like a process died mid-flight."""

    turn_id = "turn_eval_dead"
    user = store.create_message(
        thread_id=thread_id,
        turn_id=turn_id,
        role="user",
        status="complete",
        blocks=[{"type": "text", "text": "Interrupted question."}],
    )
    blocks: list[dict[str, Any]] = [{"type": "text", "text": "Partial answer."}]
    if tool:
        blocks.append(
            {
                "type": "tool_call",
                "call_id": "call_eval_dead",
                "name": "schedule.create",
                "status": "running",
                "detail": {},
                "approval_id": "apr_eval_pending" if approval else None,
            }
        )
    if approval:
        blocks.append(
            {
                "type": "approval",
                "approval_id": "apr_eval_pending",
                "approval": {
                    "id": "apr_eval_pending",
                    "status": "pending",
                    "tool": "schedule.create",
                    "effects": ["write"],
                    "requested_at": "2026-10-29T09:00:00Z",
                    "expires_at": "2026-10-29T09:10:00Z",
                },
            }
        )
    assistant = store.create_message(
        thread_id=thread_id,
        turn_id=turn_id,
        role="assistant",
        status="streaming",
        blocks=blocks,
    )
    store.create_turn(
        thread_id=thread_id,
        user_message_id=user["id"],
        assistant_message_id=assistant["id"],
        client_turn_id="ct-eval-dead",
        budget={},
        turn_id=turn_id,
    )
    store.update_turn(turn_id, state="running", started_at="2026-10-29T09:00:00Z")
    if approval:
        store.save_approval(
            {
                "id": "apr_eval_pending",
                "thread_id": thread_id,
                "turn_id": turn_id,
                "call_id": "call_eval_dead",
                "source": "skill",
                "status": "pending",
                "requested_at": "2026-10-29T09:00:00Z",
                "expires_at": "2026-10-29T09:10:00Z",
                "tool": "schedule.create",
                "effects": ["write"],
                "arguments": {"title": "Interrupted digest"},
            }
        )
    return {"turn_id": turn_id, "user_message_id": user["id"], "assistant_message_id": assistant["id"]}

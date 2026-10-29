"""Task scenarios for the KEL-15 eval harness.

Each scenario drives production code, not a reimplementation of it:

* retrieval calls ``skills.builtin.docs_answer.handler.run`` directly;
* tool and multi-turn tasks drive a real :class:`chat.service.ChatService`;
* approval tasks go through the real approval card, guarded transition, and
  single-use consumption path;
* recovery tasks call the real boot-recovery helper.

Only the provider and third-party tool outputs are recorded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from chat.errors import ChatError
from chat.recovery import settle_interrupted
from chat.service import ChatService
from chat.store import ChatStore, iso_now
from openshell.audit import AuditLog
from skills import SkillContext
from skills.builtin.docs_answer.handler import run as docs_answer_run

from .results import Observation
from .tools import (
    ToolRecorder,
    assistant_text,
    seed_interrupted,
    send_and_wait,
    wait_for_pending_approval,
)

__all__ = ["ScenarioContext", "run_scenario"]


@dataclass
class ScenarioContext:
    task: Mapping[str, Any]
    workspace: Path
    store: ChatStore
    service: ChatService
    recorder: ToolRecorder
    provider: Any | None
    handle: str | None
    expected_tool_calls: list[dict[str, Any]]
    live: bool
    fixture: Mapping[str, Any] | None


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _contains(haystack: str, needles: list[str]) -> tuple[bool, str]:
    lowered = haystack.lower()
    missing = [needle for needle in needles if needle.lower() not in lowered]
    return (not missing, "missing " + ", ".join(missing) if missing else f"matched {len(needles)}")


def _add_terms(obs: Observation, label: str, text: str, terms: Any) -> None:
    if not isinstance(terms, list) or not terms:
        return
    wanted = [str(term) for term in terms]
    ok, detail = _contains(text, wanted)
    obs.add(label, ok, detail)


def _assistant_message(store: ChatStore, payload: Mapping[str, Any]) -> dict[str, Any] | None:
    return store.get_message(str(payload.get("assistant_message_id") or ""))


def _tool_blocks(message: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not message:
        return []
    return [block for block in message.get("blocks") or [] if block.get("type") == "tool_call"]


def _approval_blocks(message: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not message:
        return []
    return [block for block in message.get("blocks") or [] if block.get("type") == "approval"]


def _event_statuses(store: ChatStore, thread_id: str) -> list[str]:
    return [
        str(event.get("status"))
        for event in store.events_after(thread_id)
        if event.get("type") == "approval_resolved"
    ]


def _event_types(store: ChatStore, thread_id: str) -> list[str]:
    return [str(event.get("type")) for event in store.events_after(thread_id)]


# ---------------------------------------------------------------------------
# retrieval


async def run_retrieval(
    task: Mapping[str, Any], workspace: Path, handle: str | None
) -> Observation:
    obs = Observation()
    if not handle:
        obs.error = "retrieval task has no artifact"
        return obs
    doc = task["doc"]
    arguments = {
        "artifact": handle,
        "kind": str(doc["kind"]),
        "question": str(task["prompt"]),
        "max_rows": 20,
    }
    context = SkillContext("docs.answer", workspace)
    result = dict(await docs_answer_run(arguments, context))
    answer = str(result.get("answer") or "")
    artifact = result.get("artifact") or {}
    citations = [
        str(item.get("locator"))
        for item in result.get("citations") or []
        if isinstance(item, Mapping) and item.get("locator")
    ]
    obs.citations = citations
    expect = task.get("expect") or {}
    obs.add(
        "artifact_kind",
        str(artifact.get("kind")) == str(expect.get("artifact_kind") or doc["kind"]),
        str(artifact.get("kind")),
    )
    _add_terms(obs, "answer_terms", answer, expect.get("answer_contains"))
    expected_citations = [str(item) for item in expect.get("citation_locators") or []]
    if expected_citations:
        missing = [item for item in expected_citations if item not in citations]
        obs.add(
            "expected_citations",
            not missing,
            "missing " + ", ".join(missing) if missing else "all expected locators present",
        )
    obs.add("answer_present", bool(answer), answer[:80] if answer else "empty answer")
    return obs


# ---------------------------------------------------------------------------
# tool turns


async def run_tool_turn(ctx: ScenarioContext) -> Observation:
    obs = Observation()
    thread = ctx.service.create_thread()
    payload = await send_and_wait(
        ctx.service,
        thread["id"],
        str(ctx.task["prompt"]),
        client_turn_id="ct-eval-tool",
    )
    turn = ctx.store.get_turn(payload["turn_id"])
    message = _assistant_message(ctx.store, payload)
    text = assistant_text(ctx.store, payload.get("assistant_message_id"))

    obs.add(
        "turn_finished",
        bool(turn and turn["state"] == "finished"),
        str(turn["state"]) if turn else "missing",
    )
    obs.add(
        "turn_outcome",
        bool(turn and turn["reason"] == "stop"),
        str(turn["reason"]) if turn else "missing",
    )
    obs.add(
        "message_complete",
        bool(message and message.get("status") == "complete"),
        str(message.get("status")) if message else "missing",
    )
    expect = ctx.task.get("expect") or {}
    _add_terms(obs, "final_terms", text, expect.get("final_contains"))
    if ctx.recorder.errors:
        obs.add("tool_errors", False, ", ".join(ctx.recorder.errors[:3]))
    else:
        obs.add("tool_errors", True, "none")
    if expect.get("tool_output_contains"):
        haystack = "\n".join(_text(item) for item in ctx.recorder.outputs)
        _add_terms(obs, "tool_output_terms", haystack, expect.get("tool_output_contains"))
    return obs


# ---------------------------------------------------------------------------
# approvals


def _approval_expectation(task: Mapping[str, Any]) -> Mapping[str, Any]:
    value = (task.get("expect") or {}).get("approval")
    return value if isinstance(value, Mapping) else {}


def _check_card_arguments(
    obs: Observation, card: Mapping[str, Any], expected_calls: list[dict[str, Any]]
) -> None:
    if not expected_calls:
        return
    expected = expected_calls[0]
    arguments = card.get("arguments")
    arguments = arguments if isinstance(arguments, Mapping) else {}
    wanted = expected.get("arguments") or {}
    mismatched = [key for key, value in wanted.items() if arguments.get(key) != value]
    obs.add(
        "approval_arguments",
        not mismatched,
        "mismatched " + ", ".join(mismatched) if mismatched else f"matched {len(wanted)}",
    )


async def _settle_decision(
    ctx: ScenarioContext,
    payload: Mapping[str, Any],
    card: Mapping[str, Any],
    decision: str,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        ctx.service.decide(str(card["id"]), decision)
    except ChatError as exc:
        return None, f"decision failed: {exc.code}"
    await ctx.service.wait_for_turn(str(payload["turn_id"]), timeout=120.0)
    return ctx.store.get_approval(str(card["id"])), None


def _finish_approval_checks(
    ctx: ScenarioContext,
    obs: Observation,
    *,
    thread_id: str,
    payload: Mapping[str, Any],
    card_before: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> None:
    card = ctx.store.get_approval(str(card_before["id"])) or card_before
    message = _assistant_message(ctx.store, payload)
    statuses = _event_statuses(ctx.store, thread_id)
    expected_status = str(expected.get("status") or "")
    expected_executions = int(expected.get("executions") or 0)
    tool_name = str(card_before.get("tool") or "")

    obs.approval = {
        "requested": True,
        "tool": tool_name,
        "status": str(card.get("status") or ""),
        "executions": len(ctx.recorder.executions),
        "events": statuses,
    }
    obs.add(
        "approval_status",
        str(card.get("status")) == expected_status,
        str(card.get("status")),
    )
    obs.add(
        "approval_executions",
        len(ctx.recorder.executions) == expected_executions,
        f"{len(ctx.recorder.executions)} executed",
    )
    obs.add(
        "approval_single_use",
        int(card.get("consumed_at") is not None) == int(expected_status == "consumed"),
        "consumed_at present" if card.get("consumed_at") else "not consumed",
    )
    if expected_status == "consumed":
        obs.add("approval_events", statuses[-1:] == ["consumed"], ",".join(statuses) or "none")
    else:
        obs.add("approval_events", statuses[-1:] == [expected_status], ",".join(statuses) or "none")

    tools = _tool_blocks(message)
    approval_blocks = _approval_blocks(message)
    expected_tool_status = "done" if expected_executions else "blocked"
    obs.add(
        "tool_block_status",
        bool(tools) and all(str(block.get("status")) == expected_tool_status for block in tools),
        str(tools[0].get("status")) if tools else "missing",
    )
    approval_payload = approval_blocks[0].get("approval") if approval_blocks else None
    obs.add(
        "approval_block_status",
        isinstance(approval_payload, Mapping)
        and str(approval_payload.get("status")) == expected_status,
        str(approval_payload.get("status"))
        if isinstance(approval_payload, Mapping)
        else "missing",
    )

    effects = [str(item) for item in card_before.get("effects") or []]
    wanted_effects = [str(item) for item in expected.get("effects") or []]
    obs.add(
        "approval_effects",
        all(item in effects for item in wanted_effects),
        ",".join(effects) or "none",
    )

    sentinel = expected.get("secret_absent")
    if isinstance(sentinel, str) and sentinel:
        approval_events = [
            event
            for event in ctx.store.events_after(thread_id)
            if str(event.get("type", "")).startswith("approval")
        ]
        haystack = json.dumps(
            {
                "card": card,
                "events": approval_events,
                "blocks": message.get("blocks") if message else [],
            },
            sort_keys=True,
            default=str,
        )
        absent = sentinel not in haystack
        obs.add("secret_absent", absent, "sentinel absent" if absent else "sentinel leaked")


async def run_approval(ctx: ScenarioContext, mode: str) -> Observation:
    obs = Observation()
    expected = _approval_expectation(ctx.task)
    thread = ctx.service.create_thread()
    payload, _created = await ctx.service.send(
        thread["id"], text=str(ctx.task["prompt"]), client_turn_id=f"ct-eval-{mode}"
    )
    card = await wait_for_pending_approval(
        ctx.store, thread["id"], str(payload["turn_id"]), timeout=60.0
    )
    if card is None:
        obs.approval = {"requested": False}
        obs.add("approval_requested", False, "no pending card")
        return obs

    obs.add("approval_requested", True, "pending card present")
    obs.add("approval_source", card.get("source") == "skill", str(card.get("source")))
    obs.add("approval_tool", card.get("tool") == "schedule.create", str(card.get("tool")))
    _check_card_arguments(obs, card, ctx.expected_tool_calls)
    decision = str(expected.get("decision") or "deny")
    settled, error = await _settle_decision(ctx, payload, card, decision)
    if error:
        obs.error = error
    if settled is not None:
        _finish_approval_checks(
            ctx,
            obs,
            thread_id=thread["id"],
            payload=payload,
            card_before=card,
            expected=expected,
        )
    if mode == "approval_replay":
        code = ""
        try:
            ctx.service.decide(str(card["id"]), "approve")
        except ChatError as exc:
            code = exc.code
        expected_code = str(expected.get("replay_code") or "already_used")
        obs.add("replay_rejected", code == expected_code, code or "no error")
        if isinstance(obs.approval, dict):
            obs.approval["replay_code"] = code
    return obs


async def run_approval_expired(ctx: ScenarioContext) -> Observation:
    obs = Observation()
    expected = _approval_expectation(ctx.task)
    thread = ctx.service.create_thread()
    now = datetime.now(timezone.utc)
    card_id = "apr_eval_expired"
    ctx.store.save_approval(
        {
            "id": card_id,
            "thread_id": thread["id"],
            "turn_id": None,
            "call_id": None,
            "source": "skill",
            "status": "pending",
            "requested_at": iso_now(now - timedelta(minutes=20)),
            "expires_at": iso_now(now - timedelta(minutes=10)),
            "tool": "schedule.create",
            "effects": ["write"],
            "arguments": {"title": "Expired digest"},
        }
    )
    code = ""
    try:
        ctx.service.decide(card_id, "approve")
    except ChatError as exc:
        code = exc.code
    card = ctx.store.get_approval(card_id) or {}
    statuses = _event_statuses(ctx.store, thread["id"])
    obs.approval = {
        "requested": True,
        "tool": "schedule.create",
        "status": str(card.get("status") or ""),
        "executions": 0,
        "events": statuses,
    }
    obs.add("expired_rejected", code == "expired", code or "no error")
    obs.add(
        "approval_status",
        card.get("status") == str(expected.get("status") or "expired"),
        str(card.get("status")),
    )
    obs.add("approval_executions", not ctx.recorder.executions, "0 executed")
    obs.add("approval_events", statuses[-1:] == ["expired"], ",".join(statuses) or "none")
    return obs


# ---------------------------------------------------------------------------
# restart / resume


def _recovery_expected(task: Mapping[str, Any]) -> Mapping[str, Any]:
    value = (task.get("expect") or {}).get("recovery")
    return value if isinstance(value, Mapping) else {}


async def run_recovery_running_turn(ctx: ScenarioContext) -> Observation:
    obs = Observation()
    expected = _recovery_expected(ctx.task)
    thread = ctx.service.create_thread()
    seeded = seed_interrupted(ctx.store, thread["id"])
    audit = AuditLog(ctx.workspace / "audit.jsonl")
    report = settle_interrupted(ctx.service, ctx.store, audit)
    turn = ctx.store.get_turn(seeded["turn_id"]) or {}
    message = ctx.store.get_message(seeded["assistant_message_id"]) or {}
    events = _event_types(ctx.store, thread["id"])

    obs.add(
        "closed_turns",
        len(report.closed_turns) == int(expected.get("closed_turns") or 1),
        str(len(report.closed_turns)),
    )
    obs.add(
        "message_status",
        message.get("status") == str(expected.get("message_status") or "interrupted"),
        str(message.get("status")),
    )
    obs.add(
        "turn_reason",
        turn.get("reason") == str(expected.get("turn_reason") or "interrupted"),
        str(turn.get("reason")),
    )
    usage_persisted = (turn.get("usage") or {}).get("usage_persisted")
    obs.add(
        "usage_persisted",
        usage_persisted == expected.get("usage_persisted", False),
        str(usage_persisted),
    )
    obs.add("turn_finished_event", "turn_finished" in events, ",".join(events))
    return obs


async def run_recovery_tools_and_approval(ctx: ScenarioContext) -> Observation:
    obs = Observation()
    expected = _recovery_expected(ctx.task)
    thread = ctx.service.create_thread()
    seeded = seed_interrupted(ctx.store, thread["id"], tool=True, approval=True)
    audit = AuditLog(ctx.workspace / "audit.jsonl")
    report = settle_interrupted(ctx.service, ctx.store, audit)
    message = ctx.store.get_message(seeded["assistant_message_id"]) or {}
    tools = _tool_blocks(message)
    approvals = _approval_blocks(message)
    card = ctx.store.get_approval("apr_eval_pending") or {}
    events = _event_types(ctx.store, thread["id"])
    audit_events = [str(entry.get("event")) for entry in audit.entries()]

    obs.add(
        "failed_tool_calls",
        report.failed_tool_calls == int(expected.get("failed_tool_calls") or 1),
        str(report.failed_tool_calls),
    )
    obs.add(
        "expired_approvals",
        len(report.expired_approvals) == int(expected.get("expired_approvals") or 1),
        str(len(report.expired_approvals)),
    )
    tool_status = str(tools[0].get("status")) if tools else ""
    tool_error = ""
    if tools:
        detail = tools[0].get("detail") or {}
        error = detail.get("error") if isinstance(detail, Mapping) else None
        tool_error = str(error.get("code")) if isinstance(error, Mapping) else ""
    obs.add(
        "tool_status",
        tool_status == str(expected.get("tool_status") or "failed"),
        tool_status or "missing",
    )
    obs.add(
        "tool_error",
        tool_error == str(expected.get("tool_error") or "interrupted"),
        tool_error or "missing",
    )
    obs.add(
        "approval_status",
        card.get("status") == str(expected.get("approval_status") or "expired"),
        str(card.get("status")),
    )
    approval_payload = approvals[0].get("approval") if approvals else None
    obs.add(
        "approval_block_status",
        isinstance(approval_payload, Mapping)
        and approval_payload.get("status") == str(expected.get("approval_status") or "expired"),
        str(approval_payload.get("status"))
        if isinstance(approval_payload, Mapping)
        else "missing",
    )
    for event_name in expected.get("events") or []:
        obs.add(f"event_{event_name}", str(event_name) in events, ",".join(events))
    obs.add("audit_turn_interrupted", "turn_interrupted" in audit_events, ",".join(audit_events))
    obs.add("audit_approval_expired", "approval_expired" in audit_events, ",".join(audit_events))
    obs.approval = {
        "requested": True,
        "status": str(card.get("status") or ""),
        "executions": 0,
        "recovery": report.to_dict(),
    }
    return obs


async def run_recovery_idempotent(ctx: ScenarioContext) -> Observation:
    obs = Observation()
    expected = _recovery_expected(ctx.task)
    thread = ctx.service.create_thread()
    seed_interrupted(ctx.store, thread["id"], tool=True, approval=True)
    audit = AuditLog(ctx.workspace / "audit.jsonl")
    first = settle_interrupted(ctx.service, ctx.store, audit)
    second = settle_interrupted(ctx.service, ctx.store, audit)
    events = _event_types(ctx.store, thread["id"])

    obs.add("first_closed_turns", len(first.closed_turns) == 1, str(len(first.closed_turns)))
    obs.add("first_failed_tools", first.failed_tool_calls == 1, str(first.failed_tool_calls))
    obs.add("first_expired", len(first.expired_approvals) == 1, str(len(first.expired_approvals)))
    obs.add(
        "second_closed_turns",
        len(second.closed_turns) == int(expected.get("second_closed_turns") or 0),
        str(len(second.closed_turns)),
    )
    obs.add(
        "second_failed_tools",
        second.failed_tool_calls == int(expected.get("second_failed_tool_calls") or 0),
        str(second.failed_tool_calls),
    )
    obs.add(
        "second_expired",
        len(second.expired_approvals) == int(expected.get("second_expired_approvals") or 0),
        str(len(second.expired_approvals)),
    )
    obs.add(
        "single_turn_finished",
        events.count("turn_finished") == (1 if expected.get("single_turn_finished", True) else 2),
        str(events.count("turn_finished")),
    )
    obs.add(
        "single_approval_resolved",
        events.count("approval_resolved")
        == (1 if expected.get("single_approval_resolved", True) else 2),
        str(events.count("approval_resolved")),
    )
    return obs


async def run_resume_retry(ctx: ScenarioContext) -> Observation:
    obs = Observation()
    expected = ctx.task.get("expect") or {}
    retry_expected = expected.get("retry") if isinstance(expected.get("retry"), Mapping) else {}
    thread = ctx.service.create_thread()
    payload = await send_and_wait(
        ctx.service,
        thread["id"],
        str(ctx.task["prompt"]),
        client_turn_id="ct-eval-resume",
    )
    failed = ctx.store.get_turn(payload["turn_id"]) or {}
    obs.add("first_turn_error", failed.get("reason") == "error", str(failed.get("reason")))

    before = ctx.store.list_messages(thread["id"])[0]
    retried = await ctx.service.retry(payload["turn_id"])
    await ctx.service.wait_for_turn(str(retried["turn_id"]), timeout=120.0)
    after = ctx.store.list_messages(thread["id"])[0]
    old = ctx.store.get_message(str(payload["assistant_message_id"])) or {}
    retried_turn = ctx.store.get_turn(str(retried["turn_id"])) or {}
    new_text = assistant_text(
        ctx.store, str(retried_turn.get("assistant_message_id") or "")
    )
    requests = len(getattr(ctx.provider, "requests", []) or [])

    obs.add(
        "same_user_message",
        bool(retry_expected.get("same_user_message", True))
        and len([m for m in after if m["role"] == "user"]) == 1,
        f"{len([m for m in after if m['role'] == 'user'])} user message(s)",
    )
    obs.add(
        "superseded",
        old.get("superseded_by") == retried["turn_id"],
        str(old.get("superseded_by")),
    )
    obs.add("message_count", len(after) == len(before) + 1, str(len(after)))
    obs.add(
        "provider_requests",
        requests == int(retry_expected.get("provider_requests") or 2),
        str(requests),
    )
    _add_terms(obs, "retry_answer_terms", new_text, expected.get("assistant_contains"))
    return obs


# ---------------------------------------------------------------------------
# multi-turn


def _check_thread(
    obs: Observation,
    label: str,
    messages: list[Mapping[str, Any]],
    expected_count: int,
) -> None:
    obs.add(label, len(messages) == expected_count, str(len(messages)))


async def run_multi_turn_two(ctx: ScenarioContext) -> Observation:
    obs = Observation()
    expect = ctx.task.get("expect") or {}
    thread = ctx.service.create_thread()
    first = await send_and_wait(
        ctx.service,
        thread["id"],
        str(ctx.task["prompt"]),
        client_turn_id="ct-eval-multi-1",
    )
    first_text = assistant_text(ctx.store, first.get("assistant_message_id"))
    _add_terms(obs, "first_answer_terms", first_text, expect.get("assistant_contains"))
    title_before = (ctx.store.get_thread(thread["id"]) or {}).get("title")

    second = await send_and_wait(
        ctx.service,
        thread["id"],
        str(ctx.task.get("followup") or ""),
        client_turn_id="ct-eval-multi-2",
    )
    second_text = assistant_text(ctx.store, second.get("assistant_message_id"))
    messages = ctx.store.list_messages(thread["id"])[0]
    _check_thread(obs, "message_count", messages, int(expect.get("message_count") or 4))
    _add_terms(obs, "followup_terms", second_text, expect.get("followup_contains"))
    users = [m for m in messages if m["role"] == "user"]
    obs.add(
        "both_user_messages",
        len(users) == 2
        and str(ctx.task["prompt"]) in json.dumps(users[0], default=str)
        and str(ctx.task.get("followup")) in json.dumps(users[1], default=str),
        f"{len(users)} user message(s)",
    )
    obs.add(
        "first_answer_retained",
        bool(first_text) and first_text in json.dumps(messages, default=str),
        first_text or "missing",
    )
    thread_after = ctx.store.get_thread(thread["id"]) or {}
    obs.add("turn_idle", not thread_after.get("active_turn_id"), str(thread_after.get("active_turn_id")))
    obs.add("title_stable", thread_after.get("title") == title_before, str(thread_after.get("title")))
    requests = len(getattr(ctx.provider, "requests", []) or [])
    obs.add(
        "provider_requests",
        requests == int(expect.get("provider_requests") or 2),
        str(requests),
    )
    return obs


async def run_multi_turn_idempotent(ctx: ScenarioContext) -> Observation:
    obs = Observation()
    expect = ctx.task.get("expect") or {}
    thread = ctx.service.create_thread()
    payload, created = await ctx.service.send(
        thread["id"], text=str(ctx.task["prompt"]), client_turn_id="ct-eval-idem"
    )
    await ctx.service.wait_for_turn(str(payload["turn_id"]), timeout=120.0)
    second, created_again = await ctx.service.send(
        thread["id"], text=str(ctx.task["prompt"]), client_turn_id="ct-eval-idem"
    )
    text = assistant_text(ctx.store, payload.get("assistant_message_id"))
    messages = ctx.store.list_messages(thread["id"])[0]
    requests = len(getattr(ctx.provider, "requests", []) or [])

    obs.add("created_first", bool(created), str(created))
    idempotent = bool(expect.get("idempotent", True))
    obs.add(
        "created_again",
        (created_again is False) if idempotent else (created_again is True),
        str(created_again),
    )
    obs.add("same_turn", second["turn_id"] == payload["turn_id"], str(second["turn_id"]))
    _check_thread(obs, "message_count", messages, int(expect.get("message_count") or 2))
    obs.add(
        "provider_requests",
        requests == int(expect.get("provider_requests") or 1),
        str(requests),
    )
    _add_terms(obs, "answer_terms", text, expect.get("assistant_contains"))
    return obs


async def run_multi_turn_isolation(ctx: ScenarioContext) -> Observation:
    obs = Observation()
    expect = ctx.task.get("expect") or {}
    first_thread = ctx.service.create_thread()
    second_thread = ctx.service.create_thread()
    first = await send_and_wait(
        ctx.service,
        first_thread["id"],
        str(ctx.task["prompt"]),
        client_turn_id="ct-eval-iso-1",
    )
    second = await send_and_wait(
        ctx.service,
        second_thread["id"],
        str(ctx.task.get("followup") or ""),
        client_turn_id="ct-eval-iso-2",
    )
    first_text = assistant_text(ctx.store, first.get("assistant_message_id"))
    second_text = assistant_text(ctx.store, second.get("assistant_message_id"))
    first_messages = ctx.store.list_messages(first_thread["id"])[0]
    second_messages = ctx.store.list_messages(second_thread["id"])[0]
    expected_count = int(expect.get("message_count") or 2)
    _check_thread(obs, "first_thread_count", first_messages, expected_count)
    _check_thread(obs, "second_thread_count", second_messages, expected_count)
    followup = str(ctx.task.get("followup"))
    prompt = str(ctx.task["prompt"])
    first_dump = json.dumps(first_messages, default=str)
    second_dump = json.dumps(second_messages, default=str)
    obs.add("first_thread_isolated", followup not in first_dump, "isolated" if followup not in first_dump else "leaked")
    obs.add(
        "second_thread_isolated",
        prompt not in second_dump,
        "isolated" if prompt not in second_dump else "leaked",
    )
    _add_terms(obs, "first_answer_terms", first_text, expect.get("assistant_contains"))
    _add_terms(obs, "second_answer_terms", second_text, expect.get("followup_contains"))
    requests = len(getattr(ctx.provider, "requests", []) or [])
    obs.add(
        "provider_requests",
        requests == int(expect.get("provider_requests") or 2),
        str(requests),
    )
    return obs


async def run_multi_turn_title(ctx: ScenarioContext) -> Observation:
    obs = Observation()
    expect = ctx.task.get("expect") or {}
    thread = ctx.service.create_thread()
    first = await send_and_wait(
        ctx.service,
        thread["id"],
        str(ctx.task["prompt"]),
        client_turn_id="ct-eval-title-1",
    )
    title_before = (ctx.store.get_thread(thread["id"]) or {}).get("title")
    source_before = (ctx.store.get_thread(thread["id"]) or {}).get("title_source")
    second = await send_and_wait(
        ctx.service,
        thread["id"],
        str(ctx.task.get("followup") or ""),
        client_turn_id="ct-eval-title-2",
    )
    thread_after = ctx.store.get_thread(thread["id"]) or {}
    messages = ctx.store.list_messages(thread["id"])[0]
    _check_thread(obs, "message_count", messages, int(expect.get("message_count") or 4))
    obs.add("title_auto", source_before == "auto", str(source_before))
    obs.add(
        "title_stable",
        thread_after.get("title") == title_before,
        f"{title_before!r} -> {thread_after.get('title')!r}",
    )
    _add_terms(
        obs,
        "first_answer_terms",
        assistant_text(ctx.store, first.get("assistant_message_id")),
        expect.get("assistant_contains"),
    )
    _add_terms(
        obs,
        "followup_terms",
        assistant_text(ctx.store, second.get("assistant_message_id")),
        expect.get("followup_contains"),
    )
    requests = len(getattr(ctx.provider, "requests", []) or [])
    obs.add(
        "provider_requests",
        requests == int(expect.get("provider_requests") or 2),
        str(requests),
    )
    return obs


# ---------------------------------------------------------------------------
# dispatch


async def run_scenario(name: str, ctx: ScenarioContext) -> Observation:
    handlers = {
        "tool_turn": run_tool_turn,
        "approval_approve": lambda ctx: run_approval(ctx, "approval_approve"),
        "approval_deny": lambda ctx: run_approval(ctx, "approval_deny"),
        "approval_redaction": lambda ctx: run_approval(ctx, "approval_redaction"),
        "approval_replay": lambda ctx: run_approval(ctx, "approval_replay"),
        "approval_expired": run_approval_expired,
        "recovery_running_turn": run_recovery_running_turn,
        "recovery_running_tools_and_approval": run_recovery_tools_and_approval,
        "recovery_idempotent": run_recovery_idempotent,
        "resume_retry": run_resume_retry,
        "multi_turn_two": run_multi_turn_two,
        "multi_turn_idempotent": run_multi_turn_idempotent,
        "multi_turn_isolation": run_multi_turn_isolation,
        "multi_turn_title": run_multi_turn_title,
    }
    handler = handlers.get(name)
    if handler is None:
        raise ValueError(f"unknown scenario {name!r}")
    return await handler(ctx)

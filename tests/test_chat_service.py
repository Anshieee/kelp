"""Turn orchestration: cards, budgets, approvals, stop/retry, recovery."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from chat import ArtifactError, ChatService, ChatStore
from chat.errors import ChatError as ChatErrorType
from kernel import CallableToolExecutor, TurnBudget
from kernel.provider import ProviderFailure, StreamEnd, TextDelta, ToolCallReady, UsageReport
from skills.context import ApprovalRequest

from .chatkit import (
    StubProvider,
    executor_factory,
    make_service,
    send_and_wait,
    text_script,
    tool_script,
    wait_until,
)


def event_types(store: object, thread_id: str) -> list[str]:
    return [event["type"] for event in store.events_after(thread_id)]  # type: ignore[attr-defined]


def test_text_turn_streams_in_order_and_carries_kernel_limits(tmp_path: Path) -> None:
    service, store, _provider = make_service(
        tmp_path, [text_script("Hello there")], budget=TurnBudget(max_steps=3)
    )
    try:
        thread = service.create_thread()

        async def scenario() -> None:
            payload = await send_and_wait(service, thread["id"], "hi")
            events = store.events_after(thread["id"])
            assert [event["type"] for event in events] == [
                "message_created",
                "message_created",
                "thread_updated",
                "turn_started",
                "token_delta",
                "usage_updated",
                "turn_finished",
            ]
            started = next(event for event in events if event["type"] == "turn_started")
            assert started["limits"]["max_steps"] == 3
            finished = events[-1]
            assert finished["reason"] == "stop"
            assert finished["message_status"] == "complete"
            assistant = store.get_message(payload["assistant_message_id"])
            assert assistant["status"] == "complete"
            assert assistant["blocks"] == [{"type": "text", "text": "Hello there"}]
            user = store.get_message(payload["user_message_id"])
            assert user["status"] == "complete"

        asyncio.run(scenario())
    finally:
        store.close()


def test_tool_call_opens_a_card_and_text_continues_after_it(tmp_path: Path) -> None:
    async def executor(call: object) -> dict:
        return {
            "ok": True,
            "skill": "web.search",
            "tool": "web.search",
            "duration_ms": 1,
            "output": {
                "query": "Nebius GPU pricing",
                "backend": "stub",
                "results": [
                    {"title": "A", "url": "https://a.example/1", "snippet": "one"},
                    {"title": "B", "url": "https://b.example/2", "snippet": "two"},
                ],
            },
        }

    scripts = [
        tool_script("web.search", {"query": "Nebius GPU pricing"}),
        text_script("Found two."),
    ]
    service, store, _provider = make_service(
        tmp_path, scripts, tool_executor_factory=executor_factory(executor)
    )
    try:
        thread = service.create_thread()

        async def scenario() -> None:
            payload = await send_and_wait(service, thread["id"], "search")
            blocks = store.get_message(payload["assistant_message_id"])["blocks"]
            assert [block["type"] for block in blocks] == ["tool_call", "text"]
            card = blocks[0]
            assert card["tool"] == "web.search"
            assert card["label"] == "Web search"
            assert card["summary"] == "Nebius GPU pricing"
            assert card["status"] == "done"
            assert card["detail"]["result_summary"] == "2 results"
            assert blocks[1]["text"] == "Found two."

            sources = store.get_message(payload["assistant_message_id"])["sources"]
            assert [source["url"] for source in sources] == [
                "https://a.example/1",
                "https://b.example/2",
            ]
            assert "sources_ready" in event_types(store, thread["id"])

        asyncio.run(scenario())
    finally:
        store.close()


def test_budget_comes_from_the_kernel_loop(tmp_path: Path) -> None:
    async def executor(call: object) -> dict:
        return {"ok": True, "output": {}}

    scripts = [tool_script("stub.tool", {"n": 1}), text_script("never")]
    service, store, _provider = make_service(
        tmp_path,
        scripts,
        budget=TurnBudget(max_steps=1, max_tool_calls=1),
        tool_executor_factory=executor_factory(executor),
    )
    try:
        thread = service.create_thread()

        async def scenario() -> None:
            payload = await send_and_wait(service, thread["id"], "go")
            events = store.events_after(thread["id"])
            error = next(event for event in events if event["type"] == "error")
            assert error["code"] == "turn_budget_exceeded"
            assert error["details"] == {"limit": "max_steps"}
            assert error["message"] == "That answer got too long."
            finished = events[-1]
            assert finished["reason"] == "budget"
            assert finished["message_status"] == "error"
            assert store.get_message(payload["assistant_message_id"])["status"] == "error"

        asyncio.run(scenario())
    finally:
        store.close()


def _schedule_request(call: object) -> ApprovalRequest:
    now = datetime.now(timezone.utc)
    arguments = dict(call.arguments)  # type: ignore[attr-defined]
    return ApprovalRequest(
        skill="schedule.create",
        tool="schedule.create",
        arguments=arguments,
        argument_hash="sha256:abc",
        effects=("write",),
        scopes={"paths": ("${WORKSPACE}/var/scheduler/**",), "hosts": (), "repos": ()},
        manifest_version="1.0",
        requested_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=600)).isoformat(),
    )


def test_approval_blocks_the_turn_then_runs_once(tmp_path: Path) -> None:
    executions: list[str] = []

    def factory(hook: object) -> CallableToolExecutor:
        async def executor(call: object) -> dict:
            allowed = await hook(_schedule_request(call))  # type: ignore[operator]
            if not allowed:
                return {"ok": False, "error": {"code": "approval_denied"}}
            executions.append(call.call_id)  # type: ignore[attr-defined]
            return {
                "ok": True,
                "output": {"task_id": "task_1", "status": "scheduled", "next_run_at": "soon"},
            }

        return CallableToolExecutor(executor)

    scripts = [
        tool_script(
            "schedule.create",
            {
                "title": "Weekday summary",
                "prompt": "summarize",
                "schedule": {"kind": "cron", "expr": "0 8 * * 1-5"},
                "timezone": "Europe/Berlin",
            },
        ),
        text_script("Scheduled."),
    ]
    service, store, _provider = make_service(tmp_path, scripts, tool_executor_factory=factory)
    try:
        thread = service.create_thread()

        async def scenario() -> None:
            payload, _created = await service.send(
                thread["id"], text="schedule it", client_turn_id="ct-approval"
            )
            pending = await wait_until(lambda: store.list_approvals(status="pending"))
            card = pending[0]
            assert card["source"] == "skill"
            assert card["status"] == "pending"
            assert card["effects"] == ["write"]
            assert "change something in your workspace" in card["rationale"]
            assert executions == []

            result = service.decide(card["id"], "approve")
            assert result["executed"] is False
            await service.wait_for_turn(payload["turn_id"])

            assert executions == ["call_1"]
            settled = store.get_approval(card["id"])
            assert settled["status"] == "consumed"
            assert settled["consumed_at"] is not None
            blocks = store.get_message(payload["assistant_message_id"])["blocks"]
            tool = next(block for block in blocks if block["type"] == "tool_call")
            approval = next(block for block in blocks if block["type"] == "approval")
            assert tool["status"] == "done"
            assert tool["approval_id"] == card["id"]
            assert approval["approval"]["status"] == "consumed"

            resolved = [
                event
                for event in store.events_after(thread["id"])
                if event["type"] == "approval_resolved"
            ]
            assert [event["status"] for event in resolved] == ["approved", "consumed"]
            assert resolved[-1]["executed"] is True

        asyncio.run(scenario())
    finally:
        store.close()


def test_denial_blocks_the_card_and_never_runs_the_handler(tmp_path: Path) -> None:
    executions: list[str] = []

    def factory(hook: object) -> CallableToolExecutor:
        async def executor(call: object) -> dict:
            allowed = await hook(_schedule_request(call))  # type: ignore[operator]
            executions.append(str(call.call_id))  # type: ignore[attr-defined]
            if not allowed:
                return {"ok": False, "error": {"code": "approval_denied"}}
            return {"ok": True, "output": {}}

        return CallableToolExecutor(executor)

    scripts = [tool_script("schedule.create", {"title": "x", "prompt": "y",
                                               "schedule": {"kind": "cron", "expr": "* * * * *"},
                                               "timezone": "UTC"}), text_script("Understood.")]
    service, store, _provider = make_service(tmp_path, scripts, tool_executor_factory=factory)
    try:
        thread = service.create_thread()

        async def scenario() -> None:
            payload, _created = await service.send(
                thread["id"], text="schedule", client_turn_id="ct-deny"
            )
            pending = await wait_until(lambda: store.list_approvals(status="pending"))
            service.decide(pending[0]["id"], "deny", "not now")
            await service.wait_for_turn(payload["turn_id"])

            assert store.get_approval(pending[0]["id"])["status"] == "denied"
            blocks = store.get_message(payload["assistant_message_id"])["blocks"]
            tool = next(block for block in blocks if block["type"] == "tool_call")
            assert tool["status"] == "blocked"
            assert executions == ["call_1"]  # the hook resumed, the handler did not run

        asyncio.run(scenario())
    finally:
        store.close()


def test_approval_decisions_are_idempotent_and_conflicts_are_rejected(
    tmp_path: Path,
) -> None:
    service, store, _provider = make_service(tmp_path, [text_script("ok")])
    try:
        thread = service.create_thread()
        card = {
            "id": "apr_manual",
            "thread_id": thread["id"],
            "turn_id": None,
            "call_id": None,
            "source": "skill",
            "status": "pending",
            "requested_at": "2026-10-29T09:00:00Z",
            "expires_at": "2099-01-01T00:00:00Z",
            "effects": ["write"],
        }
        store.save_approval(card)

        first = service.decide("apr_manual", "approve")
        assert first["approval"]["status"] == "approved"
        again = service.decide("apr_manual", "approve")
        assert again["idempotent"] is True
        with pytest.raises(ChatErrorType) as conflict:
            service.decide("apr_manual", "deny")
        assert conflict.value.status == 409
        assert conflict.value.code == "not_approved"
        assert store.get_approval("apr_manual")["status"] == "approved"

        unknown = service.decide
        with pytest.raises(ChatErrorType) as missing:
            unknown("apr_missing", "approve")
        assert missing.value.status == 404
    finally:
        store.close()


def test_expired_approval_cannot_be_decided(tmp_path: Path) -> None:
    service, store, _provider = make_service(tmp_path, [text_script("ok")])
    try:
        thread = service.create_thread()
        store.save_approval(
            {
                "id": "apr_old",
                "thread_id": thread["id"],
                "turn_id": None,
                "call_id": None,
                "source": "skill",
                "status": "pending",
                "requested_at": "2020-01-01T00:00:00Z",
                "expires_at": "2020-01-01T00:10:00Z",
                "effects": ["write"],
            }
        )
        with pytest.raises(ChatErrorType) as expired:
            service.decide("apr_old", "approve")
        assert expired.value.status == 409
        assert expired.value.code == "expired"
        assert store.get_approval("apr_old")["status"] == "expired"
    finally:
        store.close()


class _SlowProvider:
    async def stream(self, request: object) -> object:
        yield TextDelta("partial ")
        await asyncio.sleep(10)
        yield TextDelta("never")
        yield StreamEnd("stop")


def test_stop_preserves_partial_text_and_closes_the_turn(tmp_path: Path) -> None:
    store = ChatStore(tmp_path / "chat.db")
    service = ChatService(
        store=store,
        provider=_SlowProvider(),
        model="nvidia/Nemotron-3_5-Super-VL",
    )
    try:
        thread = service.create_thread()

        async def scenario() -> None:
            payload, created = await service.send(
                thread["id"], text="go", client_turn_id="ct-stop"
            )
            assert created is True
            await asyncio.sleep(0.02)
            result = await service.stop(payload["turn_id"])
            assert result["status"] == "stopping"
            await service.wait_for_turn(payload["turn_id"], timeout=3)

            assistant = store.get_message(payload["assistant_message_id"])
            assert assistant["status"] == "stopped"
            assert assistant["blocks"] == [{"type": "text", "text": "partial "}]
            turn = store.get_turn(payload["turn_id"])
            assert turn["state"] == "finished"
            assert turn["reason"] == "cancelled"
            finished = [
                event
                for event in store.events_after(thread["id"])
                if event["type"] == "turn_finished"
            ]
            assert finished[-1]["message_status"] == "stopped"
            assert finished[-1]["reason"] == "cancelled"

            with pytest.raises(ChatErrorType) as not_running:
                await service.stop(payload["turn_id"])
            assert not_running.value.status == 409

        asyncio.run(scenario())
    finally:
        store.close()


def test_retry_reuses_the_user_message_without_duplicating_it(tmp_path: Path) -> None:
    failing = [
        TextDelta("par"),
        ProviderFailure("provider_timeout", "timed out", retryable=True),
    ]
    scripts = [failing, text_script("recovered")]
    service, store, _provider = make_service(tmp_path, scripts)
    try:
        thread = service.create_thread()

        async def scenario() -> None:
            payload = await send_and_wait(service, thread["id"], "question")
            assert store.get_turn(payload["turn_id"])["reason"] == "error"
            before = len(store.list_messages(thread["id"])[0])

            retried = await service.retry(payload["turn_id"])
            await service.wait_for_turn(retried["turn_id"])
            assert retried["retry_of"] == payload["turn_id"]
            messages = store.list_messages(thread["id"])[0]
            assert len(messages) == before + 1  # only a new assistant message
            assert len([message for message in messages if message["role"] == "user"]) == 1
            old = store.get_message(payload["assistant_message_id"])
            assert old["superseded_by"] == retried["turn_id"]
            assert store.get_turn(retried["turn_id"])["reason"] == "stop"

        asyncio.run(scenario())
    finally:
        store.close()


def test_retry_rejects_a_non_latest_or_unfinished_turn(tmp_path: Path) -> None:
    service, store, _provider = make_service(tmp_path, [text_script("one"), text_script("two")])
    try:
        thread = service.create_thread()

        async def scenario() -> None:
            first = await send_and_wait(service, thread["id"], "one", client_turn_id="ct-1")
            second = await send_and_wait(service, thread["id"], "two", client_turn_id="ct-2")
            with pytest.raises(ChatErrorType) as not_latest:
                await service.retry(first["turn_id"])
            assert not_latest.value.status == 409
            assert not_latest.value.code == "not_latest_turn"
            with pytest.raises(ChatErrorType) as not_retryable:
                await service.retry(second["turn_id"])
            assert not_retryable.value.code == "turn_not_retryable"

        asyncio.run(scenario())
    finally:
        store.close()


def test_boot_recovery_closes_a_running_turn_as_interrupted(tmp_path: Path) -> None:
    service, store, _provider = make_service(tmp_path, [text_script("never")])
    try:
        thread = service.create_thread()
        user = store.create_message(
            thread_id=thread["id"],
            turn_id="turn_dead",
            role="user",
            status="complete",
            blocks=[{"type": "text", "text": "hi"}],
        )
        assistant = store.create_message(
            thread_id=thread["id"],
            turn_id="turn_dead",
            role="assistant",
            status="streaming",
            blocks=[{"type": "text", "text": "partial"}],
        )
        store.create_turn(
            thread_id=thread["id"],
            user_message_id=user["id"],
            assistant_message_id=assistant["id"],
            client_turn_id="ct-dead",
            budget={},
            turn_id="turn_dead",
        )
        store.update_turn("turn_dead", state="running", started_at="2026-10-29T09:00:00Z")

        closed = service.recover_interrupted()
        assert closed == ["turn_dead"]
        assert store.get_message(assistant["id"])["status"] == "interrupted"
        finished = [
            event
            for event in store.events_after(thread["id"])
            if event["type"] == "turn_finished"
        ][-1]
        assert finished["reason"] == "interrupted"
        assert finished["usage_persisted"] is False
        assert store.get_turn("turn_dead")["reason"] == "interrupted"
    finally:
        store.close()


def test_docs_answer_emits_an_artifact_card(tmp_path: Path) -> None:
    service, store, _provider = make_service(tmp_path, [text_script("unused")])
    try:
        thread = service.create_thread()
        meta = service.artifacts.save_upload(
            "q3-report.pdf", b"%PDF-1.4\n1 0 obj<</Type /Page>>endobj\n%%EOF"
        )
        handle = meta["handle"]

        async def executor(call: object) -> dict:
            return {
                "ok": True,
                "output": {
                    "answer": "Revenue was $4.2M.",
                    "artifact": {
                        "handle": handle,
                        "kind": "pdf",
                        "mime": "application/pdf",
                    },
                    "citations": [{"locator": "p.4", "text": "Revenue was $4.2M"}],
                },
            }

        service._tool_executor_factory = executor_factory(executor)  # noqa: SLF001
        service._provider = StubProvider(  # noqa: SLF001
            [
                tool_script(
                    "docs.answer",
                    {"artifact": handle, "kind": "pdf", "question": "rev"},
                ),
                text_script("Here is the answer."),
            ]
        )

        async def scenario() -> None:
            payload = await send_and_wait(service, thread["id"], "summarize")
            blocks = store.get_message(payload["assistant_message_id"])["blocks"]
            artifact = next(block for block in blocks if block["type"] == "artifact")
            assert artifact["artifact"]["handle"] == handle
            assert artifact["artifact"]["preview_url"].endswith("#page=1&toolbar=0")
            sources = store.get_message(payload["assistant_message_id"])["sources"]
            assert sources[0]["kind"] == "doc"
            assert sources[0]["locator"] == "p.4"
            ready = [
                event
                for event in store.events_after(thread["id"])
                if event["type"] == "artifact_ready"
            ]
            assert ready[0]["artifact"]["handle"] == handle

        asyncio.run(scenario())
    finally:
        store.close()


def test_unknown_artifact_handle_emits_an_error_and_no_card(tmp_path: Path) -> None:
    async def executor(call: object) -> dict:
        return {
            "ok": True,
            "output": {
                "answer": "text",
                "artifact": {"handle": "artifact:doesnotexist", "kind": "pdf", "mime": "application/pdf"},
                "citations": [],
            },
        }

    service, store, _provider = make_service(
        tmp_path,
        [tool_script("docs.answer", {"artifact": "artifact:doesnotexist", "kind": "pdf", "question": "q"}), text_script("done")],
        tool_executor_factory=executor_factory(executor),
    )
    try:
        thread = service.create_thread()

        async def scenario() -> None:
            payload = await send_and_wait(service, thread["id"], "q")
            blocks = store.get_message(payload["assistant_message_id"])["blocks"]
            assert not any(block["type"] == "artifact" for block in blocks)
            errors = [
                event
                for event in store.events_after(thread["id"])
                if event["type"] == "error"
            ]
            assert errors[0]["code"] == "artifact_not_found"
            assert errors[0]["message"] == "That file isn't available any more."
            assert store.get_turn(payload["turn_id"])["reason"] == "stop"

        asyncio.run(scenario())
    finally:
        store.close()


def test_github_read_run_becomes_one_sweep_card(tmp_path: Path) -> None:
    def issue(number: int) -> dict:
        return {
            "ok": True,
            "output": {
                "kind": "issue",
                "issue": {
                    "number": number,
                    "title": f"Issue {number}",
                    "state": "open",
                    "html_url": f"https://github.com/a/b/issues/{number}",
                },
            },
        }

    calls = {"n": 0}

    async def executor(call: object) -> dict:
        calls["n"] += 1
        return issue(calls["n"])

    scripts = [
        [
            ToolCallReady(0, "call_a", "github.read", {"kind": "issue", "repo": "a/b", "number": 1}),
            UsageReport(1, 1, 2),
            StreamEnd("tool_calls"),
        ],
        [
            ToolCallReady(0, "call_b", "github.read", {"kind": "issue", "repo": "a/b", "number": 2}),
            UsageReport(1, 1, 2),
            StreamEnd("tool_calls"),
        ],
        text_script("Two issues."),
    ]
    service, store, _provider = make_service(
        tmp_path, scripts, tool_executor_factory=executor_factory(executor)
    )
    try:
        thread = service.create_thread()

        async def scenario() -> None:
            payload = await send_and_wait(service, thread["id"], "check issues")
            blocks = store.get_message(payload["assistant_message_id"])["blocks"]
            anchor = blocks[0]
            assert anchor["tool"] == "github.read"
            assert anchor["detail"]["sweep"]["count"] == 2
            assert [item["number"] for item in anchor["detail"]["sweep"]["items"]] == [1, 2]
            assert blocks[1]["detail"]["sweep_anchor"] == "call_a"
            sources = store.get_message(payload["assistant_message_id"])["sources"]
            assert len(sources) == 2

        asyncio.run(scenario())
    finally:
        store.close()


def test_secrets_are_redacted_and_never_persisted(tmp_path: Path) -> None:
    secret = "super-secret-value-9876"

    async def executor(call: object) -> dict:
        return {"ok": True, "output": {}}

    service, store, _provider = make_service(
        tmp_path,
        [tool_script("web.search", {"query": f"q {secret}", "api_key": secret}), text_script("ok")],
        secrets={"TAVILY_API_KEY": secret},
        tool_executor_factory=executor_factory(executor),
    )
    try:
        thread = service.create_thread()

        async def scenario() -> None:
            payload = await send_and_wait(service, thread["id"], "search")
            blocks = store.get_message(payload["assistant_message_id"])["blocks"]
            arguments = blocks[0]["detail"]["arguments"]
            assert arguments["api_key"] == "[redacted]"
            assert secret not in json.dumps(arguments)
            assert secret not in json.dumps(store.events_after(thread["id"]))

        asyncio.run(scenario())
    finally:
        path = store.path
        store.close()
        blobs = [path.read_bytes()]
        wal = path.with_name(path.name + "-wal")
        if wal.exists():
            blobs.append(wal.read_bytes())
        assert all(secret.encode() not in blob for blob in blobs)


def test_attachment_is_claimed_once_and_rendered_first(tmp_path: Path) -> None:
    service, store, _provider = make_service(tmp_path, [text_script("ok"), text_script("ok")])
    try:
        thread = service.create_thread()
        other = service.create_thread()
        meta = service.artifacts.save_upload("rows.csv", b"region,revenue\nEMEA,42\nAPAC,29\n")

        async def scenario() -> None:
            user_payload, _created = await service.send(
                thread["id"],
                text="summarize",
                client_turn_id="ct-file-2",
                attachment_handles=[meta["handle"]],
            )
            await service.wait_for_turn(user_payload["turn_id"])
            user = store.get_message(user_payload["user_message_id"])
            assert [block["type"] for block in user["blocks"]] == ["attachment", "text"]
            assert user["blocks"][0]["attachment"]["handle"] == meta["handle"]
            assert service.artifacts.get(meta["handle"])["thread_id"] == thread["id"]

            with pytest.raises(ChatErrorType) as other_thread:
                await service.send(
                    other["id"],
                    text="steal",
                    client_turn_id="ct-steal",
                    attachment_handles=[meta["handle"]],
                )
            assert other_thread.value.status == 400
            assert other_thread.value.code == "invalid_message"

        asyncio.run(scenario())
    finally:
        store.close()


def test_delete_stops_the_turn_and_hides_approvals_and_artifacts(tmp_path: Path) -> None:
    store = ChatStore(tmp_path / "chat.db")
    service = ChatService(
        store=store, provider=_SlowProvider(), model="nvidia/Nemotron-3_5-Super-VL"
    )
    try:
        thread = service.create_thread()
        meta = service.artifacts.save_upload("a.pdf", b"%PDF-1.4\n%%EOF")
        service.artifacts.claim(meta["handle"], thread["id"])
        card = service.open_exec_approval(
            thread_id=thread["id"],
            turn_id=None,
            call_id=None,
            tool="shell.exec",
            command=["echo", "hi"],
        )

        async def scenario() -> None:
            payload, _created = await service.send(
                thread["id"], text="go", client_turn_id="ct-delete"
            )
            await asyncio.sleep(0.02)
            await service.delete_thread(thread["id"])
            assert store.get_thread(thread["id"]) is None
            assert store.get_turn(payload["turn_id"])["reason"] == "cancelled"
            with pytest.raises(ChatErrorType):
                service.get_approval(card["id"])
            # The thread is gone, so its bound artifact is unreachable.
            with pytest.raises(ArtifactError):
                service.artifacts.get(meta["handle"], thread_id=thread["id"])

        asyncio.run(scenario())
    finally:
        store.close()


def test_open_exec_approval_uses_the_kel7_store(tmp_path: Path) -> None:
    service, store, _provider = make_service(tmp_path, [text_script("ok")])
    try:
        thread = service.create_thread()
        card = service.open_exec_approval(
            thread_id=thread["id"],
            turn_id=None,
            call_id="call_exec",
            tool="shell.exec",
            command=["echo", "hi"],
            blast_radius=("process:shell",),
        )
        assert card["source"] == "openshell"
        assert card["command"] == ["echo", "hi"]
        assert service.approvals.get(card["id"]).status == "pending"

        approved = service.decide(card["id"], "approve")
        assert approved["approval"]["status"] == "approved"
        assert service.approvals.get(card["id"]).status == "approved"

        with pytest.raises(ChatErrorType) as conflict:
            service.decide(card["id"], "deny")
        assert conflict.value.code == "not_approved"
        assert service.approvals.get(card["id"]).status == "approved"
    finally:
        store.close()


def test_missing_provider_is_a_typed_error(tmp_path: Path) -> None:
    store = ChatStore(tmp_path / "chat.db")
    service = ChatService(store=store, provider=None)
    try:
        thread = service.create_thread()

        async def scenario() -> None:
            payload = await send_and_wait(service, thread["id"], "hi")
            events = store.events_after(thread["id"])
            error = next(event for event in events if event["type"] == "error")
            assert error["code"] == "missing_api_key"
            assert error["message"] == "The assistant isn't connected to its model yet."
            assert events[-1]["reason"] == "error"

        asyncio.run(scenario())
    finally:
        store.close()


def test_idempotent_send_creates_one_turn(tmp_path: Path) -> None:
    service, store, provider = make_service(tmp_path, [text_script("hi")])
    try:
        thread = service.create_thread()

        async def scenario() -> None:
            first, created = await service.send(
                thread["id"], text="hello", client_turn_id="ct-same"
            )
            await service.wait_for_turn(first["turn_id"])
            second, created_again = await service.send(
                thread["id"], text="hello", client_turn_id="ct-same"
            )
            assert created is True
            assert created_again is False
            assert second["turn_id"] == first["turn_id"]
            assert second["idempotent"] is True
            assert len(store.list_messages(thread["id"])[0]) == 2
            assert len(provider.requests) == 1

        asyncio.run(scenario())
    finally:
        store.close()


def test_three_tool_calls_keep_their_order_and_cards(tmp_path: Path) -> None:
    async def executor(call: object) -> dict:
        return {"ok": True, "output": {"n": call.arguments.get("n")}}  # type: ignore[attr-defined]

    scripts = [
        tool_script("stub.tool", {"n": 1}, call_id="call_1"),
        tool_script("stub.tool", {"n": 2}, call_id="call_2"),
        tool_script("stub.tool", {"n": 3}, call_id="call_3"),
        text_script("done"),
    ]
    service, store, _provider = make_service(
        tmp_path, scripts, tool_executor_factory=executor_factory(executor)
    )
    try:
        thread = service.create_thread()

        async def scenario() -> None:
            payload = await send_and_wait(service, thread["id"], "go")
            blocks = store.get_message(payload["assistant_message_id"])["blocks"]
            assert [block["type"] for block in blocks] == ["tool_call"] * 3 + ["text"]
            assert [block["call_id"] for block in blocks[:3]] == [
                "call_1",
                "call_2",
                "call_3",
            ]
            assert all(block["status"] == "done" for block in blocks[:3])
            assert blocks[3]["text"] == "done"

        asyncio.run(scenario())
    finally:
        store.close()


def test_pending_approval_survives_boot_recovery(tmp_path: Path) -> None:
    service, store, _provider = make_service(tmp_path, [text_script("never")])
    try:
        thread = service.create_thread()
        assistant = store.create_message(
            thread_id=thread["id"],
            turn_id="turn_crash",
            role="assistant",
            status="streaming",
            blocks=[],
        )
        store.create_turn(
            thread_id=thread["id"],
            user_message_id=assistant["id"],
            assistant_message_id=assistant["id"],
            client_turn_id="ct-crash",
            budget={},
            turn_id="turn_crash",
        )
        store.update_turn("turn_crash", state="running", started_at="2026-10-29T09:00:00Z")
        store.save_approval(
            {
                "id": "apr_crash",
                "thread_id": thread["id"],
                "turn_id": "turn_crash",
                "call_id": "call_1",
                "source": "skill",
                "status": "pending",
                "requested_at": "2026-10-29T09:00:00Z",
                "expires_at": "2099-01-01T00:00:00Z",
                "effects": ["write"],
            }
        )

        service.recover_interrupted()
        card = store.get_approval("apr_crash")
        assert card["status"] == "pending"
        assert card["expires_at"] == "2099-01-01T00:00:00Z"
        assert service.decide("apr_crash", "approve")["approval"]["status"] == "approved"
    finally:
        store.close()


def test_tool_results_are_scrubbed_before_persistence(tmp_path: Path) -> None:
    secret = "result-secret-value"

    async def executor(call: object) -> dict:
        return {"ok": True, "output": {"echo": f"leaked {secret}"}}

    service, store, _provider = make_service(
        tmp_path,
        [tool_script("stub.tool", {"secret": secret}), text_script("done")],
        secrets={"DEMO_TOKEN": secret},
        tool_executor_factory=executor_factory(executor),
    )
    try:
        thread = service.create_thread()

        async def scenario() -> None:
            payload = await send_and_wait(service, thread["id"], "go")
            assert secret not in json.dumps(
                store.get_message(payload["assistant_message_id"])["blocks"]
            )
            assert secret not in json.dumps(store.events_after(thread["id"]))

        asyncio.run(scenario())
    finally:
        store.close()


def test_an_expired_approval_marks_the_card_expired_not_denied(tmp_path: Path) -> None:
    def factory(hook: object) -> CallableToolExecutor:
        async def executor(call: object) -> dict:
            allowed = await hook(_schedule_request(call))  # type: ignore[operator]
            if not allowed:
                return {"ok": False, "error": {"code": "approval_denied"}}
            return {"ok": True, "output": {}}

        return CallableToolExecutor(executor)

    scripts = [
        tool_script("schedule.create", {"title": "x", "prompt": "y",
                                        "schedule": {"kind": "cron", "expr": "* * * * *"},
                                        "timezone": "UTC"}),
        text_script("It expired, ask again."),
    ]
    service, store, _provider = make_service(
        tmp_path,
        scripts,
        tool_executor_factory=factory,
        approval_ttl_seconds=0.05,
    )
    try:
        thread = service.create_thread()

        async def scenario() -> None:
            payload = await send_and_wait(service, thread["id"], "schedule")
            cards_list = store.list_approvals(thread_id=thread["id"])
            assert cards_list[0]["status"] == "expired"
            blocks = store.get_message(payload["assistant_message_id"])["blocks"]
            tool = next(block for block in blocks if block["type"] == "tool_call")
            assert tool["status"] == "expired"
            resolved = [
                event
                for event in store.events_after(thread["id"])
                if event["type"] == "approval_resolved"
            ]
            assert resolved[-1]["status"] == "expired"
            assert resolved[-1]["executed"] is False

        asyncio.run(scenario())
    finally:
        store.close()

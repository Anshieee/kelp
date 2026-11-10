"""P3-1: TaskRun records, the runs table, and terminal-once semantics."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

from chat import ChatAPI, ChatService, ChatStore, Request
from chat.api import LoopRunner
from chat.errors import ChatError
from chat.proactive import ProactiveRunner
from chat.runs import (
    RUN_STATUSES,
    RUN_TERMINAL_STATUSES,
    RunNotFoundError,
    RunRecorder,
    RunStatus,
    RunTerminalError,
    TaskRun,
    TaskRunner,
)
from chat.scheduler import ScheduleStore, SchedulerRuntime
from openshell.audit import AuditLog

from .chatkit import (
    MODEL,
    StubProvider,
    executor_factory,
    make_service,
    send_and_wait,
    text_script,
    tool_script,
)

CONTRACT_FIELD_ORDER = [
    "run_id",
    "tenant_id",
    "thread_id",
    "turn_id",
    "surface",
    "task_type",
    "status",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "model",
    "provider",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cost_usd",
    "steps",
    "tool_calls",
    "approvals",
    "error_code",
    "retry_of",
    "idempotency_key",
]

BASE = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
DUE = BASE + timedelta(seconds=60)

AUTH = {"authorization": "Bearer tok"}
CSRF = {"x-csrf-token": "csrf"}


def _make_api(
    tmp_path: Path, scripts: list[list[Any]] | None = None
) -> tuple[ChatAPI, ChatService, ChatStore, StubProvider]:
    """A one-owner API over a real store, with the service's stub provider."""

    store = ChatStore(tmp_path / "chat.db")
    provider = StubProvider(scripts or [text_script("Hello")])
    service = ChatService(store=store, provider=provider, model=MODEL)
    api = ChatAPI(service, auth_token="tok", csrf_token="csrf")
    return api, service, store, provider


def _api_call(
    api: ChatAPI,
    method: str,
    path: str,
    body: Mapping[str, Any] | None = None,
    *,
    query: Mapping[str, list[str]] | None = None,
    auth: bool = True,
    csrf: bool = True,
) -> Any:
    headers: dict[str, str] = {}
    if auth:
        headers.update(AUTH)
    if csrf and method.upper() in {"POST", "PATCH", "DELETE"}:
        headers.update(CSRF)
    if body is not None:
        headers["content-type"] = "application/json"
    raw = json.dumps(body).encode() if body is not None else b""
    return api.handle(Request(method, path, query or {}, headers, raw))


class FailingProvider:
    """Every turn fails, which makes the turn retryable."""

    async def stream(self, request: Any) -> Any:
        raise RuntimeError("provider down")
        yield  # pragma: no cover - makes this an async generator


def _seed_turn(store: ChatStore, thread_id: str, turn_id: str) -> dict[str, Any]:
    user = store.create_message(
        thread_id=thread_id,
        turn_id=turn_id,
        role="user",
        status="complete",
        blocks=[{"type": "text", "text": "hi"}],
    )
    assistant = store.create_message(
        thread_id=thread_id,
        turn_id=turn_id,
        role="assistant",
        status="streaming",
        blocks=[],
    )
    store.create_turn(
        thread_id=thread_id,
        user_message_id=user["id"],
        assistant_message_id=assistant["id"],
        client_turn_id=None,
        budget={},
        turn_id=turn_id,
    )
    store.update_turn(turn_id, state="running", started_at="2026-10-29T09:00:00Z")
    return store.get_turn(turn_id)  # type: ignore[return-value]


# -- the frozen shape ------------------------------------------------------


def test_task_run_fields_and_statuses_match_the_frozen_contract() -> None:
    assert [field.name for field in dataclasses.fields(TaskRun)] == (
        CONTRACT_FIELD_ORDER
    )
    assert tuple(status.value for status in RunStatus) == RUN_STATUSES
    assert RUN_STATUSES == (
        "queued",
        "running",
        "awaiting_approval",
        "stopping",
        "completed",
        "failed",
        "cancelled",
        "interrupted",
    )
    assert RUN_TERMINAL_STATUSES == {
        "completed",
        "failed",
        "cancelled",
        "interrupted",
    }

    record = TaskRun(run_id="run_1", status=RunStatus.COMPLETED).to_dict()
    assert list(record) == CONTRACT_FIELD_ORDER
    assert record["status"] == "completed"
    assert TaskRun.from_record(record) == TaskRun(
        run_id="run_1", status=RunStatus.COMPLETED
    )
    assert "turn_id" not in TaskRun(run_id="run_1").to_admin_row()


# -- one run per turn ------------------------------------------------------


def test_one_run_per_turn_records_metadata_and_counters(tmp_path: Path) -> None:
    service, store, _provider = make_service(tmp_path, [text_script("Hello")])
    try:
        thread = service.create_thread()

        async def scenario() -> None:
            payload = await send_and_wait(service, thread["id"], "hi")
            runs = store.list_runs(tenant_id="owner", thread_id=thread["id"])
            assert len(runs) == 1
            run = runs[0]
            assert payload["run_id"] == run["run_id"]
            assert run["run_id"].startswith("run_")
            assert run["turn_id"] == payload["turn_id"]
            assert run["turn_id"].startswith("turn_")
            assert run["thread_id"] == thread["id"]
            assert run["tenant_id"] == "owner"
            assert run["surface"] == "web"
            assert run["task_type"] == "chat"
            assert run["status"] == "completed"
            assert run["idempotency_key"] == "ct-1234abcd"
            assert run["model"] == MODEL
            assert run["provider"]
            assert (run["input_tokens"], run["output_tokens"], run["total_tokens"]) == (
                10,
                5,
                15,
            )
            assert run["started_at"] is not None
            assert run["finished_at"] is not None
            assert run["error_code"] is None
            assert run["retry_of"] is None
            assert store.get_run_by_turn(payload["turn_id"]) == run

        asyncio.run(scenario())
    finally:
        store.close()


# -- terminal once ---------------------------------------------------------


def test_a_terminal_run_cannot_transition_again(tmp_path: Path) -> None:
    store = ChatStore(tmp_path / "chat.db")
    recorder = RunRecorder(store)
    try:
        run = recorder.create(
            tenant_id="owner", thread_id="ses_a", turn_id="turn_a", model="m"
        )
        running = recorder.transition(run.run_id, RunStatus.RUNNING)
        assert running.status is RunStatus.RUNNING
        assert running.started_at is not None

        completed = recorder.transition(run.run_id, RunStatus.COMPLETED)
        assert completed.status is RunStatus.COMPLETED
        assert completed.finished_at is not None

        with pytest.raises(RunTerminalError):
            recorder.transition(run.run_id, RunStatus.FAILED)
        with pytest.raises(RunTerminalError):
            recorder.transition(run.run_id, RunStatus.COMPLETED)

        # The store repeats the guard: a direct late writer also loses.
        assert store.update_run(run.run_id, status="failed") is None
        assert store.get_run(run.run_id)["status"] == "completed"
    finally:
        store.close()


def test_terminal_hook_runs_once_and_never_reopens_the_record(tmp_path: Path) -> None:
    seen: list[TaskRun] = []
    store = ChatStore(tmp_path / "chat.db")
    recorder = RunRecorder(store, on_terminal=seen.append)
    try:
        run = recorder.create(tenant_id="owner", turn_id="turn_hook")
        recorder.transition(run.run_id, RunStatus.COMPLETED)
        assert len(seen) == 1
        assert seen[0].run_id == run.run_id
        with pytest.raises(RunTerminalError):
            recorder.transition(run.run_id, RunStatus.FAILED)
        assert len(seen) == 1
    finally:
        store.close()


# -- idempotency -----------------------------------------------------------


def test_replayed_idempotency_key_returns_the_existing_run(tmp_path: Path) -> None:
    store = ChatStore(tmp_path / "chat.db")
    recorder = RunRecorder(store)
    try:
        first = recorder.create(
            tenant_id="owner",
            thread_id="ses_a",
            turn_id="turn_a",
            idempotency_key="client-1",
        )
        replay = recorder.create(
            tenant_id="owner",
            thread_id="ses_a",
            turn_id="turn_a",
            idempotency_key="client-1",
        )
        assert replay.run_id == first.run_id
        assert len(store.list_runs(tenant_id="owner")) == 1

        # A client turn id is per thread, so the same key on another thread is
        # different work, not a replay.
        other = recorder.create(
            tenant_id="owner",
            thread_id="ses_b",
            turn_id="turn_b",
            idempotency_key="client-1",
        )
        assert other.run_id != first.run_id
        assert len(store.list_runs(tenant_id="owner")) == 2
    finally:
        store.close()


def test_service_replay_returns_the_same_run_and_creates_no_second_row(
    tmp_path: Path,
) -> None:
    service, store, provider = make_service(tmp_path, [text_script("once")])
    try:
        thread = service.create_thread()

        async def scenario() -> None:
            first = await send_and_wait(service, thread["id"], "hi")
            second, created = await service.send(
                thread["id"], text="hi", client_turn_id="ct-1234abcd"
            )
            assert created is False
            assert second["idempotent"] is True
            assert second["run_id"] == first["run_id"]
            assert len(store.list_runs(tenant_id="owner")) == 1
            assert len(provider.requests) == 1

        asyncio.run(scenario())
    finally:
        store.close()


# -- retry -----------------------------------------------------------------


def test_retry_creates_a_new_run_linked_by_retry_of(tmp_path: Path) -> None:
    store = ChatStore(tmp_path / "chat.db")
    service = ChatService(store=store, provider=FailingProvider(), model=MODEL)
    try:
        thread = service.create_thread()

        async def scenario() -> None:
            first = await send_and_wait(service, thread["id"], "hi")
            first_run = store.get_run_by_turn(first["turn_id"])
            assert first_run["status"] == "failed"

            retried = await service.retry(first["turn_id"])
            await service.wait_for_turn(retried["turn_id"], timeout=5.0)
            second_run = store.get_run_by_turn(retried["turn_id"])
            assert second_run["run_id"] != first_run["run_id"]
            assert second_run["retry_of"] == first_run["run_id"]
            assert second_run["turn_id"] != first_run["turn_id"]
            assert retried["retry_of_run"] == first_run["run_id"]
            assert len(store.list_runs(tenant_id="owner")) == 2

        asyncio.run(scenario())
    finally:
        store.close()


# -- recovery --------------------------------------------------------------


def test_boot_recovery_closes_an_orphaned_run_as_interrupted(tmp_path: Path) -> None:
    service, store, _provider = make_service(tmp_path, [text_script("never")])
    try:
        thread = service.create_thread()
        _seed_turn(store, thread["id"], "turn_dead")
        run = service._runs.create(  # noqa: SLF001 - the recorder under test
            tenant_id="owner",
            thread_id=thread["id"],
            turn_id="turn_dead",
            idempotency_key="ct-dead",
        )
        service._runs.transition(run.run_id, RunStatus.RUNNING)  # noqa: SLF001

        assert service.recover_interrupted() == ["turn_dead"]
        recovered = store.get_run_by_turn("turn_dead")
        assert recovered["status"] == "interrupted"
        assert recovered["finished_at"] is not None
        assert recovered["error_code"] is None

        # Nothing changes on a second boot: the terminal write is final.
        before = dict(recovered)
        assert service.recover_interrupted() == []
        assert store.get_run_by_turn("turn_dead") == before
    finally:
        store.close()


def test_recovery_uses_each_turns_own_tenant(tmp_path: Path) -> None:
    service, store, _provider = make_service(tmp_path, [text_script("never")])
    try:
        owner_thread = service.create_thread()
        other_thread = service.create_thread(tenant="acme")
        _seed_turn(store, owner_thread["id"], "turn_owner")
        _seed_turn(store, other_thread["id"], "turn_acme")
        owner_run = service._runs.create(  # noqa: SLF001
            tenant_id="owner", thread_id=owner_thread["id"], turn_id="turn_owner"
        )
        other_run = service._runs.create(  # noqa: SLF001
            tenant_id="acme", thread_id=other_thread["id"], turn_id="turn_acme"
        )
        service._runs.transition(owner_run.run_id, RunStatus.RUNNING)  # noqa: SLF001
        service._runs.transition(other_run.run_id, RunStatus.RUNNING, tenant_id="acme")  # noqa: SLF001

        closed = service.recover_interrupted()
        assert set(closed) == {"turn_owner", "turn_acme"}
        assert store.get_run(owner_run.run_id, tenant_id="owner")["status"] == (
            "interrupted"
        )
        assert store.get_run(other_run.run_id, tenant_id="acme")["status"] == (
            "interrupted"
        )
    finally:
        store.close()


# -- content exclusions and tenant isolation -------------------------------


def test_run_rows_hold_no_prompt_tool_arguments_or_secrets(tmp_path: Path) -> None:
    async def executor(call: object) -> dict:
        return {"ok": True, "output": {"results": []}}

    service, store, _provider = make_service(
        tmp_path,
        [tool_script("web.search", {"query": "super-secret-query"}), text_script("done")],
        tool_executor_factory=executor_factory(executor),
    )
    try:
        thread = service.create_thread()

        async def scenario() -> None:
            await send_and_wait(service, thread["id"], "super-secret-prompt")

        asyncio.run(scenario())

        connection = sqlite3.connect(str(store.path))
        try:
            columns = [
                str(row[1])
                for row in connection.execute("PRAGMA table_info(runs)").fetchall()
            ]
            rows = connection.execute("SELECT * FROM runs").fetchall()
        finally:
            connection.close()
        assert rows
        for forbidden in (
            "prompt_text",
            "message_text",
            "tool_arguments",
            "tool_results",
            "provider_response_body",
            "api_key",
            "secret_values",
            "headers",
            "cookies",
        ):
            assert forbidden not in columns
        serialized = json.dumps(
            [dict(zip(columns, row)) for row in rows], default=str
        )
        assert "super-secret-prompt" not in serialized
        assert "super-secret-query" not in serialized

        # A caller that tries to make the run a conversation copy is refused.
        with pytest.raises(ValueError):
            store.create_run(
                {"run_id": "run_bad", "prompt_text": "super-secret-prompt"}
            )
    finally:
        store.close()


def test_cross_tenant_runs_are_invisible_and_immutable(tmp_path: Path) -> None:
    store = ChatStore(tmp_path / "chat.db")
    recorder = RunRecorder(store)
    try:
        acme = recorder.create(
            tenant_id="acme",
            thread_id="ses_acme",
            turn_id="turn_acme",
            idempotency_key="key-acme",
        )
        assert store.get_run(acme.run_id, tenant_id="owner") is None
        assert recorder.get(acme.run_id, tenant_id="owner") is None
        assert recorder.for_turn("turn_acme", tenant_id="owner") is None
        assert store.get_run_by_turn("turn_acme", tenant_id="owner") is None
        assert store.find_run_by_idempotency(
            "key-acme", tenant_id="owner", thread_id="ses_acme"
        ) is None
        assert store.list_runs(tenant_id="owner") == []
        assert store.get_run(acme.run_id, tenant_id="acme")["run_id"] == acme.run_id

        with pytest.raises(RunNotFoundError):
            recorder.transition(acme.run_id, RunStatus.FAILED, tenant_id="owner")
        assert store.get_run(acme.run_id, tenant_id="acme")["status"] == "queued"
    finally:
        store.close()


# -- proactive scheduled turns --------------------------------------------


def test_scheduled_turn_creates_a_scheduler_run(tmp_path: Path) -> None:
    clock = lambda: BASE  # noqa: E731 - one pinned instant for the whole test
    store = ChatStore(tmp_path / "chat.db")
    audit = AuditLog(tmp_path / "audit" / "audit.jsonl")
    service = ChatService(
        store=store,
        provider=StubProvider([text_script("Morning brief ready")]),
        model=MODEL,
        audit=audit,
    )
    schedule_store = ScheduleStore(tmp_path / "workspace", now=clock)
    runner = LoopRunner()
    proactive = ProactiveRunner(
        service=service,
        store=store,
        schedule_store=schedule_store,
        audit=audit,
        runner=runner,
        now=clock,
    )
    scheduler = SchedulerRuntime(
        schedule_store,
        on_fire=proactive.fire,
        audit=proactive.audit_gate,
        now=clock,
    )
    try:
        task = schedule_store.create_task(
            title="Morning brief",
            prompt="Summarize the open issues",
            expr="* * * * *",
            timezone="UTC",
            now=clock(),
            audit=audit,
        )
        report = scheduler.tick(now=DUE)
        assert report["fired"] == 1
        assert proactive.wait_for_idle(5.0) is True

        thread_id = proactive.thread_id_for(task["task_id"])
        runs = store.list_runs(tenant_id="owner", thread_id=thread_id)
        assert len(runs) == 1
        run = runs[0]
        assert run["surface"] == "scheduler"
        assert run["task_type"] == "scheduled"
        assert run["status"] == "completed"
        assert run["turn_id"].startswith("turn_")
        assert run["idempotency_key"] == (
            f"schedule:{task['task_id']}:2026-10-30T12:01:00Z"
        )
    finally:
        scheduler.stop()
        try:
            runner.run(service.aclose(), timeout=5.0)
        except Exception:  # noqa: BLE001 - cleanup must not mask a failure
            pass
        proactive.close(5.0)
        runner.close()
        store.close()


# -- task-run API (P3-2) ---------------------------------------------------


def test_task_runner_starts_a_run_and_replays_its_key(tmp_path: Path) -> None:
    service, store, provider = make_service(tmp_path, [text_script("Hello")])
    runner = TaskRunner(service)
    try:

        async def scenario() -> None:
            run = await runner.start(text="hi", idempotency_key="run-key-1")
            assert isinstance(run, TaskRun)
            assert run.run_id.startswith("run_")
            assert run.thread_id is not None
            assert run.turn_id is not None
            assert run.surface == "web"
            assert run.task_type == "chat"
            await service.wait_for_turn(run.turn_id, timeout=5.0)
            assert runner.get(run.run_id).status is RunStatus.COMPLETED

            # The replay returns the same run and reaches no provider.
            replay = await runner.start(text="hi", idempotency_key="run-key-1")
            assert replay.run_id == run.run_id
            assert len(provider.requests) == 1
            assert len(store.list_runs(tenant_id="owner")) == 1

            # A named thread is reused rather than replaced.
            again = await runner.start(
                prompt="again",
                thread_id=run.thread_id,
                idempotency_key="run-key-2",
                surface="cli",
                task_type="plugin",
            )
            assert again.thread_id == run.thread_id
            assert again.surface == "cli"
            assert again.task_type == "plugin"
            await service.wait_for_turn(again.turn_id, timeout=5.0)

            listed = runner.list(tenant_id="owner")
            assert {item.run_id for item in listed} == {run.run_id, again.run_id}
            assert runner.list(tenant_id="acme") == []
            assert runner.get(run.run_id, tenant_id="acme") is None

        asyncio.run(scenario())
    finally:
        store.close()


def test_task_runner_validates_before_minting_a_thread(tmp_path: Path) -> None:
    service, store, _provider = make_service(tmp_path, [text_script("Hello")])
    runner = TaskRunner(service)
    try:

        async def scenario() -> None:
            with pytest.raises(ChatError):
                await runner.start(prompt="   ")
            with pytest.raises(ChatError):
                await runner.start(text="a", prompt="b")
            with pytest.raises(ChatError):
                await runner.start(text="hi", idempotency_key="not valid!")
            with pytest.raises(ChatError):
                await runner.start(text="hi", surface="telepathy")
            with pytest.raises(ChatError):
                await runner.start(text="hi", task_type="Chat Bot")
            # Nothing malformed cost a thread or a turn.
            threads, _cursor = store.list_threads(tenant_id="owner")
            assert threads == []
            assert store.list_runs(tenant_id="owner") == []

        asyncio.run(scenario())
    finally:
        store.close()


def test_api_run_create_get_and_list_expose_state_only(tmp_path: Path) -> None:
    api, service, store, _provider = _make_api(tmp_path)
    try:
        created = _api_call(
            api,
            "POST",
            "/api/runs",
            {"text": "super-secret-prompt", "idempotency_key": "api-key-1"},
        )
        assert created.status == 202, created.body
        envelope = json.loads(created.body)["run"]
        assert envelope["run_id"].startswith("run_")
        assert envelope["status"] in RUN_STATUSES
        assert envelope["tenant"] == "owner"
        stored = store.get_run(envelope["run_id"], tenant_id="owner")
        assert envelope["thread"] == stored["thread_id"]
        assert envelope["links"] == {
            "self": f"/api/runs/{envelope['run_id']}",
            "thread": f"/api/threads/{stored['thread_id']}",
            "stream": f"/api/threads/{stored['thread_id']}/stream",
        }
        assert b"super-secret-prompt" not in created.body

        api.runner.run(service.wait_for_turn(stored["turn_id"], timeout=5.0))

        fetched = _api_call(api, "GET", f"/api/runs/{envelope['run_id']}")
        assert fetched.status == 200, fetched.body
        row = json.loads(fetched.body)["run"]
        assert row["run_id"] == envelope["run_id"]
        assert row["status"] == "completed"
        assert row["turn_id"] == stored["turn_id"]
        assert "prompt_text" not in row
        assert b"super-secret-prompt" not in fetched.body

        listed = _api_call(api, "GET", "/api/runs", query={"status": ["completed"]})
        assert listed.status == 200
        payload = json.loads(listed.body)
        assert [item["run_id"] for item in payload["runs"]] == [envelope["run_id"]]
        assert payload["next_offset"] is None
        assert b"super-secret-prompt" not in listed.body
    finally:
        api.close()
        store.close()


def test_api_run_post_replay_returns_the_same_run_without_new_work(
    tmp_path: Path,
) -> None:
    api, service, store, provider = _make_api(tmp_path, [text_script("once")])
    try:
        submission = {"text": "hi", "idempotency_key": "replay-key-1"}
        first = _api_call(api, "POST", "/api/runs", submission)
        assert first.status == 202, first.body
        original = json.loads(first.body)["run"]
        turn_id = store.get_run(original["run_id"], tenant_id="owner")["turn_id"]
        api.runner.run(service.wait_for_turn(turn_id, timeout=5.0))

        second = _api_call(api, "POST", "/api/runs", submission)
        assert second.status == 202, second.body
        replay = json.loads(second.body)["run"]
        assert replay["run_id"] == original["run_id"]
        assert replay["thread"] == original["thread"]
        assert replay["status"] == "completed"
        assert len(provider.requests) == 1
        assert len(store.list_runs(tenant_id="owner")) == 1
        threads, _cursor = store.list_threads(tenant_id="owner")
        assert len(threads) == 1
    finally:
        api.close()
        store.close()


def test_api_run_reads_are_tenant_scoped_and_foreign_ids_look_missing(
    tmp_path: Path,
) -> None:
    api, _service, store, _provider = _make_api(tmp_path)
    recorder = RunRecorder(store)
    try:
        foreign = recorder.create(
            tenant_id="acme",
            thread_id="ses_acme",
            turn_id="turn_acme",
            idempotency_key="acme-key",
        )
        foreign_response = _api_call(api, "GET", f"/api/runs/{foreign.run_id}")
        missing_response = _api_call(api, "GET", "/api/runs/run_missing")
        assert foreign_response.status == 404
        assert missing_response.status == 404
        # The two answers are byte-for-byte the same: existence is not an oracle.
        assert foreign_response.body == missing_response.body

        listed = _api_call(
            api, "GET", "/api/runs", query={"thread_id": ["ses_acme"]}
        )
        assert json.loads(listed.body)["runs"] == []

        denied = _api_call(
            api, "POST", "/api/runs", {"text": "hi", "tenant_id": "acme"}
        )
        assert denied.status == 403
        assert json.loads(denied.body)["error"]["code"] == "tenant_mismatch"

        # The ordinary boundary still stands in front of the run routes.
        assert _api_call(api, "GET", "/api/runs", auth=False).status == 401
        no_csrf = _api_call(api, "POST", "/api/runs", {"text": "hi"}, csrf=False)
        assert no_csrf.status == 403
    finally:
        api.close()
        store.close()


def test_api_run_list_filters_and_paginates(tmp_path: Path) -> None:
    api, _service, store, _provider = _make_api(tmp_path)
    recorder = RunRecorder(store)
    try:
        older = recorder.create(
            tenant_id="owner",
            thread_id="ses_a",
            turn_id="turn_1",
            created_at="2026-10-30T12:00:00Z",
        )
        newer = recorder.create(
            tenant_id="owner",
            thread_id="ses_a",
            turn_id="turn_2",
            status=RunStatus.COMPLETED,
            created_at="2026-10-30T12:01:00Z",
        )
        other_thread = recorder.create(
            tenant_id="owner",
            thread_id="ses_b",
            turn_id="turn_3",
            created_at="2026-10-30T12:02:00Z",
        )

        # Newest first, one row per page, with an honest next offset.
        page = json.loads(
            _api_call(api, "GET", "/api/runs", query={"limit": ["2"]}).body
        )
        assert [item["run_id"] for item in page["runs"]] == [
            other_thread.run_id,
            newer.run_id,
        ]
        assert page["next_offset"] == 2

        page2 = json.loads(
            _api_call(
                api,
                "GET",
                "/api/runs",
                query={"limit": ["2"], "offset": ["2"]},
            ).body
        )
        assert [item["run_id"] for item in page2["runs"]] == [older.run_id]
        assert page2["next_offset"] is None

        filtered = json.loads(
            _api_call(
                api,
                "GET",
                "/api/runs",
                query={"thread_id": ["ses_a"], "status": ["completed"]},
            ).body
        )
        assert [item["run_id"] for item in filtered["runs"]] == [newer.run_id]

        short = json.loads(
            _api_call(api, "GET", "/api/runs", query={"thread": ["ses_b"]}).body
        )
        assert [item["run_id"] for item in short["runs"]] == [other_thread.run_id]
    finally:
        api.close()
        store.close()


def test_api_run_route_rejects_bad_input_without_creating_work(
    tmp_path: Path,
) -> None:
    api, _service, store, _provider = _make_api(tmp_path)
    try:
        empty = _api_call(api, "POST", "/api/runs", {})
        assert empty.status == 400
        assert json.loads(empty.body)["error"]["code"] == "invalid_message"

        both = _api_call(api, "POST", "/api/runs", {"text": "a", "prompt": "b"})
        assert both.status == 400
        assert json.loads(both.body)["error"]["code"] == "invalid_request"

        bad_surface = _api_call(
            api, "POST", "/api/runs", {"text": "hi", "surface": "telepathy"}
        )
        assert bad_surface.status == 400

        bad_status = _api_call(
            api, "GET", "/api/runs", query={"status": ["zombie"]}
        )
        assert bad_status.status == 400
        assert json.loads(bad_status.body)["error"]["code"] == "invalid_request"

        assert (
            _api_call(api, "DELETE", "/api/runs/run_nope").status == 404
        )
        assert store.list_threads(tenant_id="owner")[0] == []
        assert store.list_runs(tenant_id="owner") == []
    finally:
        api.close()
        store.close()

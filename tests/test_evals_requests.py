"""Request-shape observations: the offline gate inspects what was sent.

The offline tool and multi-turn tasks replay recorded provider output, so the
final text alone cannot tell whether the request carried the tool specs, the
tool results, or the prompt. These tests pin the observations that do, and
break the request on purpose to show each one can fail.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from evals import runner
from evals.dataset import DEFAULT_DATASET, DEFAULT_FIXTURES, load_dataset
from evals.fixtures import load_recorded_fixtures
from evals.request_checks import (
    check_followup_last,
    check_history,
    check_retry_prompt,
    check_tool_results,
    check_tool_specs,
    tool_spec_name,
)
from kernel.providers.base import ChatRequest, StreamEnd, TextDelta

_DATASET = load_dataset(DEFAULT_DATASET)
_FIXTURES = load_recorded_fixtures(DEFAULT_FIXTURES)
_TASKS = {str(task["id"]): task for task in _DATASET.tasks}

_TOOL_TASK_IDS = [
    str(task["id"])
    for task in _DATASET.tasks
    if task["scenario"] in {"tool_turn", "approval_approve", "approval_deny",
                            "approval_redaction", "approval_replay"}
    and (task.get("expect") or {}).get("tool_calls")
]


def _run(task: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    return asyncio.run(
        runner._run_task(
            task,
            fixtures=_FIXTURES,
            live=False,
            model=runner.DEFAULT_MODEL,
            repeat=1,
            live_client=None,
            work_root=tmp_path,
        )
    )


def _checks(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {check["name"]: check for check in result["checks"]}


def _request(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> ChatRequest:
    return ChatRequest(model="m", messages=messages, tools=tools or [])


def _spec(name: str) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "parameters": {}}}


# ---------------------------------------------------------------------------
# helpers


class TestHelpers:
    def test_tool_spec_name_reads_function_wrapper_and_flat_specs(self) -> None:
        assert tool_spec_name(_spec("docs.answer")) == "docs.answer"
        assert tool_spec_name({"name": "web.search"}) == "web.search"
        assert tool_spec_name({"type": "function"}) is None
        assert tool_spec_name("nope") is None  # type: ignore[arg-type]

    def test_tool_specs_pass_when_every_request_lists_the_names(self) -> None:
        requests = [_request([], [_spec("docs.answer")]), _request([], [_spec("docs.answer")])]
        assert check_tool_specs(requests, ["docs.answer"])[0] is True

    def test_tool_specs_fail_when_one_request_drops_the_tool(self) -> None:
        requests = [_request([], [_spec("docs.answer")]), _request([], [])]
        ok, detail = check_tool_specs(requests, ["docs.answer"])
        assert ok is False
        assert "request 2" in detail and "docs.answer" in detail

    def test_tool_specs_fail_without_any_request(self) -> None:
        assert check_tool_specs([], ["docs.answer"])[0] is False

    def test_tool_results_pass_when_the_last_request_feeds_each_call_back(self) -> None:
        requests = [
            _request([{"role": "user", "content": "q"}]),
            _request(
                [
                    {"role": "user", "content": "q"},
                    {"role": "assistant", "content": None, "tool_calls": []},
                    {"role": "tool", "tool_call_id": "call_1", "content": "{}"},
                ]
            ),
        ]
        assert check_tool_results(requests, ["call_1"])[0] is True

    def test_tool_results_fail_for_a_missing_or_mismatched_call_id(self) -> None:
        requests = [
            _request([{"role": "user", "content": "q"}]),
            _request([{"role": "tool", "tool_call_id": "other", "content": "{}"}]),
        ]
        ok, detail = check_tool_results(requests, ["call_1"])
        assert ok is False
        assert "call_1" in detail

    def test_tool_results_fail_when_there_was_no_second_request(self) -> None:
        assert check_tool_results([_request([{"role": "user", "content": "q"}])], ["c"])[0] is False

    def test_tool_results_fail_without_any_observed_call(self) -> None:
        assert check_tool_results([_request([]), _request([])], [])[0] is False

    def test_history_passes_when_prompt_and_answer_are_in_the_second_request(self) -> None:
        second = _request(
            [
                {"role": "user", "content": "My name is Ada."},
                {"role": "assistant", "content": "Hello Ada."},
                {"role": "user", "content": "What is my name?"},
            ]
        )
        assert check_history([_request([]), second], "My name is Ada.", "Hello Ada.")[0] is True

    def test_history_fails_when_the_second_request_is_stripped(self) -> None:
        second = _request([{"role": "user", "content": "What is my name?"}])
        ok, detail = check_history([_request([]), second], "My name is Ada.", "Hello Ada.")
        assert ok is False
        assert "prompt" in detail and "answer" in detail

    def test_history_fails_without_a_second_request(self) -> None:
        assert check_history([_request([])], "p", "a")[0] is False

    def test_history_reads_block_list_content(self) -> None:
        second = _request(
            [
                {"role": "user", "content": [{"type": "text", "text": "My name is Ada."}]},
                {"role": "assistant", "content": [{"type": "text", "text": "Hello Ada."}]},
            ]
        )
        assert check_history([_request([]), second], "My name is Ada.", "Hello Ada.")[0] is True

    def test_followup_last_requires_the_final_user_message(self) -> None:
        good = _request([{"role": "user", "content": "a"}, {"role": "user", "content": "What?"}])
        bad = _request([{"role": "user", "content": "What?"}, {"role": "assistant", "content": "x"}])
        assert check_followup_last([_request([]), good], "What?")[0] is True
        assert check_followup_last([_request([]), bad], "What?")[0] is False

    def test_retry_prompt_requires_the_original_prompt_in_the_retried_request(self) -> None:
        good = _request([{"role": "user", "content": "Explain."}])
        bad = _request([{"role": "user", "content": "something else"}])
        assert check_retry_prompt([_request([]), good], "Explain.")[0] is True
        assert check_retry_prompt([_request([]), bad], "Explain.")[0] is False
        assert check_retry_prompt([good], "Explain.")[0] is False

    def test_details_stay_short_for_long_transcripts(self) -> None:
        big = "x" * 5000
        second = _request([{"role": "user", "content": big}])
        _ok, detail = check_history([_request([]), second], "p" * 400, "a" * 400)
        assert len(detail) < 200


# ---------------------------------------------------------------------------
# the provider wrapper must record what was sent, not what the transcript later became


class _Inner:
    async def stream(self, request: ChatRequest) -> AsyncIterator[Any]:
        yield TextDelta("ok")
        yield StreamEnd("stop")


def test_counting_provider_snapshots_messages_at_request_time() -> None:
    transcript: list[dict[str, Any]] = [{"role": "user", "content": "q"}]
    provider = runner.CountingProvider(_Inner())

    async def go() -> None:
        async for _ in provider.stream(_request(transcript)):
            pass

    asyncio.run(go())
    transcript.append({"role": "assistant", "content": "later"})
    transcript[0]["content"] = "mutated"

    assert [m["content"] for m in provider.requests[0].messages] == ["q"]


# ---------------------------------------------------------------------------
# real offline runs


class TestOfflineRunsObserveRequests:
    @pytest.mark.parametrize("task_id", _TOOL_TASK_IDS)
    def test_tool_tasks_check_specs_and_results_and_pass(self, task_id: str, tmp_path: Path) -> None:
        result = _run(_TASKS[task_id], tmp_path)
        checks = _checks(result)

        assert checks["request_tool_specs"]["ok"] is True, checks["request_tool_specs"]
        assert checks["request_tool_results"]["ok"] is True, checks["request_tool_results"]
        assert result["passed"] is True

    def test_tool_task_set_is_not_empty(self) -> None:
        assert "EV-010" in _TOOL_TASK_IDS
        assert len(_TOOL_TASK_IDS) >= 6

    def test_multi_turn_two_checks_the_followup_is_the_last_message(self, tmp_path: Path) -> None:
        result = _run(_TASKS["EV-023"], tmp_path)

        assert _checks(result)["followup_sent_last"]["ok"] is True
        assert result["passed"] is True

    def test_history_check_is_opt_in_through_the_expect_block(self, tmp_path: Path) -> None:
        plain = _run(_TASKS["EV-023"], tmp_path / "a")
        assert "history_sent" not in _checks(plain)

        task = copy.deepcopy(_TASKS["EV-023"])
        task["expect"]["history_in_request"] = True
        opted = _run(task, tmp_path / "b")
        assert "history_sent" in _checks(opted)

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "ChatService._run_turn sends only the current user text to the model; "
            "docs/CHAT_SPEC.md section 1 leaves prompt assembly to a later slice. "
            "Remove this marker, and set expect.history_in_request in EV-023, when "
            "the service starts sending history."
        ),
    )
    def test_the_real_service_sends_history_on_the_second_turn(self, tmp_path: Path) -> None:
        task = copy.deepcopy(_TASKS["EV-023"])
        task["expect"]["history_in_request"] = True

        checks = _checks(_run(task, tmp_path))

        assert checks["history_sent"]["ok"] is True

    def test_resume_retry_checks_the_retried_request_carries_the_prompt(self, tmp_path: Path) -> None:
        result = _run(_TASKS["EV-022"], tmp_path)

        assert _checks(result)["retry_request_prompt"]["ok"] is True
        assert result["passed"] is True


# ---------------------------------------------------------------------------
# negative runs: break the request, the observation must fail


def _patch_provider(monkeypatch: pytest.MonkeyPatch, edit: Any) -> None:
    """Make the service's provider hand a damaged request to the recorder."""

    class Damaging(runner.CountingProvider):
        async def stream(self, request: ChatRequest) -> AsyncIterator[Any]:
            async for event in super().stream(edit(request)):
                yield event

    monkeypatch.setattr(runner, "CountingProvider", Damaging)


class TestDamagedRequestsFailTheGate:
    def test_dropping_every_tool_spec_fails_request_tool_specs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(runner, "build_tool_specs", lambda *a, **k: ())

        result = _run(_TASKS["EV-010"], tmp_path)
        check = _checks(result)["request_tool_specs"]

        assert check["ok"] is False
        assert "docs.answer" in check["detail"]
        assert result["passed"] is False

    def test_dropping_only_the_expected_tool_spec_fails_with_its_name(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def edit(request: ChatRequest) -> ChatRequest:
            kept = [s for s in request.tools if tool_spec_name(s) != "docs.answer"]
            return dataclasses.replace(request, tools=kept)

        _patch_provider(monkeypatch, edit)
        check = _checks(_run(_TASKS["EV-010"], tmp_path))["request_tool_specs"]

        assert check["ok"] is False
        assert "docs.answer" in check["detail"]

    def test_dropping_tool_spec_from_only_the_second_request_still_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def edit(request: ChatRequest) -> ChatRequest:
            if any(m.get("role") == "tool" for m in request.messages):
                return dataclasses.replace(request, tools=[])
            return request

        _patch_provider(monkeypatch, edit)
        check = _checks(_run(_TASKS["EV-010"], tmp_path))["request_tool_specs"]

        assert check["ok"] is False
        assert "request 2" in check["detail"]

    def test_dropping_the_tool_result_message_fails_request_tool_results(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def edit(request: ChatRequest) -> ChatRequest:
            kept = [m for m in request.messages if m.get("role") != "tool"]
            return dataclasses.replace(request, messages=kept)

        _patch_provider(monkeypatch, edit)
        result = _run(_TASKS["EV-010"], tmp_path)
        check = _checks(result)["request_tool_results"]

        assert check["ok"] is False
        assert result["passed"] is False

    def test_a_tool_result_for_the_wrong_call_id_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def edit(request: ChatRequest) -> ChatRequest:
            messages = [
                {**m, "tool_call_id": "wrong"} if m.get("role") == "tool" else m
                for m in request.messages
            ]
            return dataclasses.replace(request, messages=messages)

        _patch_provider(monkeypatch, edit)
        check = _checks(_run(_TASKS["EV-010"], tmp_path))["request_tool_results"]

        assert check["ok"] is False

    def test_history_check_fails_for_a_stripped_second_request(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """With history opted in, a request that omits the first turn is caught."""

        task = copy.deepcopy(_TASKS["EV-023"])
        task["expect"]["history_in_request"] = True

        check = _checks(_run(task, tmp_path))["history_sent"]

        assert check["ok"] is False
        assert "prompt" in check["detail"]

    def test_history_check_passes_when_a_wrapper_supplies_history(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Proves the opt-in check can pass: a provider that prepends history."""

        def edit(request: ChatRequest) -> ChatRequest:
            if len(request.messages) == 1 and "What is my name?" in str(request.messages[0]):
                history = [
                    {"role": "user", "content": "My name is Ada."},
                    {"role": "assistant", "content": "Hello Ada."},
                ]
                return dataclasses.replace(request, messages=[*history, *request.messages])
            return request

        _patch_provider(monkeypatch, edit)
        task = copy.deepcopy(_TASKS["EV-023"])
        task["expect"]["history_in_request"] = True

        checks = _checks(_run(task, tmp_path))

        assert checks["history_sent"]["ok"] is True
        assert checks["followup_sent_last"]["ok"] is True

    def test_a_followup_request_that_loses_the_user_prompt_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def edit(request: ChatRequest) -> ChatRequest:
            return dataclasses.replace(request, messages=[])

        _patch_provider(monkeypatch, edit)
        checks = _checks(_run(_TASKS["EV-023"], tmp_path))

        assert checks["followup_sent_last"]["ok"] is False

    def test_a_retry_request_that_loses_the_prompt_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def edit(request: ChatRequest) -> ChatRequest:
            return dataclasses.replace(request, messages=[])

        _patch_provider(monkeypatch, edit)
        checks = _checks(_run(_TASKS["EV-022"], tmp_path))

        assert checks["retry_request_prompt"]["ok"] is False

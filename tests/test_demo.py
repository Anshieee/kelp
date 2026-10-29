"""KEL-9 demo tests: the six-beat run and the fixtures behind it.

The end-to-end test is the acceptance harness: one command, six beats, exit 0.
The rest pin the pieces that would otherwise fail quietly.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from demo.config import REPO_ROOT
from demo.fixtures import assets
from demo.fixtures.registry import build_registry
from demo.handlers import docs_answer, github_read, schedule_create, web_search
from demo.runner import PLANTED_SECRET, TOTAL_BUDGET_SECONDS
from skills.context import SkillContext
from skills.errors import SkillHandlerError


def _env_without_secrets() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if "KEY" not in key.upper() and "TOKEN" not in key.upper()
    }
    env["PYTHONPATH"] = str(REPO_ROOT)
    return env


def _run_demo(state: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "demo", "--state", str(state), "--json"],
        cwd=str(REPO_ROOT),
        env=_env_without_secrets(),
        capture_output=True,
        text=True,
        # The run's own budget is the contract; the harness only outlives it.
        timeout=int(TOTAL_BUDGET_SECONDS) + 60,
    )


# -- the run ---------------------------------------------------------------


def test_demo_end_to_end(tmp_path: Path) -> None:
    state = tmp_path / "state"
    result = _run_demo(state)
    assert result.returncode == 0, result.stderr[-2000:]

    summary = json.loads(result.stdout)
    assert summary["ok"] is True
    assert [beat["beat"] for beat in summary["beats"]] == [1, 2, 3, 4, 5, 6]
    assert all(beat["checks"] for beat in summary["beats"])

    receipts = state / "receipts"
    for number in range(1, 7):
        payload = json.loads((receipts / f"beat-{number}.json").read_text())
        assert payload["ok"] is True and payload["beat"] == number
    assert (receipts / "demo-report.md").is_file()

    # Artifacts and the scheduler state are on disk, not just in memory.
    artifact_files = sorted((state / "var" / "artifacts").iterdir())
    assert {path.suffix for path in artifact_files} == {".pdf", ".csv"}
    tasks = [
        json.loads(line)
        for line in (state / "var" / "scheduler" / "tasks.jsonl").read_text().splitlines()
    ]
    assert len(tasks) == 2
    assert all(task["status"] == "scheduled" for task in tasks)

    audit = [json.loads(line) for line in (state / "audit.jsonl").read_text().splitlines()]
    assert {"approval_decision", "approval_settled", "tool_executed"} <= {
        entry["event"] for entry in audit
    }
    decisions = {
        entry.get("decision") for entry in audit if entry["event"] == "approval_decision"
    }
    assert {"approved", "denied"} <= decisions, "both decisions were audited"
    # Every decision that promised a settlement has one: no dangling
    # awaiting_execution for any turn (beat 4 and beat 5 both settle).
    pending = {
        (entry.get("turn_id"), entry.get("request_id"))
        for entry in audit
        if entry["event"] == "approval_decision"
        and entry.get("outcome") == "awaiting_execution"
    }
    settled = {
        (entry.get("turn_id"), entry.get("request_id"))
        for entry in audit
        if entry["event"] == "approval_settled"
    }
    assert pending - settled == set(), "an approved write never settled its audit outcome"
    assert settled, "at least one approval settled"
    raw_audit = (state / "audit.jsonl").read_text()
    assert PLANTED_SECRET not in raw_audit
    assert "[redacted]" in raw_audit

    beat_six = json.loads((receipts / "beat-6.json").read_text())
    data = beat_six["data"]
    assert data["events_before_kill"] > 0
    assert data["resumed_turn"] != data["killed_turn"]
    assert 0 < data["partial_chars"] < data["answer_chars"]
    assert data["recovered_by"] == "demo.server --recover"
    assert any("new process" in check for check in beat_six["checks"])


def test_demo_exits_non_zero_when_a_beat_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI is the guard rail: a broken fixture must fail the run."""

    import demo.cli as cli

    def broken(self, beat):  # noqa: ANN001, ANN202 - signature matches the beat hook
        from demo.errors import DemoError

        raise DemoError(beat.number, "intentional failure")

    monkeypatch.setattr("demo.runner.Demo._beat_github", broken)
    code = cli.main(["--state", str(tmp_path / "state"), "--json"])
    assert code == 1


# -- fixtures and handlers -------------------------------------------------


def test_fixture_manifests_keep_the_real_contract(tmp_path: Path) -> None:
    registry = build_registry(tmp_path / "skills")
    assert set(registry.names()) == {
        "docs.answer",
        "github.read",
        "schedule.create",
        "web.search",
    }
    manifests = sorted((tmp_path / "skills").glob("*/manifest.json"))
    assert len(manifests) == 4
    for manifest in manifests:
        fixture = json.loads(manifest.read_text())
        builtin = json.loads(
            (REPO_ROOT / "skills" / "builtin" / manifest.parent.name / "manifest.json").read_text()
        )
        # Compare against the raw builtin JSON, so a drift in any contract field
        # (schemas, permissions, effects, scopes, timeout) fails here. Only the
        # entry point is allowed to differ, and it must point at a fixture.
        without_entry = {
            key: value for key, value in fixture.items() if key != "entry"
        }
        assert without_entry == {
            key: value for key, value in builtin.items() if key != "entry"
        }
        assert fixture["entry"] == {
            "module": f"demo.handlers.{fixture['name'].replace('.', '_')}",
            "callable": "run",
        }
        assert registry.get(fixture["name"]).requires_approval == builtin["permissions"]["requires_approval"]


def test_schedule_write_fails_closed_without_approval(tmp_path: Path) -> None:
    registry = build_registry(tmp_path / "skills")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def deny(_request):  # noqa: ANN001, ANN202
        return False

    envelope = asyncio.run(
        registry.invoke(
            "schedule.create",
            {
                "title": "Never runs",
                "prompt": "This must not be written.",
                "schedule": {"kind": "cron", "expr": "0 8 * * 1-5"},
                "timezone": "UTC",
            },
            approval=deny,
            workspace_root=workspace,
        )
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "approval_denied"
    assert not schedule_create.tasks_path(workspace).exists()


def test_docs_answer_sums_the_csv(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    artifacts = workspace / "var" / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "abcdefgh1234.csv").write_bytes(assets.csv_bytes())
    context = SkillContext("docs.answer", workspace)

    output = asyncio.run(
        docs_answer.run(
            {"artifact": "artifact:abcdefgh1234", "kind": "csv", "question": "What was the total revenue?"},
            context,
        )
    )
    assert "1,284,500" in output["answer"]
    assert output["artifact"]["rows"] == 3

    # Every citation names a real line of the attached file: locator and text
    # both come from the source rows, not from the answer string.
    rows = [line.split(",") for line in assets.csv_bytes().decode("utf-8").splitlines()]
    revenue = sum(int(row[2]) for row in rows[1:])
    assert f"{revenue:,}" in output["answer"]
    assert output["citations"] == [
        {"locator": f"row {number}", "text": ",".join(row)}
        for number, row in enumerate(rows[1:], start=2)
    ]
    assert output["citations"][0]["locator"] == "row 2"
    assert output["citations"][0]["text"].startswith("emea,Q3,412300")


def test_docs_answer_cites_the_matching_pdf_page(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    artifacts = workspace / "var" / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "abcdefgh1234.pdf").write_bytes(assets.pdf_bytes())
    context = SkillContext("docs.answer", workspace)

    output = asyncio.run(
        docs_answer.run(
            {"artifact": "artifact:abcdefgh1234", "kind": "pdf", "question": "What is the support response target?"},
            context,
        )
    )
    assert output["citations"][0]["locator"] == "page 2"
    assert "30 minutes" in output["answer"]
    assert output["artifact"]["pages"] == 2


def test_docs_answer_refuses_a_missing_artifact(tmp_path: Path) -> None:
    context = SkillContext("docs.answer", tmp_path / "ws")
    with pytest.raises(SkillHandlerError):
        asyncio.run(
            docs_answer.run(
                {"artifact": "artifact:missingmissing", "kind": "csv", "question": "total?"},
                context,
            )
        )


def test_github_fixture_is_read_only_and_repo_scoped(tmp_path: Path) -> None:
    context = SkillContext("github.read", tmp_path)

    output = asyncio.run(
        github_read.run(
            {"kind": "issue", "repo": "Anshieee/nebius-nvidia", "number": 12}, context
        )
    )
    assert output["issue"]["number"] == 12
    with pytest.raises(SkillHandlerError):
        asyncio.run(
            github_read.run({"kind": "issue", "repo": "someone/else", "number": 12}, context)
        )


def test_web_search_returns_recorded_results(tmp_path: Path) -> None:
    output = asyncio.run(
        web_search.run({"query": "nebius gpu"}, SkillContext("web.search", tmp_path))
    )
    assert output["backend"] == "fixture"
    assert len(output["results"]) == 3
    assert output["results"][0]["url"].startswith("https://")


def test_cron_next_run_lands_on_a_weekday() -> None:
    after = dt.datetime(2026, 10, 30, 12, 0, tzinfo=dt.timezone.utc)  # Friday
    assert after.weekday() == 4
    next_run = schedule_create.next_run_at("30 8 * * 1-5", "UTC", after=after)
    assert next_run == dt.datetime(2026, 11, 2, 8, 30, tzinfo=dt.timezone.utc)
    assert next_run.weekday() == 0


def test_cron_treats_seven_as_sunday() -> None:
    after = dt.datetime(2026, 10, 30, 12, 0, tzinfo=dt.timezone.utc)  # Friday
    next_run = schedule_create.next_run_at("0 9 * * 7", "UTC", after=after)
    assert next_run == dt.datetime(2026, 11, 1, 9, 0, tzinfo=dt.timezone.utc)
    assert next_run.weekday() == 6


def test_cron_rejects_an_impossible_expression() -> None:
    with pytest.raises(ValueError):
        schedule_create.next_run_at("0 0 31 2 *", "UTC")
    with pytest.raises(ValueError):
        schedule_create.next_run_at("0 0 * * *", "Mars/Olympus")

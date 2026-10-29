"""End-to-end offline eval runner tests: gates, reports, determinism, live gate."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from evals.cli import main
from evals.offline import OfflineViolation, block_network
from evals.runner import LiveModeError, run_suite


def test_offline_suite_passes_is_deterministic_and_writes_both_reports(
    tmp_path: Path,
) -> None:
    out = tmp_path / "evals"
    outcome = run_suite(out_dir=out)
    report = outcome.report

    assert outcome.exit_code == 0
    assert report["mode"] == "offline"
    assert report["network"] == "blocked"
    assert report["repeat"] == 3
    assert report["metrics"]["task_count"] == 26
    assert report["metrics"]["pass_rate"] == 1.0
    assert report["metrics"]["citation_precision"] == 1.0
    assert report["metrics"]["tool_accuracy"] == 1.0
    assert report["metrics"]["approval_correctness"] == 1.0
    assert report["metrics"]["error_rate"] == 0.0
    assert report["metrics"]["deterministic"] is True
    assert report["duration_ms"] < 60_000
    assert report["thresholds"]["passed"] is True
    assert report["comparison"]["previous_run"] is None
    assert all(task["deterministic"] for task in report["tasks"])
    assert all(task["passed"] for task in report["tasks"])

    assert outcome.report_path.name == "report.json"
    assert outcome.markdown_path.name == "report.md"
    payload = outcome.report_path.read_text(encoding="utf-8")
    markdown = outcome.markdown_path.read_text(encoding="utf-8")
    assert json.loads(payload)["run_id"] == report["run_id"]
    assert "# KEL-15 eval report — offline" in markdown
    # The fake sentinel is configured as a secret; it must not reach the report.
    assert "eval-sentinel-1234" not in payload
    assert "NEBIUS_API_KEY" not in payload


def test_second_run_compares_against_the_first(tmp_path: Path) -> None:
    out = tmp_path / "evals"
    first = run_suite(out_dir=out)
    second = run_suite(out_dir=out)

    assert second.exit_code == 0
    assert second.report["comparison"]["previous_run"] == first.report["run_id"]
    assert second.report["comparison"]["regressions"] == []
    deltas = second.report["comparison"]["deltas"]
    assert deltas["pass_rate"]["delta"] == 0.0
    assert deltas["approval_correctness"]["delta"] == 0.0


def test_regression_against_a_previous_run_exits_non_zero(tmp_path: Path) -> None:
    out = tmp_path / "evals"
    first = run_suite(out_dir=out)
    data = json.loads(first.report_path.read_text(encoding="utf-8"))
    data["metrics"]["pass_rate"] = 1.5  # impossible previous baseline -> regression
    first.report_path.write_text(json.dumps(data), encoding="utf-8")

    second = run_suite(out_dir=out)
    assert second.exit_code == 1
    regressions = {
        item["metric"] for item in second.report["comparison"]["regressions"]
    }
    assert "pass_rate" in regressions
    assert second.report["thresholds"]["passed"] is False


def test_threshold_violation_exits_non_zero(tmp_path: Path) -> None:
    thresholds = json.loads(
        (Path(__file__).resolve().parents[1] / "evals" / "thresholds.json").read_text(
            encoding="utf-8"
        )
    )
    thresholds["metrics"]["pass_rate"]["min"] = 1.1
    path = tmp_path / "thresholds.json"
    path.write_text(json.dumps(thresholds), encoding="utf-8")

    outcome = run_suite(out_dir=tmp_path / "evals", thresholds_path=path)
    assert outcome.exit_code == 1
    assert any(
        item["metric"] == "pass_rate"
        for item in outcome.report["thresholds"]["violations"]
    )


def test_live_mode_requires_the_nebius_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    with pytest.raises(LiveModeError) as info:
        run_suite(out_dir=tmp_path / "evals", live=True)
    assert "NEBIUS_API_KEY" in str(info.value)


def test_cli_live_without_key_exits_two_and_prints_no_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    code = main(["--live", "--out", str(tmp_path / "evals")])
    captured = capsys.readouterr()
    assert code == 2
    assert "NEBIUS_API_KEY" in captured.err
    assert "eval-sentinel-1234" not in captured.out + captured.err


def test_offline_guard_blocks_connections_and_name_resolution() -> None:
    with block_network():
        with pytest.raises(OfflineViolation):
            socket.create_connection(("example.com", 80))
        with pytest.raises(OfflineViolation):
            socket.getaddrinfo("example.com", 80)
    # Restored after the context.
    assert socket.create_connection is not None


def test_cli_list_does_not_run_the_suite(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--list"]) == 0
    captured = capsys.readouterr()
    assert "dataset 1.0.0 (26 tasks)" in captured.out
    assert "EV-001" in captured.out

"""Metric, threshold, comparison, and report-rendering tests."""

from __future__ import annotations

import json
from pathlib import Path

from evals.metrics import (
    compare_to_previous,
    compute_metrics,
    evaluate_thresholds,
    percentile,
)
from evals.report import render_markdown, write_reports


def _task(
    task_id: str,
    category: str,
    *,
    passed: bool = True,
    citations: list[str] | None = None,
    expected: list[str] | None = None,
    tool_score: float = 1.0,
    expected_calls: list[dict] | None = None,
    error: str | None = None,
) -> dict:
    return {
        "id": task_id,
        "category": category,
        "passed": passed,
        "citations": citations or [],
        "expected_citations": expected or [],
        "tool_score": tool_score,
        "expected_tool_calls": expected_calls or [],
        "duration_ms": 10,
        "durations_ms": [10, 20],
        "input_tokens": 10,
        "output_tokens": 5,
        "cost_usd": 0.001,
        "cost_known": True,
        "error": error,
        "deterministic": True,
    }


def test_percentile_is_nearest_rank() -> None:
    assert percentile([], 0.5) == 0.0
    assert percentile([4, 1, 3, 2], 0.5) == 2
    assert percentile([1], 0.95) == 1
    assert percentile([1, 2, 3, 4, 5], 0.95) == 5


def test_metrics_cover_citation_tool_approval_latency_and_cost() -> None:
    tasks = [
        _task("r1", "retrieval", citations=["row 2", "row 9"], expected=["row 2"]),
        _task("r2", "retrieval", citations=[], expected=[]),
        _task(
            "t1",
            "tool",
            expected_calls=[{"name": "web.search", "arguments": {"query": "x"}}],
        ),
        _task(
            "t2",
            "tool",
            passed=False,
            tool_score=0.5,
            expected_calls=[{"name": "github.read", "arguments": {}}],
        ),
        _task(
            "a1",
            "approval",
            expected_calls=[{"name": "schedule.create", "arguments": {}}],
        ),
        _task(
            "a2",
            "approval",
            passed=False,
            tool_score=0.0,
            expected_calls=[{"name": "schedule.create", "arguments": {}}],
        ),
        _task("x1", "multi_turn", passed=False, error="boom"),
    ]
    metrics = compute_metrics(tasks)
    assert metrics["task_count"] == 7
    assert metrics["passed"] == 4
    assert metrics["pass_rate"] == round(4 / 7, 6)
    assert metrics["citation_precision"] == 0.5
    assert metrics["citation_recall"] == 1.0
    assert metrics["tool_accuracy"] == round((1.0 + 0.5 + 1.0 + 0.0) / 4, 6)
    assert metrics["approval_correctness"] == 0.5
    assert metrics["error_rate"] == round(1 / 7, 6)
    assert metrics["p50_latency_ms"] == 10
    assert metrics["p95_latency_ms"] == 20
    assert metrics["tokens_total"] == 105
    assert metrics["cost_usd_total"] == 0.007
    assert metrics["categories"]["tool"]["pass_rate"] == 0.5
    assert metrics["deterministic"] is True


def test_thresholds_report_each_violation() -> None:
    metrics = {
        "task_count": 5,
        "pass_rate": 0.5,
        "citation_precision": 0.5,
        "tool_accuracy": 0.5,
        "approval_correctness": 0.5,
        "error_rate": 0.2,
        "p95_latency_ms": 9000,
        "deterministic": False,
    }
    thresholds = {
        "metrics": {
            "pass_rate": {"min": 0.9},
            "error_rate": {"max": 0.0},
            "p95_latency_ms": {"max": 1000},
        },
        "min_task_count": 20,
        "offline_max_duration_ms": 100,
        "require_deterministic": True,
    }
    violations = evaluate_thresholds(
        metrics, thresholds, duration_ms=101, offline=True
    )
    names = {item["metric"] for item in violations}
    assert {
        "pass_rate",
        "error_rate",
        "p95_latency_ms",
        "task_count",
        "offline_duration_ms",
        "deterministic",
    } <= names
    assert evaluate_thresholds(
        metrics, thresholds, duration_ms=101, offline=False
    ) == [item for item in violations if item["metric"] not in {"offline_duration_ms", "deterministic"}]


def test_comparison_detects_regressions_but_respects_tolerance() -> None:
    previous = {
        "run_id": "run-1",
        "pass_rate": 0.95,
        "citation_precision": 0.96,
        "tool_accuracy": 0.9,
        "approval_correctness": 1.0,
        "error_rate": 0.0,
        "p95_latency_ms": 100.0,
    }
    current = {
        "run_id": "run-2",
        "pass_rate": 0.90,
        "citation_precision": 0.95,
        "tool_accuracy": 0.9,
        "approval_correctness": 1.0,
        "error_rate": 0.1,
        "p95_latency_ms": 50.0,
    }
    thresholds = {
        "regression_tolerance": {
            "pass_rate": 0.0,
            "citation_precision": 0.02,
            "tool_accuracy": 0.0,
            "approval_correctness": 0.0,
            "error_rate": 0.0,
            "p95_latency_ms": 500,
        }
    }
    result = compare_to_previous(current, previous, thresholds)
    assert result["previous_run"] == "run-1"
    regressed = {item["metric"] for item in result["regressions"]}
    assert regressed == {"pass_rate", "error_rate"}
    assert result["deltas"]["citation_precision"]["delta"] == round(0.95 - 0.96, 6)


def test_no_previous_run_is_a_baseline() -> None:
    result = compare_to_previous({"pass_rate": 1.0}, None, {})
    assert result["previous_run"] is None
    assert result["regressions"] == []
    assert "baseline" in result["note"]


def test_markdown_and_json_reports_render(tmp_path: Path) -> None:
    report = {
        "run_id": "20260101T000000000000Z-offline",
        "mode": "offline",
        "provider": {"label": "recorded://synthetic", "model": "m"},
        "network": "blocked",
        "repeat": 3,
        "duration_ms": 12,
        "dataset": {"version": "1.0.0"},
        "metrics": {
            "task_count": 1,
            "pass_rate": 1.0,
            "citation_precision": 1.0,
            "citation_recall": 1.0,
            "tool_accuracy": 1.0,
            "approval_correctness": 1.0,
            "error_rate": 0.0,
            "p50_latency_ms": 2,
            "p95_latency_ms": 3,
            "tokens_total": 10,
            "cost_usd_total": 0.0,
            "deterministic": True,
            "categories": {
                "retrieval": {"total": 1, "passed": 1, "pass_rate": 1.0}
            },
        },
        "thresholds": {"rules": {}, "violations": [], "gating": True, "passed": True},
        "comparison": {"previous_run": None, "note": "baseline", "regressions": []},
        "tasks": [{"id": "EV-001", "category": "retrieval", "passed": True}],
    }
    markdown = render_markdown(report)
    assert "# KEL-15 eval report" in markdown
    assert "recorded://synthetic" in markdown
    assert "Deterministic: yes" in markdown
    json_path, md_path = write_reports(tmp_path, report["run_id"], report)
    assert json.loads(json_path.read_text())["run_id"] == report["run_id"]
    assert md_path.read_text() == markdown

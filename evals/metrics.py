"""Metric aggregation, threshold gates, and run-to-run regression checks."""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

__all__ = [
    "compare_to_previous",
    "compute_metrics",
    "evaluate_thresholds",
    "percentile",
]

_HIGHER_IS_BETTER = {
    "pass_rate": True,
    "citation_precision": True,
    "citation_recall": True,
    "tool_accuracy": True,
    "approval_correctness": True,
    "error_rate": False,
    "p50_latency_ms": False,
    "p95_latency_ms": False,
}

_COMPARED = (
    "pass_rate",
    "citation_precision",
    "tool_accuracy",
    "approval_correctness",
    "error_rate",
    "p95_latency_ms",
)


def percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile: deterministic and free of interpolation."""

    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    rank = max(1, min(len(ordered), int(math.ceil(fraction * len(ordered)))))
    return ordered[rank - 1]


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def compute_metrics(tasks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    total = len(tasks)
    passed = [task for task in tasks if task.get("passed")]
    durations = [float(value) for task in tasks for value in task.get("durations_ms") or []]
    if not durations:
        durations = [float(task.get("duration_ms") or 0) for task in tasks]

    retrieval = [task for task in tasks if task.get("category") == "retrieval"]
    citation_returned = 0
    citation_hits = 0
    expected_total = 0
    expected_hits = 0
    for task in retrieval:
        returned = [str(item) for item in task.get("citations") or []]
        expected = {str(item) for item in task.get("expected_citations") or []}
        citation_returned += len(returned)
        for item in returned:
            if item in expected:
                citation_hits += 1
        expected_total += len(expected)
        expected_hits += len(expected & set(returned))
    if citation_returned:
        citation_precision = citation_hits / citation_returned
    elif expected_total:
        citation_precision = 0.0
    else:
        citation_precision = 1.0
    citation_recall = expected_hits / expected_total if expected_total else 1.0

    tool_tasks = [
        task
        for task in tasks
        if task.get("category") in {"tool", "approval"}
        and task.get("expected_tool_calls")
    ]
    tool_accuracy = _mean([float(task.get("tool_score") or 0.0) for task in tool_tasks])

    approval_tasks = [task for task in tasks if task.get("category") == "approval"]
    approval_correctness = _mean(
        [1.0 if task.get("passed") else 0.0 for task in approval_tasks]
    )

    errors = [task for task in tasks if task.get("error")]
    input_tokens = sum(int(task.get("input_tokens") or 0) for task in tasks)
    output_tokens = sum(int(task.get("output_tokens") or 0) for task in tasks)
    cost = sum(float(task.get("cost_usd") or 0.0) for task in tasks)
    cost_known = all(bool(task.get("cost_known", True)) for task in tasks)

    categories: dict[str, dict[str, Any]] = {}
    for category in sorted({str(task.get("category")) for task in tasks}):
        group = [task for task in tasks if task.get("category") == category]
        group_passed = [task for task in group if task.get("passed")]
        categories[category] = {
            "total": len(group),
            "passed": len(group_passed),
            "pass_rate": round(len(group_passed) / len(group), 6) if group else 0.0,
        }

    deterministic = all(bool(task.get("deterministic", True)) for task in tasks)
    return {
        "task_count": total,
        "passed": len(passed),
        "pass_rate": round(len(passed) / total, 6) if total else 0.0,
        "citation_precision": round(citation_precision, 6),
        "citation_recall": round(citation_recall, 6),
        "tool_accuracy": round(tool_accuracy, 6),
        "approval_correctness": round(approval_correctness, 6),
        "error_rate": round(len(errors) / total, 6) if total else 0.0,
        "p50_latency_ms": round(percentile(durations, 0.50), 3),
        "p95_latency_ms": round(percentile(durations, 0.95), 3),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "tokens_total": input_tokens + output_tokens,
        "cost_usd_total": round(cost, 8),
        "cost_known": cost_known,
        "deterministic": deterministic,
        "categories": categories,
    }


def evaluate_thresholds(
    metrics: Mapping[str, Any],
    thresholds: Mapping[str, Any],
    *,
    duration_ms: int,
    offline: bool,
) -> list[dict[str, Any]]:
    """Return every gate violation; an empty list means the run passes."""

    violations: list[dict[str, Any]] = []
    rules = thresholds.get("metrics") if isinstance(thresholds.get("metrics"), Mapping) else {}
    for name, rule in rules.items():
        if not isinstance(rule, Mapping):
            continue
        value = metrics.get(name)
        if value is None:
            violations.append(
                {"metric": name, "value": None, "rule": dict(rule), "message": "metric missing"}
            )
            continue
        if "min" in rule and float(value) < float(rule["min"]):
            violations.append(
                {
                    "metric": name,
                    "value": value,
                    "rule": dict(rule),
                    "message": f"{value} < min {rule['min']}",
                }
            )
        if "max" in rule and float(value) > float(rule["max"]):
            violations.append(
                {
                    "metric": name,
                    "value": value,
                    "rule": dict(rule),
                    "message": f"{value} > max {rule['max']}",
                }
            )

    task_count = int(metrics.get("task_count") or 0)
    if "min_task_count" in thresholds and task_count < int(thresholds["min_task_count"]):
        violations.append(
            {
                "metric": "task_count",
                "value": task_count,
                "rule": {"min": thresholds["min_task_count"]},
                "message": f"{task_count} < min {thresholds['min_task_count']}",
            }
        )
    if "max_task_count" in thresholds and task_count > int(thresholds["max_task_count"]):
        violations.append(
            {
                "metric": "task_count",
                "value": task_count,
                "rule": {"max": thresholds["max_task_count"]},
                "message": f"{task_count} > max {thresholds['max_task_count']}",
            }
        )

    if offline:
        max_duration = thresholds.get("offline_max_duration_ms")
        if isinstance(max_duration, (int, float)) and duration_ms > max_duration:
            violations.append(
                {
                    "metric": "offline_duration_ms",
                    "value": duration_ms,
                    "rule": {"max": max_duration},
                    "message": f"{duration_ms}ms > max {max_duration}ms",
                }
            )
        if thresholds.get("require_deterministic", True) and not metrics.get("deterministic"):
            violations.append(
                {
                    "metric": "deterministic",
                    "value": False,
                    "rule": {"required": True},
                    "message": "repeated runs were not identical",
                }
            )
    return violations


def compare_to_previous(
    current: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
    thresholds: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare the gate metrics with the most recent matching run."""

    if not previous:
        return {
            "previous_run": None,
            "deltas": {},
            "regressions": [],
            "note": "no previous matching run; this run is the baseline",
        }
    tolerance = (
        thresholds.get("regression_tolerance")
        if isinstance(thresholds.get("regression_tolerance"), Mapping)
        else {}
    )
    deltas: dict[str, Any] = {}
    regressions: list[dict[str, Any]] = []
    for name in _COMPARED:
        before = previous.get(name)
        after = current.get(name)
        if before is None or after is None:
            continue
        try:
            delta = round(float(after) - float(before), 6)
        except (TypeError, ValueError):
            continue
        deltas[name] = {"previous": before, "current": after, "delta": delta}
        limit = float(tolerance.get(name, 0.0) or 0.0)
        higher_is_better = _HIGHER_IS_BETTER.get(name, True)
        regressed = delta < -limit if higher_is_better else delta > limit
        if regressed:
            regressions.append(
                {
                    "metric": name,
                    "previous": before,
                    "current": after,
                    "delta": delta,
                    "tolerance": limit,
                    "direction": "higher-is-better" if higher_is_better else "lower-is-better",
                }
            )
    return {
        "previous_run": previous.get("run_id"),
        "deltas": deltas,
        "regressions": regressions,
        "note": "gate metrics compared with the previous matching run",
    }

"""JSON and Markdown report writing plus previous-run discovery."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

__all__ = ["find_previous", "render_markdown", "write_reports"]


def _metric(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _percent(value: Any) -> str:
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return str(value)


def _cell(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, sort_keys=True, default=str)
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(report: Mapping[str, Any]) -> str:
    metrics = report.get("metrics") or {}
    thresholds = report.get("thresholds") or {}
    comparison = report.get("comparison") or {}
    lines: list[str] = []
    mode = str(report.get("mode") or "offline")
    lines.append(f"# KEL-15 eval report — {mode}")
    lines.append("")
    lines.append(f"- Run: `{report.get('run_id')}`")
    lines.append(
        f"- Dataset: `{(report.get('dataset') or {}).get('version')}` "
        f"({metrics.get('task_count', 0)} tasks)"
    )
    lines.append(f"- Mode: `{mode}`")
    lines.append(f"- Provider: `{(report.get('provider') or {}).get('label')}`")
    lines.append(f"- Network: `{report.get('network')}`")
    lines.append(f"- Repeat: {report.get('repeat')}x")
    lines.append(f"- Deterministic: {_metric(metrics.get('deterministic'))}")
    lines.append(f"- Duration: {report.get('duration_ms', 0)} ms")
    lines.append("")

    lines.append("## Metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | ---: |")
    for name in (
        "pass_rate",
        "citation_precision",
        "citation_recall",
        "tool_accuracy",
        "approval_correctness",
        "error_rate",
        "p50_latency_ms",
        "p95_latency_ms",
        "tokens_total",
        "cost_usd_total",
    ):
        value = metrics.get(name)
        if name.endswith("_rate") or name in {
            "citation_precision",
            "citation_recall",
            "tool_accuracy",
            "approval_correctness",
        }:
            value = _percent(value)
        elif name.endswith("_latency_ms"):
            value = f"{value} ms"
        elif name == "cost_usd_total":
            value = f"${value}"
        lines.append(f"| `{name}` | {_cell(value)} |")
    lines.append("")

    categories = metrics.get("categories") or {}
    if categories:
        lines.append("## Categories")
        lines.append("")
        lines.append("| Category | Passed | Total | Pass rate |")
        lines.append("| --- | ---: | ---: | ---: |")
        for name, row in categories.items():
            lines.append(
                f"| `{name}` | {row.get('passed')} | {row.get('total')} | "
                f"{_percent(row.get('pass_rate'))} |"
            )
        lines.append("")

    failed = [task for task in report.get("tasks") or [] if not task.get("passed")]
    lines.append("## Task results")
    lines.append("")
    if failed:
        lines.append("| Task | Category | Failed checks | Error |")
        lines.append("| --- | --- | --- | --- |")
        for task in failed:
            checks = [
                check.get("name")
                for check in task.get("checks") or []
                if not check.get("ok")
            ]
            error = task.get("error") or ""
            lines.append(
                f"| `{task.get('id')}` | `{task.get('category')}` | "
                f"{_cell(', '.join(checks) or '-')} | {_cell(error or '-')} |"
            )
    else:
        lines.append("All tasks passed.")
    lines.append("")

    lines.append("## Gates")
    lines.append("")
    lines.append("| Gate | Value | Rule | Result |")
    lines.append("| --- | --- | --- | --- |")
    for name, rule in (thresholds.get("rules") or {}).items():
        lines.append(f"| `{name}` | {_metric(rule.get('value'))} | {_cell(rule.get('rule'))} | {rule.get('status')} |")
    for violation in thresholds.get("violations") or []:
        lines.append(
            f"| `{violation.get('metric')}` | {_metric(violation.get('value'))} | "
            f"{_cell(violation.get('rule'))} | FAIL: {_cell(violation.get('message'))} |"
        )
    lines.append("")

    lines.append("## Comparison")
    lines.append("")
    previous = comparison.get("previous_run")
    if previous:
        lines.append(f"Previous run: `{previous}`")
        lines.append("")
        lines.append("| Metric | Previous | Current | Delta |")
        lines.append("| --- | ---: | ---: | ---: |")
        for name, row in (comparison.get("deltas") or {}).items():
            lines.append(
                f"| `{name}` | {_metric(row.get('previous'))} | "
                f"{_metric(row.get('current'))} | {_metric(row.get('delta'))} |"
            )
        regressions = comparison.get("regressions") or []
        lines.append("")
        if regressions:
            lines.append("**Regressions:**")
            lines.append("")
            for row in regressions:
                lines.append(
                    f"- `{row.get('metric')}`: {_metric(row.get('previous'))} -> "
                    f"{_metric(row.get('current'))} (tolerance {_metric(row.get('tolerance'))})"
                )
        else:
            lines.append("No regressions against the previous matching run.")
    else:
        lines.append(str(comparison.get("note") or "No previous matching run; this is the baseline."))
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "Offline runs replay deterministic provider transcripts and block network "
        "access. Live runs are opt-in, labelled, and do not gate the offline result."
    )
    lines.append("")
    return "\n".join(lines)


def write_reports(
    out_dir: str | Path,
    run_id: str,
    report: Mapping[str, Any],
) -> tuple[Path, Path]:
    base = Path(out_dir)
    run_dir = base / run_id
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    json_path = run_dir / "report.json"
    md_path = run_dir / "report.md"
    payload = json.dumps(report, indent=2, sort_keys=True, default=str) + "\n"
    json_path.write_text(payload, encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    for path in (json_path, md_path):
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return json_path, md_path


def find_previous(
    out_dir: str | Path,
    *,
    mode: str,
    dataset_version: str,
) -> dict[str, Any] | None:
    """Most recent report with the same mode and dataset version."""

    base = Path(out_dir)
    if not base.exists():
        return None
    candidates: list[tuple[str, Path]] = []
    for path in base.glob("*/report.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, Mapping):
            continue
        if data.get("mode") != mode:
            continue
        dataset = data.get("dataset")
        if not isinstance(dataset, Mapping) or dataset.get("version") != dataset_version:
            continue
        run_id = data.get("run_id")
        if isinstance(run_id, str) and run_id:
            candidates.append((run_id, path))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    for _run_id, path in reversed(candidates):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, Mapping):
            return dict(data)
    return None

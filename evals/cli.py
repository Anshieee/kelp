"""Command-line entry point: ``python -m evals`` / ``make eval``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .dataset import DEFAULT_DATASET, DEFAULT_FIXTURES, DEFAULT_THRESHOLDS, DatasetError, load_dataset
from .runner import LiveModeError, ThresholdError, run_suite

__all__ = ["main"]


def _relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def _percent(value: object) -> str:
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return str(value)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals",
        description="Offline KEL-15 evaluation harness (use --live for the opt-in provider run).",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="use the real Nebius Token Factory provider; requires NEBIUS_API_KEY",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=None,
        help="run each task N times (offline default 3, live default 1)",
    )
    parser.add_argument(
        "--out",
        default="var/evals",
        help="report base directory (default: var/evals)",
    )
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET), help="task dataset JSON")
    parser.add_argument("--fixtures", default=str(DEFAULT_FIXTURES), help="recorded provider fixture JSON")
    parser.add_argument("--thresholds", default=str(DEFAULT_THRESHOLDS), help="threshold JSON")
    parser.add_argument("--list", action="store_true", help="list dataset tasks and exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    try:
        dataset = load_dataset(args.dataset)
    except DatasetError as exc:
        print(f"eval: {exc}", file=sys.stderr)
        return 2

    if args.list:
        print(f"dataset {dataset.version} ({len(dataset.tasks)} tasks)")
        for task in dataset.tasks:
            print(f"{task['id']}\t{task['category']}\t{task['scenario']}\t{task['name']}")
        return 0

    try:
        outcome = run_suite(
            dataset_path=args.dataset,
            fixtures_path=args.fixtures,
            thresholds_path=args.thresholds,
            out_dir=args.out,
            live=args.live,
            repeat=args.repeat,
        )
    except (DatasetError, ThresholdError, LiveModeError, ValueError) as exc:
        print(f"eval: {exc}", file=sys.stderr)
        return 2

    report = outcome.report
    metrics = report["metrics"]
    mode = report["mode"]
    gating = "gating" if report["thresholds"]["gating"] else "non-gating"
    print(
        f"EVAL {mode} ({gating}) dataset={report['dataset']['version']} "
        f"tasks={metrics['task_count']} repeat={report['repeat']}"
    )
    print(
        "pass_rate={pass_rate} citation_precision={citation_precision} "
        "tool_accuracy={tool_accuracy} approval_correctness={approval_correctness} "
        "error_rate={error_rate}".format(
            pass_rate=_percent(metrics.get("pass_rate")),
            citation_precision=_percent(metrics.get("citation_precision")),
            tool_accuracy=_percent(metrics.get("tool_accuracy")),
            approval_correctness=_percent(metrics.get("approval_correctness")),
            error_rate=_percent(metrics.get("error_rate")),
        )
    )
    print(
        f"p50={metrics.get('p50_latency_ms')}ms p95={metrics.get('p95_latency_ms')}ms "
        f"tokens={metrics.get('tokens_total')} cost=${metrics.get('cost_usd_total')} "
        f"deterministic={'yes' if metrics.get('deterministic') else 'no'}"
    )
    violations = report["thresholds"].get("violations") or []
    regressions = report["comparison"].get("regressions") or []
    gates_ok = not violations and not regressions
    print(
        f"thresholds={'pass' if not violations else 'fail'} "
        f"regressions={len(regressions)} gates={'pass' if gates_ok else 'fail'} "
        f"duration={report['duration_ms']}ms"
    )
    print(f"report={_relative(outcome.report_path)}")
    if not report["thresholds"].get("gating"):
        print("live mode is labelled and does not gate the offline result")
    return outcome.exit_code


if __name__ == "__main__":  # pragma: no cover - exercised through __main__
    raise SystemExit(main())

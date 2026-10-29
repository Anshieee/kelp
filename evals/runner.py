"""Offline/live suite runner and report assembly."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import tempfile
import time
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

from chat.metrics import Metrics
from chat.service import ChatService
from chat.store import ChatStore
from kernel.config import KernelConfig
from kernel.provider import ChatRequest, TokenFactoryClient
from kernel.usage import InMemoryUsageStore, UsageRecord
from openshell.audit import AuditLog, SecretScrubber

from .dataset import (
    DEFAULT_DATASET,
    DEFAULT_FIXTURES,
    DEFAULT_THRESHOLDS,
    PROVIDER_SCENARIOS,
    Dataset,
    DatasetError,
    SCENARIOS,
    load_dataset,
)
from .fixtures import (
    FixtureError,
    RecordedProvider,
    load_recorded_fixtures,
    substitute,
    write_artifact,
)
from .metrics import compare_to_previous, compute_metrics, evaluate_thresholds
from .offline import block_network
from .report import find_previous, write_reports
from .results import Observation, TaskResult
from .scenarios import ScenarioContext, run_retrieval, run_scenario
from .tools import (
    EvalToolExecutor,
    ToolRecorder,
    build_tool_specs,
    registry,
)

__all__ = ["LiveModeError", "SuiteOutcome", "ThresholdError", "run_suite"]

DEFAULT_MODEL = "nvidia/Nemotron-3_5-Super-VL"
_MODE_OFFLINE = "offline"
_MODE_LIVE = "live"
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{4,}"),
)


class LiveModeError(RuntimeError):
    """``--live`` was requested without the one allowed credential."""


class ThresholdError(ValueError):
    """The threshold file is missing or malformed."""


@dataclass
class SuiteOutcome:
    exit_code: int
    report_path: Path
    markdown_path: Path
    report: dict[str, Any]


class CountingProvider:
    """Record requests so multi-turn/retry tests can assert call counts."""

    def __init__(self, inner: Any):
        self._inner = inner
        self.requests: list[ChatRequest] = []

    async def stream(self, request: ChatRequest) -> AsyncIterator[Any]:
        self.requests.append(request)
        async for event in self._inner.stream(request):
            yield event


def _load_thresholds(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ThresholdError(f"thresholds are unreadable: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ThresholdError(f"thresholds are not valid JSON: {path}") from exc
    if not isinstance(data, Mapping):
        raise ThresholdError("thresholds must be a JSON object")
    if not isinstance(data.get("metrics", {}), Mapping):
        raise ThresholdError("thresholds.metrics must be an object")
    return dict(data)


def _sha256(path: str | Path) -> str:
    try:
        data = Path(path).read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(data).hexdigest()


def _scrub_text(text: str, secrets: Mapping[str, str], workspaces: list[Path]) -> str:
    scrubber = SecretScrubber(secrets.values())
    value = scrubber.scrub_text(text)
    for workspace in workspaces:
        value = value.replace(str(workspace), "<workspace>")
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub("[redacted]", value)
    return value[:500]


def _scrub_value(
    value: Any, secrets: Mapping[str, str], workspaces: list[Path]
) -> Any:
    if isinstance(value, str):
        return _scrub_text(value, secrets, workspaces)
    if isinstance(value, Mapping):
        return {
            str(key): _scrub_value(item, secrets, workspaces)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_scrub_value(item, secrets, workspaces) for item in value]
    return value


def _score_tool_calls(
    expected: list[Mapping[str, Any]],
    observed: list[Mapping[str, Any]],
) -> tuple[float, int]:
    if not expected:
        return 1.0, 0
    used = [False] * len(observed)
    matched = 0
    for wanted in expected:
        name = str(wanted.get("name") or "")
        arguments = wanted.get("arguments") if isinstance(wanted.get("arguments"), Mapping) else {}
        for index, call in enumerate(observed):
            if used[index] or str(call.get("name")) != name:
                continue
            actual = call.get("arguments") if isinstance(call.get("arguments"), Mapping) else {}
            if all(actual.get(key) == value for key, value in arguments.items()):
                used[index] = True
                matched += 1
                break
    denominator = max(len(expected), len(observed))
    return (matched / denominator if denominator else 1.0), matched


def _usage_totals(records: list[UsageRecord]) -> tuple[int, int, float, bool]:
    input_tokens = sum(int(record.input_tokens) for record in records)
    output_tokens = sum(int(record.output_tokens) for record in records)
    cost = sum(float(record.estimated_cost_usd or 0.0) for record in records)
    cost_known = all(bool(record.cost_known) for record in records) if records else True
    return input_tokens, output_tokens, cost, cost_known


def _signature(
    observation: Observation,
    observed_calls: list[Mapping[str, Any]],
    outputs: list[Mapping[str, Any]],
    records: list[UsageRecord],
) -> str:
    input_tokens, output_tokens, cost, cost_known = _usage_totals(records)
    payload = {
        "checks": [[check.name, bool(check.ok)] for check in observation.checks],
        "citations": observation.citations,
        "approval": observation.approval,
        "calls": [
            {"name": call.get("name"), "arguments": call.get("arguments")}
            for call in observed_calls
        ],
        "outputs": outputs,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": round(cost, 8),
        "cost_known": cost_known,
        "error": bool(observation.error),
    }
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _task_setup(task: Mapping[str, Any], workspace: Path) -> tuple[str | None, list[dict[str, Any]]]:
    handle: str | None = None
    if task.get("doc"):
        handle, _kind = write_artifact(workspace, task["doc"], str(task["artifact_token"]))
    expected = (task.get("expect") or {}).get("tool_calls") or []
    expected_calls = [
        substitute(dict(call), {"artifact": handle or ""})
        for call in expected
        if isinstance(call, Mapping)
    ]
    return handle, expected_calls


async def _run_sample(
    task: Mapping[str, Any],
    workspace: Path,
    *,
    provider: Any | None,
    fixture: Mapping[str, Any] | None,
    handle: str | None,
    expected_calls: list[dict[str, Any]],
    live: bool,
    model: str,
) -> tuple[Observation, list[UsageRecord], list[dict[str, Any]], list[dict[str, Any]], float]:
    scenario = str(task["scenario"])
    if scenario == "retrieval":
        observation = await run_retrieval(task, workspace, handle)
        return observation, [], [], [], 1.0

    store = ChatStore(workspace / "chat.db")
    usage = InMemoryUsageStore()
    recorder = ToolRecorder()
    secrets = (
        {"EVAL_TOKEN": str(task["secret_sentinel"])}
        if isinstance(task.get("secret_sentinel"), str)
        else {}
    )
    audit = AuditLog(workspace / "audit.jsonl", secret_values=secrets.values())

    def factory(hook: Any) -> EvalToolExecutor:
        return EvalToolExecutor(workspace, recorder, secrets=secrets).bind(hook)

    service = ChatService(
        store=store,
        provider=provider,
        registry=registry(),
        usage_store=usage,
        model=model,
        secrets=secrets,
        workspace_root=str(workspace),
        tool_executor_factory=factory,
        tools=build_tool_specs(),
        audit=audit,
        metrics=Metrics(),
    )
    context = ScenarioContext(
        task=task,
        workspace=workspace,
        store=store,
        service=service,
        recorder=recorder,
        provider=provider,
        handle=handle,
        expected_tool_calls=expected_calls,
        live=live,
        fixture=fixture,
    )
    try:
        observation = await run_scenario(scenario, context)
    finally:
        try:
            await service.aclose(timeout=10.0)
        finally:
            store.close()
    score, _matched = _score_tool_calls(expected_calls, recorder.calls)
    return observation, list(usage.records), recorder.calls, recorder.outputs, score


def _runtime_task(task: Mapping[str, Any], handle: str | None, live: bool) -> dict[str, Any]:
    runtime = dict(task)
    if live and handle:
        runtime["prompt"] = f"{task['prompt']} The artifact handle is {handle}."
    return runtime


async def _run_task(
    task: Mapping[str, Any],
    *,
    fixtures: Mapping[str, Any],
    live: bool,
    model: str,
    repeat: int,
    live_client: TokenFactoryClient | None,
    work_root: Path,
) -> dict[str, Any]:
    task_id = str(task["id"])
    scenario = str(task["scenario"])
    expected_citation_list = [
        str(item) for item in (task.get("expect") or {}).get("citation_locators") or []
    ]
    samples: list[
        tuple[Observation, list[UsageRecord], list[dict[str, Any]], list[dict[str, Any]], float, int]
    ] = []
    signatures: list[str] = []
    durations: list[int] = []

    for index in range(repeat):
        workspace = work_root / f"{task_id}-{index}"
        workspace.mkdir(parents=True, exist_ok=True)
        handle, expected_calls = _task_setup(task, workspace)
        runtime_task = _runtime_task(task, handle, live)
        fixture_record: Mapping[str, Any] | None = None
        provider: Any | None = None
        if scenario in PROVIDER_SCENARIOS:
            if live:
                if live_client is None:
                    raise LiveModeError("live client was not created")
                provider = CountingProvider(live_client)
            else:
                raw_record = (fixtures.get("tasks") or {}).get(task_id)
                if not isinstance(raw_record, Mapping):
                    raise DatasetError(f"{task_id}: no recorded provider fixture")
                fixture_record = substitute(
                    dict(raw_record), {"artifact": handle or ""}
                )
                provider = CountingProvider(
                    RecordedProvider(task_id, fixture_record)
                )
        started = time.perf_counter()
        try:
            observation, records, calls, outputs, score = await _run_sample(
                runtime_task,
                workspace,
                provider=provider,
                fixture=fixture_record,
                handle=handle,
                expected_calls=expected_calls,
                live=live,
                model=model,
            )
        except Exception as exc:  # noqa: BLE001 - a broken task is an eval failure
            observation = Observation(error=f"{type(exc).__name__}: {exc}")
            records = []
            calls = []
            outputs = []
            score = 0.0
        duration_ms = max(0, int(round((time.perf_counter() - started) * 1000)))
        durations.append(duration_ms)

        if expected_calls:
            _score, matched = _score_tool_calls(expected_calls, calls)
            observation.add(
                "tool_plan",
                score >= 1.0 and len(calls) == len(expected_calls),
                f"{matched}/{len(expected_calls)} expected, {len(calls)} observed",
            )

        input_tokens, output_tokens, cost, cost_known = _usage_totals(records)
        signatures.append(_signature(observation, calls, outputs, records))
        samples.append(
            (
                observation,
                records,
                calls,
                outputs,
                score,
                duration_ms,
            )
        )
        shutil.rmtree(workspace, ignore_errors=True)

    first = samples[0]
    observation, records, calls, _outputs, score, first_duration = first
    input_tokens, output_tokens, cost, cost_known = _usage_totals(records)
    deterministic = len(set(signatures)) == 1
    passed = observation.error is None and all(check.ok for check in observation.checks)
    checks = [check.to_dict() for check in observation.checks]
    result = TaskResult(
        task_id=task_id,
        category=str(task["category"]),
        name=str(task["name"]),
        passed=passed,
        checks=checks,
        duration_ms=first_duration,
        durations_ms=durations,
        citations=list(observation.citations),
        expected_citations=expected_citation_list,
        tool_calls=[dict(call) for call in calls],
        expected_tool_calls=[dict(call) for call in expected_calls],
        tool_score=score,
        approval=dict(observation.approval),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
        cost_known=cost_known,
        error=observation.error,
        deterministic=deterministic,
        repeat_count=repeat,
    )
    payload = result.to_dict()
    secrets = (
        {"EVAL_TOKEN": str(task["secret_sentinel"])}
        if isinstance(task.get("secret_sentinel"), str)
        else {}
    )
    return _scrub_value(payload, secrets, [work_root / f"{task_id}-{i}" for i in range(repeat)])


async def _run_all(
    dataset: Dataset,
    fixtures: Mapping[str, Any],
    *,
    live: bool,
    model: str,
    repeat: int,
    live_client: TokenFactoryClient | None,
    work_root: Path,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    try:
        for task in dataset.tasks:
            if str(task["scenario"]) not in SCENARIOS:
                raise DatasetError(
                    f"{task['id']}: scenario {task['scenario']!r} is not implemented"
                )
            results.append(
                await _run_task(
                    task,
                    fixtures=fixtures,
                    live=live,
                    model=model,
                    repeat=repeat,
                    live_client=live_client,
                    work_root=work_root,
                )
            )
    finally:
        if live_client is not None:
            await live_client.aclose()
    return results


def _new_run_id(out_dir: Path, mode: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    base = f"{stamp}-{mode}"
    candidate = base
    suffix = 2
    while (out_dir / candidate).exists():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _threshold_rules(
    metrics: Mapping[str, Any],
    thresholds: Mapping[str, Any],
    duration_ms: int,
    violations: list[dict[str, Any]],
) -> dict[str, Any]:
    failed = {str(item.get("metric")) for item in violations}
    rules: dict[str, Any] = {}
    for name, rule in (thresholds.get("metrics") or {}).items():
        rules[name] = {
            "value": metrics.get(name),
            "rule": dict(rule) if isinstance(rule, Mapping) else rule,
            "status": "fail" if name in failed else "pass",
        }
    if "offline_max_duration_ms" in thresholds:
        rules["offline_duration_ms"] = {
            "value": duration_ms,
            "rule": {"max": thresholds["offline_max_duration_ms"]},
            "status": "fail" if "offline_duration_ms" in failed else "pass",
        }
    if "min_task_count" in thresholds or "max_task_count" in thresholds:
        rule: dict[str, Any] = {}
        if "min_task_count" in thresholds:
            rule["min"] = thresholds["min_task_count"]
        if "max_task_count" in thresholds:
            rule["max"] = thresholds["max_task_count"]
        rules["task_count"] = {
            "value": metrics.get("task_count"),
            "rule": rule,
            "status": "fail" if "task_count" in failed else "pass",
        }
    if thresholds.get("require_deterministic", True):
        rules["deterministic"] = {
            "value": metrics.get("deterministic"),
            "rule": {"required": True},
            "status": "fail" if "deterministic" in failed else "pass",
        }
    return rules


def run_suite(
    *,
    dataset_path: str | Path = DEFAULT_DATASET,
    fixtures_path: str | Path = DEFAULT_FIXTURES,
    thresholds_path: str | Path = DEFAULT_THRESHOLDS,
    out_dir: str | Path = "var/evals",
    live: bool = False,
    repeat: int | None = None,
) -> SuiteOutcome:
    """Run the suite, write reports, and return the gate outcome."""

    dataset = load_dataset(dataset_path)
    try:
        fixtures = load_recorded_fixtures(fixtures_path)
    except FixtureError as exc:
        raise DatasetError(str(exc)) from exc
    missing_fixtures = [
        str(task["id"])
        for task in dataset.tasks
        if task["scenario"] in PROVIDER_SCENARIOS
        and str(task["id"]) not in (fixtures.get("tasks") or {})
    ]
    if missing_fixtures:
        raise DatasetError(
            "recorded provider fixture is missing tasks: " + ", ".join(missing_fixtures)
        )
    thresholds = _load_thresholds(thresholds_path)
    if repeat is None:
        repeat = 1 if live else 3
    if repeat < 1:
        raise ValueError("repeat must be at least 1")

    config = KernelConfig.from_env()
    if live:
        if not config.api_key:
            raise LiveModeError(
                "live mode requires NEBIUS_API_KEY; the key is never printed or written"
            )
        model = config.primary_model
    else:
        model = DEFAULT_MODEL

    mode = _MODE_LIVE if live else _MODE_OFFLINE
    out_base = Path(out_dir)
    out_base.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_id = _new_run_id(out_base, mode)
    previous = find_previous(out_base, mode=mode, dataset_version=dataset.version)
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()

    temp_parent = Path("/tmp/opencode") if Path("/tmp/opencode").is_dir() else None
    work_root = Path(tempfile.mkdtemp(prefix="kelp-eval-", dir=str(temp_parent) if temp_parent else None))
    live_client: TokenFactoryClient | None = None
    try:
        if live:
            live_client = TokenFactoryClient(config)
            guard = nullcontext()
        else:
            guard = block_network()
        with guard:
            tasks = asyncio.run(
                _run_all(
                    dataset,
                    fixtures,
                    live=live,
                    model=model,
                    repeat=repeat,
                    live_client=live_client,
                    work_root=work_root,
                )
            )
        duration_ms = max(0, int(round((time.perf_counter() - started) * 1000)))
    finally:
        shutil.rmtree(work_root, ignore_errors=True)

    metrics = compute_metrics(tasks)
    violations = evaluate_thresholds(
        metrics, thresholds, duration_ms=duration_ms, offline=not live
    )
    previous_metrics: dict[str, Any] | None = None
    if previous is not None:
        previous_metrics = dict(previous.get("metrics") or {})
        previous_metrics["run_id"] = previous.get("run_id")
    comparison = compare_to_previous(metrics, previous_metrics, thresholds)
    regressions = comparison.get("regressions") or []
    gating = not live
    exit_code = 1 if gating and (violations or regressions) else 0
    thresholds_passed = not violations and not regressions

    provider_label = (
        "nebius://token-factory"
        if live
        else str(fixtures.get("provider") or "recorded://synthetic")
    )
    report: dict[str, Any] = {
        "schema_version": "kelp-eval-report/1",
        "run_id": run_id,
        "mode": mode,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "duration_ms": duration_ms,
        "repeat": repeat,
        "dataset": {
            "version": dataset.version,
            "task_count": len(dataset.tasks),
            "sha256": dataset.sha256,
            "path": str(dataset.path),
        },
        "fixtures": {
            "path": str(fixtures_path),
            "sha256": _sha256(fixtures_path),
            "provider": fixtures.get("provider"),
            "recorded_at": fixtures.get("recorded_at"),
        },
        "provider": {"label": provider_label, "model": model},
        "network": "blocked" if not live else "live-provider-only",
        "metrics": metrics,
        "thresholds": {
            "path": str(thresholds_path),
            "gating": gating,
            "passed": thresholds_passed,
            "rules": _threshold_rules(metrics, thresholds, duration_ms, violations),
            "violations": violations,
        },
        "comparison": comparison,
        "tasks": tasks,
        "exit_code": exit_code,
    }
    json_path, md_path = write_reports(out_base, run_id, report)
    return SuiteOutcome(
        exit_code=exit_code,
        report_path=json_path,
        markdown_path=md_path,
        report=report,
    )

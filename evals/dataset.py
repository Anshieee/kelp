"""Versioned eval dataset loading and validation.

The dataset is data, not code: every task names a scenario the harness already
implements and declares its expectations explicitly. Loading fails fast on a
duplicate id, an unknown scenario, a missing document, or a category the
release gates do not measure.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "CATEGORIES",
    "DEFAULT_DATASET",
    "DEFAULT_FIXTURES",
    "DEFAULT_THRESHOLDS",
    "Dataset",
    "DatasetError",
    "SCENARIOS",
    "load_dataset",
]

PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_DATASET = PACKAGE_ROOT / "datasets" / "v1" / "tasks.json"
DEFAULT_FIXTURES = PACKAGE_ROOT / "fixtures" / "recorded" / "provider_v1.json"
DEFAULT_THRESHOLDS = PACKAGE_ROOT / "thresholds.json"

CATEGORIES = (
    "retrieval",
    "tool",
    "approval",
    "restart_resume",
    "multi_turn",
)

SCENARIOS = frozenset(
    {
        "retrieval",
        "tool_turn",
        "approval_approve",
        "approval_deny",
        "approval_redaction",
        "approval_expired",
        "approval_replay",
        "recovery_running_turn",
        "recovery_running_tools_and_approval",
        "recovery_idempotent",
        "resume_retry",
        "multi_turn_two",
        "multi_turn_idempotent",
        "multi_turn_isolation",
        "multi_turn_title",
    }
)

PROVIDER_SCENARIOS = frozenset(
    {
        "tool_turn",
        "approval_approve",
        "approval_deny",
        "approval_redaction",
        "approval_replay",
        "resume_retry",
        "multi_turn_two",
        "multi_turn_idempotent",
        "multi_turn_isolation",
        "multi_turn_title",
    }
)

DOC_SCENARIOS = frozenset({"retrieval"})

_TASK_ID_RE = re.compile(r"^EV-\d{3}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


class DatasetError(ValueError):
    """The dataset is unusable. The message names the task and the field."""


@dataclass(frozen=True)
class Dataset:
    version: str
    path: Path
    tasks: tuple[Mapping[str, Any], ...]
    sha256: str

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(str(task["id"]) for task in self.tasks)

    def by_id(self, task_id: str) -> Mapping[str, Any]:
        for task in self.tasks:
            if task["id"] == task_id:
                return task
        raise KeyError(task_id)

    def categories(self) -> dict[str, int]:
        counts = {category: 0 for category in CATEGORIES}
        for task in self.tasks:
            counts[str(task["category"])] += 1
        return counts


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DatasetError(message)


def load_dataset(path: str | Path = DEFAULT_DATASET) -> Dataset:
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise DatasetError(f"dataset is unreadable: {path}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DatasetError(f"dataset is not valid UTF-8 JSON: {path}") from exc
    _require(isinstance(data, Mapping), "dataset must be a JSON object")
    version = data.get("dataset_version")
    _require(isinstance(version, str) and bool(version.strip()), "dataset_version is required")
    tasks = data.get("tasks")
    _require(isinstance(tasks, list), "tasks must be a list")
    _require(20 <= len(tasks) <= 30, "dataset must contain 20-30 tasks")

    seen: set[str] = set()
    categories_seen: set[str] = set()
    normalised: list[Mapping[str, Any]] = []
    for index, task in enumerate(tasks):
        label = f"task[{index}]"
        _require(isinstance(task, Mapping), f"{label} must be an object")
        task_id = task.get("id")
        _require(
            isinstance(task_id, str) and bool(_TASK_ID_RE.match(task_id)),
            f"{label}: id must look like EV-001",
        )
        label = str(task_id)
        _require(task_id not in seen, f"{label}: duplicate id")
        seen.add(task_id)

        category = task.get("category")
        _require(category in CATEGORIES, f"{label}: unknown category {category!r}")
        categories_seen.add(str(category))

        scenario = task.get("scenario")
        _require(scenario in SCENARIOS, f"{label}: unknown scenario {scenario!r}")

        name = task.get("name")
        _require(isinstance(name, str) and 0 < len(name) <= 120, f"{label}: name is required")
        prompt = task.get("prompt")
        _require(isinstance(prompt, str) and 0 < len(prompt) <= 8000, f"{label}: prompt is required")

        expect = task.get("expect")
        _require(isinstance(expect, Mapping), f"{label}: expect must be an object")
        for key in expect:
            _require(isinstance(key, str), f"{label}: expect keys must be strings")

        doc = task.get("doc")
        if doc is not None:
            _require(isinstance(doc, Mapping), f"{label}: doc must be an object")
            _require(doc.get("kind") in {"csv", "pdf"}, f"{label}: doc.kind must be csv or pdf")
            token = task.get("artifact_token")
            _require(
                isinstance(token, str) and bool(_TOKEN_RE.match(token)),
                f"{label}: artifact_token must be 8-128 safe characters",
            )
            if doc.get("kind") == "csv":
                _require(
                    isinstance(doc.get("content"), str) and bool(doc["content"]),
                    f"{label}: CSV doc.content is required",
                )
            else:
                pages = doc.get("pages")
                _require(
                    isinstance(pages, list) and bool(pages) and all(
                        isinstance(page, list) and all(isinstance(line, str) for line in page)
                        for page in pages
                    ),
                    f"{label}: PDF doc.pages must be a list of string lists",
                )
        if scenario in DOC_SCENARIOS:
            _require(doc is not None, f"{label}: {scenario} requires doc")

        fixture = task.get("fixture")
        if scenario in PROVIDER_SCENARIOS:
            _require(isinstance(fixture, str) and bool(fixture), f"{label}: fixture is required")
        elif fixture is not None:
            _require(isinstance(fixture, str) and bool(fixture), f"{label}: fixture must be a string")

        citations = expect.get("citation_locators")
        if citations is not None:
            _require(
                isinstance(citations, list) and all(
                    isinstance(item, str) and (item.startswith("row ") or item.startswith("page "))
                    for item in citations
                ),
                f"{label}: citation_locators must be row/page locators",
            )
        tool_calls = expect.get("tool_calls")
        if tool_calls is not None:
            _require(
                isinstance(tool_calls, list) and all(
                    isinstance(call, Mapping)
                    and isinstance(call.get("name"), str)
                    and isinstance(call.get("arguments", {}), Mapping)
                    for call in tool_calls
                ),
                f"{label}: expect.tool_calls must be objects with name/arguments",
            )
        normalised.append(dict(task))

    _require(
        categories_seen == set(CATEGORIES),
        f"dataset must cover every category: missing {sorted(set(CATEGORIES) - categories_seen)}",
    )

    sha256 = hashlib.sha256(raw).hexdigest()
    return Dataset(version=version.strip(), path=path, tasks=tuple(normalised), sha256=sha256)

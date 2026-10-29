"""Dataset and recorded-fixture contract tests for the KEL-15 eval harness."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from evals.dataset import (
    CATEGORIES,
    DEFAULT_DATASET,
    DEFAULT_FIXTURES,
    PROVIDER_SCENARIOS,
    DatasetError,
    load_dataset,
)
from evals.fixtures import FixtureError, load_recorded_fixtures, make_pdf


def _raw_dataset() -> dict:
    return json.loads(Path(DEFAULT_DATASET).read_text(encoding="utf-8"))


def test_dataset_is_versioned_and_covers_every_category() -> None:
    dataset = load_dataset()
    assert dataset.version
    assert 20 <= len(dataset.tasks) <= 30
    assert len(dataset.ids) == len(set(dataset.ids))
    counts = dataset.categories()
    assert set(counts) == set(CATEGORIES)
    assert all(count > 0 for count in counts.values())
    assert counts == {
        "retrieval": 7,
        "tool": 6,
        "approval": 5,
        "restart_resume": 4,
        "multi_turn": 4,
    }


def test_every_recorded_task_has_a_fixture_and_every_fixture_is_used() -> None:
    dataset = load_dataset()
    fixtures = load_recorded_fixtures(DEFAULT_FIXTURES)
    tasks = fixtures["tasks"]
    referenced = {
        str(task["id"])
        for task in dataset.tasks
        if task["scenario"] in PROVIDER_SCENARIOS
    }
    assert referenced == set(tasks)
    for task_id, record in tasks.items():
        assert record["turns"]
        assert any(event["type"] == "stream_end" for event in record["turns"][-1])


def test_dataset_and_fixtures_contain_no_credential_shaped_text() -> None:
    text = Path(DEFAULT_DATASET).read_text(encoding="utf-8")
    text += Path(DEFAULT_FIXTURES).read_text(encoding="utf-8")
    assert "NEBIUS_API_KEY" not in text
    assert not re.search(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{4,}", text)


def test_duplicate_task_id_is_rejected(tmp_path: Path) -> None:
    data = _raw_dataset()
    data["tasks"][1]["id"] = data["tasks"][0]["id"]
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(DatasetError, match="duplicate"):
        load_dataset(path)


def test_too_few_tasks_are_rejected(tmp_path: Path) -> None:
    data = _raw_dataset()
    data["tasks"] = data["tasks"][:19]
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(DatasetError, match="20-30"):
        load_dataset(path)


def test_unknown_scenario_is_rejected(tmp_path: Path) -> None:
    data = _raw_dataset()
    data["tasks"][0]["scenario"] = "not_a_scenario"
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(DatasetError, match="unknown scenario"):
        load_dataset(path)


def test_malformed_fixture_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "fixtures.json"
    path.write_text(
        json.dumps({"fixture_version": 1, "tasks": {"EV-008": {"turns": []}}}),
        encoding="utf-8",
    )
    with pytest.raises(FixtureError, match="non-empty"):
        load_recorded_fixtures(path)


def test_pdf_builder_is_deterministic() -> None:
    pages = [["First page."], ["Second page."]]
    assert make_pdf(pages) == make_pdf(pages)
    assert make_pdf(pages).startswith(b"%PDF-1.4")

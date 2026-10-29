"""Audit log tests: redaction, append-only behaviour, filtering (KEL-7)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from openshell import AuditLog, SecretScrubber

SECRET = "super-secret-value-12345"


def test_scrubber_removes_known_values() -> None:
    scrubber = SecretScrubber([SECRET])
    assert scrubber.scrub_text(f"token={SECRET}") == "token=[redacted]"
    assert scrubber.scrub_text("short abc") == "short abc"


def test_scrubber_removes_common_credential_patterns() -> None:
    scrubber = SecretScrubber([])
    assert "sk_live_" not in scrubber.scrub_text("key sk_live_abcdef123456")
    assert "abcdef" not in scrubber.scrub_text("Authorization: Bearer abcdef123456")
    assert "hunter2" not in scrubber.scrub_text("password=hunter2")
    assert "hunter3" not in scrubber.scrub_text("api_key: 'hunter3'")


def test_scrub_recurses_into_structures() -> None:
    scrubber = SecretScrubber([SECRET])
    value = {"nested": [SECRET, {"deep": f"x-{SECRET}-y"}]}
    cleaned = scrubber.scrub(value)
    assert cleaned == {"nested": ["[redacted]", {"deep": "x-[redacted]-y"}]}


def test_audit_redacts_every_field(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl", secret_values=[SECRET])
    log.record(
        "exec_requested",
        turn_id="t1",
        actor=f"user-{SECRET}",
        command=("echo", SECRET),
        extra={"note": f"value={SECRET}", "nested": {"token": SECRET}},
    )
    raw = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert SECRET not in raw
    entry = json.loads(raw.strip())
    assert entry["command"] == ["echo", "[redacted]"]
    assert entry["extra"]["note"] == "value=[redacted]"


def test_audit_is_append_only_and_mode_600(tmp_path: Path) -> None:
    path = tmp_path / "audit" / "openshell.jsonl"
    log = AuditLog(path)
    log.record("first", turn_id="t1")
    log.record("second", turn_id="t1")

    entries = log.entries(turn_id="t1")
    assert [entry["event"] for entry in entries] == ["first", "second"]

    reopened = AuditLog(path)
    reopened.record("third", turn_id="t1")
    assert [entry["event"] for entry in reopened.entries()] == ["first", "second", "third"]

    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_audit_secret_names_are_read_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEBIUS_API_KEY", SECRET)
    log = AuditLog.from_env(tmp_path / "audit.jsonl", secret_names=("NEBIUS_API_KEY",))
    log.record("test", extra={"value": SECRET})
    assert SECRET not in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")


def test_entries_filter_by_event(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record("exec_started", turn_id="t1")
    log.record("exec_finished", turn_id="t1")
    assert len(log.entries(event="exec_started")) == 1
    assert log.entries(turn_id="missing") == []

"""Exec gate tests: approval-gated execution, TTL, replay, audit, no secrets (KEL-7)."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from openshell import (
    ApprovalStore,
    AuditLog,
    ExecGate,
    PolicyConfig,
    PolicyDenied,
    PolicyEngine,
)

SECRET = "super-secret-value-12345"
PROVIDER_KEY = "sk_live_abcdef123456"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def make_gate(
    tmp_path: Path,
    *,
    allow_exec: bool = True,
    allowed_commands: tuple[str, ...] = ("echo", sys.executable),
    clock: Clock | None = None,
    max_output_bytes: int = 65_536,
) -> tuple[ExecGate, AuditLog, Path]:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    policy = PolicyEngine(
        PolicyConfig(
            workspace_root=workspace,
            allowed_commands=frozenset(allowed_commands),
            allow_exec=allow_exec,
        )
    )
    approvals = ApprovalStore(now_fn=clock) if clock else ApprovalStore()
    audit = AuditLog(tmp_path / "audit" / "openshell.jsonl")
    gate = ExecGate(
        policy=policy,
        approvals=approvals,
        audit=audit,
        max_output_bytes=max_output_bytes,
    )
    return gate, audit, workspace


def run(gate: ExecGate, request_id: str, **kwargs: Any) -> Any:
    return asyncio.run(gate.execute(request_id, **kwargs))


def audit_text(audit: AuditLog) -> str:
    return audit.path.read_text(encoding="utf-8")


def test_approved_command_executes_and_is_audited(tmp_path: Path) -> None:
    gate, audit, _workspace = make_gate(tmp_path)
    request = gate.request(["echo", "hello"], turn_id="t1")

    assert request.status == "pending"
    assert request.single_use is True

    gate.approve(request.id, turn_id="t1")
    result = run(gate, request.id, turn_id="t1")

    assert result.ok is True
    assert result.outcome == "completed"
    assert result.exit_code == 0
    assert result.stdout == "hello\n"

    entries = audit.entries(turn_id="t1")
    assert [entry["event"] for entry in entries] == [
        "exec_requested",
        "approval_requested",
        "approval_granted",
        "exec_started",
        "exec_finished",
    ]
    finished = entries[-1]
    assert finished["exit_code"] == 0
    assert finished["outcome"] == "completed"
    assert "stdout_sha256" in finished["extra"]
    assert all("ts" in entry for entry in entries)


def test_execute_before_approval_does_not_run(tmp_path: Path) -> None:
    gate, audit, workspace = make_gate(tmp_path)
    marker = workspace / "should-not-exist.txt"
    code = f"open({str(marker)!r}, 'w').write('x')"
    request = gate.request([sys.executable, "-c", code], turn_id="t1")

    result = run(gate, request.id, turn_id="t1")

    assert result.ok is False
    assert result.outcome == "not_approved"
    assert not marker.exists()
    assert audit.entries(event="exec_blocked")[0]["outcome"] == "not_approved"


def test_denied_approval_leaves_no_side_effect(tmp_path: Path) -> None:
    gate, audit, workspace = make_gate(tmp_path)
    marker = workspace / "denied.txt"
    code = f"open({str(marker)!r}, 'w').write('x')"
    request = gate.request([sys.executable, "-c", code], turn_id="t1")

    gate.deny(request.id, turn_id="t1")
    result = run(gate, request.id, turn_id="t1")

    assert result.outcome == "denied"
    assert not marker.exists()
    events = [entry["event"] for entry in audit.entries(turn_id="t1")]
    assert events == [
        "exec_requested",
        "approval_requested",
        "approval_denied",
        "exec_blocked",
    ]


def test_expired_approval_cannot_be_replayed(tmp_path: Path) -> None:
    clock = Clock()
    gate, audit, workspace = make_gate(tmp_path, clock=clock)
    marker = workspace / "expired.txt"
    code = f"open({str(marker)!r}, 'w').write('x')"
    request = gate.request([sys.executable, "-c", code], turn_id="t1")
    gate.approve(request.id, turn_id="t1")

    clock.advance(601)
    result = run(gate, request.id, turn_id="t1")

    assert result.outcome == "expired"
    assert not marker.exists()
    assert audit.entries(event="exec_blocked")[-1]["outcome"] == "expired"


def test_approval_is_single_use(tmp_path: Path) -> None:
    gate, _audit, workspace = make_gate(tmp_path)
    marker = workspace / "count.txt"
    code = f"open({str(marker)!r}, 'a').write('x')"
    request = gate.request([sys.executable, "-c", code], turn_id="t1")
    gate.approve(request.id, turn_id="t1")

    first = run(gate, request.id, turn_id="t1")
    second = run(gate, request.id, turn_id="t1")

    assert first.ok is True
    assert second.ok is False
    assert second.outcome == "already_used"
    assert marker.read_text(encoding="utf-8") == "x"


def test_policy_denies_disabled_exec_before_approval(tmp_path: Path) -> None:
    gate, audit, _workspace = make_gate(tmp_path, allow_exec=False)

    with pytest.raises(PolicyDenied):
        gate.request(["echo", "hi"], turn_id="t1")

    entries = audit.entries(turn_id="t1")
    assert entries[0]["event"] == "exec_requested"
    assert entries[0]["decision"] == "deny"
    assert len(entries) == 1


def test_policy_denies_unlisted_command(tmp_path: Path) -> None:
    gate, _audit, _workspace = make_gate(tmp_path, allowed_commands=("echo",))

    with pytest.raises(PolicyDenied):
        gate.request(["rm", "-rf", "everything"], turn_id="t1")


def test_shell_strings_are_rejected(tmp_path: Path) -> None:
    gate, _audit, _workspace = make_gate(tmp_path)
    with pytest.raises(ValueError):
        gate.request("echo hello", turn_id="t1")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        gate.request([], turn_id="t1")


def test_timeout_kills_the_process(tmp_path: Path) -> None:
    gate, audit, _workspace = make_gate(tmp_path)
    request = gate.request(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        turn_id="t1",
        timeout_s=0.2,
    )
    gate.approve(request.id, turn_id="t1")

    result = run(gate, request.id, turn_id="t1")

    assert result.ok is False
    assert result.outcome == "timeout"
    assert result.duration_ms < 5_000
    assert audit.entries(event="exec_finished")[-1]["outcome"] == "timeout"


def test_secret_env_is_not_forwarded_or_logged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEBIUS_API_KEY", SECRET)
    gate, audit, _workspace = make_gate(tmp_path)
    code = "import os; print(os.environ.get('NEBIUS_API_KEY', 'absent'))"
    request = gate.request([sys.executable, "-c", code], turn_id="t1")
    gate.approve(request.id, turn_id="t1")

    result = run(gate, request.id, turn_id="t1")

    assert result.stdout.strip() == "absent"
    assert SECRET not in result.stdout
    assert SECRET not in audit_text(audit)


def test_secret_in_output_is_scrubbed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEBIUS_API_KEY", SECRET)
    gate, audit, _workspace = make_gate(tmp_path)
    code = f"print({SECRET!r})"
    request = gate.request([sys.executable, "-c", code], turn_id="t1")
    gate.approve(request.id, turn_id="t1")

    result = run(gate, request.id, turn_id="t1")

    assert "[redacted]" in result.stdout
    assert SECRET not in result.stdout
    assert SECRET not in audit_text(audit)


def test_provider_key_pattern_in_argv_and_output_is_scrubbed(tmp_path: Path) -> None:
    gate, audit, _workspace = make_gate(tmp_path)
    request = gate.request(["echo", PROVIDER_KEY], turn_id="t1")
    gate.approve(request.id, turn_id="t1")

    result = run(gate, request.id, turn_id="t1")

    assert PROVIDER_KEY not in result.stdout
    assert "[redacted]" in result.stdout
    assert PROVIDER_KEY not in audit_text(audit)


def test_output_is_bounded(tmp_path: Path) -> None:
    gate, _audit, _workspace = make_gate(tmp_path, max_output_bytes=16)
    request = gate.request(
        [sys.executable, "-c", "print('x' * 100)"],
        turn_id="t1",
    )
    gate.approve(request.id, turn_id="t1")

    result = run(gate, request.id, turn_id="t1")

    assert result.stdout.endswith("[output truncated]")
    assert "x" * 20 not in result.stdout


def test_audit_log_reconstructs_the_turn(tmp_path: Path) -> None:
    gate, audit, _workspace = make_gate(tmp_path)
    request = gate.request(["echo", "one"], turn_id="turn-42")
    gate.approve(request.id, turn_id="turn-42")
    run(gate, request.id, turn_id="turn-42")

    entries = audit.entries(turn_id="turn-42")
    assert len(entries) == 5
    requested = entries[0]
    assert requested["command"] == ["echo", "one"]
    assert requested["tool"] == "shell.exec"
    assert requested["extra"]["cwd"].endswith("ws")
    assert entries[1]["argument_hash"].startswith("sha256:")
    assert entries[1]["request_id"] == request.id
    assert entries[2]["decision"] == "approved"
    assert entries[3]["event"] == "exec_started"
    assert entries[4]["event"] == "exec_finished"

    # The file is JSONL and append-only across instances.
    lines = audit.path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["event"] for line in lines] == [
        "exec_requested",
        "approval_requested",
        "approval_granted",
        "exec_started",
        "exec_finished",
    ]

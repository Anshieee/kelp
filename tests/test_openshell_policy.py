"""Policy engine tests: allow / deny / require approval (KEL-7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from openshell import (
    Capability,
    Decision,
    Effect,
    PolicyConfig,
    PolicyEngine,
    host_allowed,
)

READ_SCOPE = ("${WORKSPACE}/var/artifacts/**",)
WRITE_SCOPE = ("${WORKSPACE}/var/scheduler/**",)


@pytest.fixture()
def engine(tmp_path: Path) -> PolicyEngine:
    return PolicyEngine(
        PolicyConfig(
            workspace_root=tmp_path,
            allowed_hosts=frozenset({"api.github.com", "*.example.com"}),
            allowed_commands=frozenset({"echo", "/usr/bin/printf"}),
            allow_exec=True,
        )
    )


def capability(*effects: Effect, paths: tuple[str, ...] = (), hosts: tuple[str, ...] = ()) -> Capability:
    return Capability(
        tool="test.tool",
        effects=frozenset(effects),
        paths=paths,
        hosts=hosts,
    )


def test_read_inside_scope_is_allowed(engine: PolicyEngine, tmp_path: Path) -> None:
    verdict = engine.evaluate(
        capability(Effect.READ, paths=READ_SCOPE),
        target="var/artifacts/report.pdf",
    )
    assert verdict.decision is Decision.ALLOW
    assert "var/artifacts/report.pdf" in verdict.blast_radius[0]


def test_read_outside_scope_is_denied(engine: PolicyEngine) -> None:
    verdict = engine.evaluate(
        capability(Effect.READ, paths=READ_SCOPE),
        target="var/scheduler/jobs.json",
    )
    assert verdict.decision is Decision.DENY
    assert "outside the declared scope" in verdict.reason


def test_read_traversal_outside_workspace_is_denied(engine: PolicyEngine) -> None:
    verdict = engine.evaluate(
        capability(Effect.READ, paths=READ_SCOPE),
        target="../../etc/passwd",
    )
    assert verdict.decision is Decision.DENY
    assert "outside the workspace" in verdict.reason


def test_read_without_target_is_denied(engine: PolicyEngine) -> None:
    verdict = engine.evaluate(capability(Effect.READ, paths=READ_SCOPE))
    assert verdict.decision is Decision.DENY


def test_write_inside_scope_requires_approval(engine: PolicyEngine) -> None:
    verdict = engine.evaluate(
        capability(Effect.WRITE, paths=WRITE_SCOPE),
        target="var/scheduler/jobs.json",
    )
    assert verdict.decision is Decision.REQUIRE_APPROVAL
    assert "var/scheduler/jobs.json" in verdict.blast_radius[0]


def test_write_outside_scope_is_denied_by_default(engine: PolicyEngine) -> None:
    verdict = engine.evaluate(
        capability(Effect.WRITE, paths=WRITE_SCOPE),
        target="var/artifacts/report.pdf",
    )
    assert verdict.decision is Decision.DENY


def test_write_outside_scope_can_escalate_to_approval(tmp_path: Path) -> None:
    escalating = PolicyEngine(
        PolicyConfig(
            workspace_root=tmp_path,
            allow_write_outside_scope=True,
        )
    )
    verdict = escalating.evaluate(
        capability(Effect.WRITE, paths=WRITE_SCOPE),
        target="var/artifacts/report.pdf",
    )
    assert verdict.decision is Decision.REQUIRE_APPROVAL
    assert "scope:outside" in verdict.blast_radius
    assert "needs approval" in verdict.reason


def test_write_outside_workspace_is_denied_even_with_escalation(tmp_path: Path) -> None:
    escalating = PolicyEngine(
        PolicyConfig(workspace_root=tmp_path, allow_write_outside_scope=True)
    )
    verdict = escalating.evaluate(
        capability(Effect.WRITE, paths=WRITE_SCOPE),
        target="../../etc/passwd",
    )
    assert verdict.decision is Decision.DENY
    assert "outside the workspace" in verdict.reason


def test_network_allowlist(engine: PolicyEngine) -> None:
    assert engine.evaluate(
        capability(Effect.NETWORK, hosts=("api.github.com",)),
        host="api.github.com",
    ).decision is Decision.ALLOW
    assert engine.evaluate(
        capability(Effect.NETWORK, hosts=("*.example.com",)),
        host="docs.example.com",
    ).decision is Decision.ALLOW
    assert engine.evaluate(
        capability(Effect.NETWORK, hosts=("*.example.com",)),
        host="example.com",
    ).decision is Decision.DENY
    assert engine.evaluate(
        capability(Effect.NETWORK, hosts=("api.github.com",)),
        host="evil.example",
    ).decision is Decision.DENY


def test_network_host_grammar(engine: PolicyEngine) -> None:
    bad_hosts = ["https://api.github.com", "api.github.com:443", "api.github.com/v1"]
    for host in bad_hosts:
        assert host_allowed(host, {"api.github.com"}) is False
        verdict = engine.evaluate(
            capability(Effect.NETWORK, hosts=("api.github.com",)),
            host=host,
        )
        assert verdict.decision is Decision.DENY


def test_network_requires_host(engine: PolicyEngine) -> None:
    verdict = engine.evaluate(
        capability(Effect.NETWORK, hosts=("api.github.com",))
    )
    assert verdict.decision is Decision.DENY


def test_exec_is_denied_when_disabled(tmp_path: Path) -> None:
    engine = PolicyEngine(
        PolicyConfig(workspace_root=tmp_path, allowed_commands=frozenset({"echo"}))
    )
    verdict = engine.evaluate(
        capability(Effect.EXECUTE, paths=("${WORKSPACE}/",)),
        argv=("echo", "hi"),
        cwd=tmp_path,
    )
    assert verdict.decision is Decision.DENY
    assert "deny-by-default" in verdict.reason


def test_exec_requires_allowlisted_command_and_workspace_cwd(
    engine: PolicyEngine, tmp_path: Path
) -> None:
    allowed = engine.evaluate(
        capability(Effect.EXECUTE, paths=("${WORKSPACE}/",)),
        argv=("echo", "hi"),
        cwd=tmp_path,
    )
    assert allowed.decision is Decision.REQUIRE_APPROVAL

    not_allowed = engine.evaluate(
        capability(Effect.EXECUTE, paths=("${WORKSPACE}/",)),
        argv=("rm", "-rf", "x"),
        cwd=tmp_path,
    )
    assert not_allowed.decision is Decision.DENY
    assert "not in the exec allowlist" in not_allowed.reason

    outside = engine.evaluate(
        capability(Effect.EXECUTE, paths=("${WORKSPACE}/",)),
        argv=("echo", "hi"),
        cwd=Path("/tmp"),
    )
    assert outside.decision is Decision.DENY
    assert "outside the workspace" in outside.reason


def test_exec_full_path_must_match_exactly(engine: PolicyEngine, tmp_path: Path) -> None:
    assert engine.evaluate(
        capability(Effect.EXECUTE),
        argv=("/usr/bin/printf", "hi"),
        cwd=tmp_path,
    ).decision is Decision.REQUIRE_APPROVAL
    # A different path with the same basename is not the allowlisted binary.
    assert engine.evaluate(
        capability(Effect.EXECUTE),
        argv=("/tmp/printf", "hi"),
        cwd=tmp_path,
    ).decision is Decision.DENY


def test_empty_or_unknown_effects_are_denied(engine: PolicyEngine) -> None:
    empty = Capability(tool="test.tool", effects=frozenset())
    assert engine.evaluate(empty).decision is Decision.DENY
    unknown = Capability(tool="test.tool", effects=frozenset({"teleport"}))  # type: ignore[arg-type]
    assert engine.evaluate(unknown).decision is Decision.DENY
    assert "unknown effect" in engine.evaluate(unknown).reason


def test_worst_decision_wins(engine: PolicyEngine) -> None:
    verdict = engine.evaluate(
        capability(Effect.READ, Effect.NETWORK, paths=READ_SCOPE, hosts=("api.github.com",)),
        target="var/artifacts/a.pdf",
        host="evil.example",
    )
    assert verdict.decision is Decision.DENY
    assert "evil.example" in verdict.reason


def test_workspace_scope_admits_directory_itself(engine: PolicyEngine, tmp_path: Path) -> None:
    verdict = engine.evaluate(
        capability(Effect.READ, paths=("${WORKSPACE}/var/**",)),
        target="var",
    )
    assert verdict.decision is Decision.ALLOW

"""KEL-20: effect/scope enforcement at invoke time.

Covers the five kill paths from the issue — scope escape, blocked effect,
denied host, direct egress, invalid output — plus the audit record and the
rule that a policy deny is never approvable.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Mapping

import pytest

from openshell import AuditLog, PolicyConfig
from skills import SkillManifestError, SkillRegistry
from skills.policy import EgressResponse
from tests.fixtures import handlers


def write_root(tmp_path: Path, manifest: Mapping[str, Any]) -> Path:
    root = tmp_path / "skills"
    skill_dir = root / "policy_fixture"
    skill_dir.mkdir(parents=True)
    (skill_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def policy_manifest(
    *,
    name: str = "demo.policy",
    callable: str = "reflect",
    effects: tuple[str, ...] = ("read",),
    paths: tuple[str, ...] = (),
    hosts: tuple[str, ...] = (),
    repos: tuple[str, ...] = (),
    requires_approval: bool = False,
    idempotent: bool = True,
    properties: Mapping[str, Any] | None = None,
    required: tuple[str, ...] = ("value",),
    output_properties: Mapping[str, Any] | None = None,
    timeout_ms: int = 2000,
) -> dict[str, Any]:
    input_properties = dict(
        properties or {"value": {"type": "string"}}
    )
    output_properties = dict(
        output_properties or {"value": {"type": "string"}}
    )
    return {
        "manifest_version": "1.0",
        "name": name,
        "version": "0.1.0",
        "description": "KEL-20 policy enforcement fixture.",
        "triggers": [
            {"kind": "model", "description": "Exercise invoke-time policy."}
        ],
        "tools": [name],
        "input_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": list(required),
            "properties": input_properties,
        },
        "output_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["value"],
            "properties": output_properties,
        },
        "permissions": {
            "effects": list(effects),
            "scopes": {
                "paths": list(paths),
                "hosts": list(hosts),
                "repos": list(repos),
            },
            "requires_approval": requires_approval,
            "secrets": [],
        },
        "entry": {"module": "tests.fixtures.handlers", "callable": callable},
        "timeout_ms": timeout_ms,
        "idempotent": idempotent,
        "cost_class": "free",
    }


def load(tmp_path: Path, manifest: Mapping[str, Any]) -> SkillRegistry:
    return SkillRegistry.load(write_root(tmp_path, manifest))


def invoke(
    registry: SkillRegistry,
    tool: str = "demo.policy",
    arguments: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    return asyncio.run(registry.invoke(tool, arguments, **kwargs))


@pytest.fixture(autouse=True)
def _reset_handlers() -> None:
    handlers.reset()
    yield
    handlers.reset()


# ---------------------------------------------------------------------------
# Path confinement
# ---------------------------------------------------------------------------


def test_in_scope_path_is_allowed(tmp_path: Path) -> None:
    registry = load(
        tmp_path,
        policy_manifest(
            callable="read_path_probe",
            paths=("${WORKSPACE}/var/artifacts/**",),
            properties={"path": {"type": "string"}},
            required=("path",),
        ),
    )
    result = invoke(
        registry,
        arguments={"path": "var/artifacts/report.pdf"},
        workspace_root=tmp_path,
    )
    assert result["ok"] is True, result
    assert result["output"]["value"] == "report.pdf"


def test_out_of_scope_path_is_denied_at_invoke(tmp_path: Path) -> None:
    registry = load(
        tmp_path,
        policy_manifest(
            callable="read_path_probe",
            paths=("${WORKSPACE}/var/artifacts/**",),
            properties={"path": {"type": "string"}},
            required=("path",),
        ),
    )
    result = invoke(
        registry,
        arguments={"path": "var/scheduler/jobs.json"},
        workspace_root=tmp_path,
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "policy_denied"
    assert result["error"]["details"]["decision"] == "deny"
    assert "outside the declared scope" in result["error"]["message"]
    assert "approval" not in result


def test_path_traversal_is_denied(tmp_path: Path) -> None:
    registry = load(
        tmp_path,
        policy_manifest(
            callable="read_path_probe",
            paths=("${WORKSPACE}/var/artifacts/**",),
            properties={"path": {"type": "string"}},
            required=("path",),
        ),
    )
    result = invoke(
        registry,
        arguments={"path": "../../etc/passwd"},
        workspace_root=tmp_path,
    )
    assert result["error"]["code"] == "policy_denied"
    assert "outside the workspace" in result["error"]["message"]


def test_symlink_escape_is_denied(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    artifacts = workspace / "var" / "artifacts"
    artifacts.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (artifacts / "link.txt").symlink_to(outside)

    registry = load(
        tmp_path,
        policy_manifest(
            callable="read_path_probe",
            paths=("${WORKSPACE}/var/artifacts/**",),
            properties={"path": {"type": "string"}},
            required=("path",),
        ),
    )
    result = invoke(
        registry,
        arguments={"path": "var/artifacts/link.txt"},
        workspace_root=workspace,
    )
    assert result["error"]["code"] == "policy_denied"
    assert "outside" in result["error"]["message"]


# ---------------------------------------------------------------------------
# Host and repo confinement
# ---------------------------------------------------------------------------


def test_out_of_scope_host_is_denied_at_invoke(tmp_path: Path) -> None:
    registry = load(
        tmp_path,
        policy_manifest(
            effects=("read", "network"),
            hosts=("api.example.com",),
            properties={"host": {"type": "string"}},
            required=("host",),
        ),
    )
    result = invoke(
        registry,
        arguments={"host": "evil.example"},
        workspace_root=tmp_path,
    )
    assert result["error"]["code"] == "policy_denied"
    assert "not in the declared allowlist" in result["error"]["message"]


def test_allowlisted_host_is_allowed(tmp_path: Path) -> None:
    registry = load(
        tmp_path,
        policy_manifest(
            effects=("read", "network"),
            hosts=("api.example.com",),
            properties={"host": {"type": "string"}},
            required=("host",),
        ),
    )
    result = invoke(
        registry,
        arguments={"host": "api.example.com"},
        workspace_root=tmp_path,
    )
    assert result["ok"] is True, result


def test_out_of_scope_repo_is_denied_at_invoke(tmp_path: Path) -> None:
    registry = load(
        tmp_path,
        policy_manifest(
            effects=("read", "network"),
            hosts=("api.github.com",),
            repos=("acme/app",),
            properties={"repo": {"type": "string"}},
            required=("repo",),
        ),
    )
    result = invoke(
        registry,
        arguments={"repo": "evil/app"},
        workspace_root=tmp_path,
    )
    assert result["error"]["code"] == "policy_denied"
    assert "repo" in result["error"]["message"]


def test_allowlisted_repo_is_allowed(tmp_path: Path) -> None:
    registry = load(
        tmp_path,
        policy_manifest(
            effects=("read", "network"),
            hosts=("api.github.com",),
            repos=("acme/app",),
            properties={"repo": {"type": "string"}},
            required=("repo",),
        ),
    )
    result = invoke(
        registry,
        arguments={"repo": "acme/app"},
        workspace_root=tmp_path,
    )
    assert result["ok"] is True, result


def test_policy_is_reevaluated_between_two_invokes(tmp_path: Path) -> None:
    registry = load(
        tmp_path,
        policy_manifest(
            effects=("read", "network"),
            hosts=("api.example.com",),
            properties={"host": {"type": "string"}},
            required=("host",),
        ),
    )
    arguments = {"host": "api.example.com"}
    first = invoke(
        registry,
        arguments=arguments,
        workspace_root=tmp_path,
        policy=PolicyConfig(
            workspace_root=tmp_path,
            allowed_hosts=frozenset({"api.example.com"}),
        ),
    )
    second = invoke(
        registry,
        arguments=arguments,
        workspace_root=tmp_path,
        policy=PolicyConfig(
            workspace_root=tmp_path,
            allowed_hosts=frozenset({"other.example"}),
        ),
    )
    assert first["ok"] is True
    assert second["error"]["code"] == "policy_denied"
    assert "blocked by deployment policy" in second["error"]["message"]


# ---------------------------------------------------------------------------
# Deny is terminal, never approvable
# ---------------------------------------------------------------------------


def test_deny_is_not_approvable(tmp_path: Path) -> None:
    registry = load(
        tmp_path,
        policy_manifest(
            callable="write_path_probe",
            effects=("write",),
            paths=("${WORKSPACE}/var/out/**",),
            requires_approval=True,
            properties={"path": {"type": "string"}},
            required=("path",),
        ),
    )
    approval_calls: list[Any] = []

    async def approve(request: Any) -> bool:
        approval_calls.append(request)
        return True

    result = invoke(
        registry,
        arguments={"path": "var/secret/answer.txt"},
        workspace_root=tmp_path,
        approval=approve,
    )
    assert result["error"]["code"] == "policy_denied"
    assert "approval" not in result
    assert approval_calls == []


def test_manifest_approval_still_runs_for_in_scope_write(tmp_path: Path) -> None:
    registry = load(
        tmp_path,
        policy_manifest(
            callable="write_path_probe",
            effects=("write",),
            paths=("${WORKSPACE}/var/out/**",),
            requires_approval=True,
            properties={"path": {"type": "string"}},
            required=("path",),
        ),
    )

    async def approve(_request: Any) -> bool:
        return True

    result = invoke(
        registry,
        arguments={"path": "var/out/task.json"},
        workspace_root=tmp_path,
        approval=approve,
    )
    assert result["ok"] is True, result
    assert result["output"]["value"] == "task.json"


def test_undeclared_effect_is_denied(tmp_path: Path) -> None:
    registry = load(
        tmp_path,
        policy_manifest(
            callable="require_effect_probe",
            properties={"effect": {"type": "string"}},
            required=("effect",),
        ),
    )
    result = invoke(
        registry,
        arguments={"effect": "write"},
        workspace_root=tmp_path,
    )
    assert result["error"]["code"] == "policy_denied"
    assert "not declared" in result["error"]["message"]


# ---------------------------------------------------------------------------
# Egress chokepoint
# ---------------------------------------------------------------------------


def test_egress_client_denies_unlisted_host_before_transport(tmp_path: Path) -> None:
    registry = load(
        tmp_path,
        policy_manifest(
            callable="egress_probe",
            effects=("read", "network"),
            hosts=("api.example.com",),
            properties={"url": {"type": "string"}},
            required=("url",),
        ),
    )
    result = invoke(
        registry,
        arguments={"url": "https://evil.example/steal"},
        workspace_root=tmp_path,
    )
    assert result["error"]["code"] == "policy_denied"
    assert "not in the declared allowlist" in result["error"]["message"]


def test_egress_client_denies_unsafe_method_on_read_skill(tmp_path: Path) -> None:
    registry = load(
        tmp_path,
        policy_manifest(
            callable="egress_probe",
            effects=("read", "network"),
            hosts=("api.example.com",),
            properties={
                "url": {"type": "string"},
                "method": {"type": "string"},
            },
            required=("url",),
        ),
    )
    result = invoke(
        registry,
        arguments={"url": "https://api.example.com/v1", "method": "POST"},
        workspace_root=tmp_path,
    )
    assert result["error"]["code"] == "policy_denied"
    assert "requires the 'write' effect" in result["error"]["message"]


def test_egress_client_allows_declared_host_and_audits(tmp_path: Path) -> None:
    handlers.EGRESS_TRANSPORT = lambda method, url, headers, body, timeout: EgressResponse(
        status=200,
        headers={"content-type": "text/plain"},
        body=b"hello",
        url=url,
    )
    registry = load(
        tmp_path,
        policy_manifest(
            callable="egress_probe",
            effects=("read", "network"),
            hosts=("api.example.com",),
            properties={"url": {"type": "string"}},
            required=("url",),
        ),
    )
    audit = AuditLog(tmp_path / "audit.jsonl")
    result = invoke(
        registry,
        arguments={"url": "https://api.example.com/v1"},
        workspace_root=tmp_path,
        audit=audit,
    )
    assert result["ok"] is True, result
    assert result["output"]["value"] == "200:5"
    egress = audit.entries(event="skill_egress")
    assert egress[-1]["decision"] == "allow"
    assert egress[-1]["outcome"] == "completed"
    assert egress[-1]["extra"]["host"] == "api.example.com"


def test_direct_socket_is_blocked_even_for_allowlisted_host(tmp_path: Path) -> None:
    registry = load(
        tmp_path,
        policy_manifest(
            callable="direct_socket_probe",
            effects=("read", "network"),
            hosts=("api.example.com",),
            properties={
                "host": {"type": "string"},
                "port": {"type": "integer"},
            },
            required=("host",),
        ),
    )
    result = invoke(
        registry,
        arguments={"host": "api.example.com", "port": 443},
        workspace_root=tmp_path,
    )
    assert result["error"]["code"] == "policy_denied"
    assert "direct network access" in result["error"]["message"]


def test_direct_urlopen_is_blocked(tmp_path: Path) -> None:
    registry = load(
        tmp_path,
        policy_manifest(
            callable="direct_urlopen_probe",
            effects=("read", "network"),
            hosts=("api.example.com",),
            properties={"url": {"type": "string"}},
            required=("url",),
        ),
    )
    result = invoke(
        registry,
        arguments={"url": "https://api.example.com/v1"},
        workspace_root=tmp_path,
    )
    assert result["error"]["code"] == "policy_denied"
    assert "direct network access" in result["error"]["message"]


def test_network_guard_is_transparent_outside_an_invocation() -> None:
    import socket

    assert socket.getaddrinfo("localhost", 0, type=socket.SOCK_STREAM)


def test_builtin_skills_do_not_perform_egress_directly() -> None:
    root = Path(__file__).resolve().parents[1] / "skills" / "builtin"
    forbidden = re.compile(
        r"(?m)^\s*(?:from|import)\s+"
        r"(socket|ssl|http\.client|urllib\.request|requests|httpx|aiohttp)\b"
        r"|\burlopen\s*\("
    )
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if forbidden.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(root)))
    assert offenders == []


# ---------------------------------------------------------------------------
# Audit and output validation
# ---------------------------------------------------------------------------


def test_skill_invoked_audit_is_content_free(tmp_path: Path) -> None:
    registry = load(tmp_path, policy_manifest())
    audit = AuditLog(tmp_path / "audit.jsonl")
    sentinel = "argument-sentinel-value"
    result = invoke(
        registry,
        arguments={"value": sentinel},
        workspace_root=tmp_path,
        audit=audit,
        turn_id="turn_1",
        actor="owner",
    )
    assert result["ok"] is True
    entries = audit.entries(event="skill_invoked")
    assert len(entries) == 1
    entry = entries[0]
    assert entry["tool"] == "demo.policy"
    assert entry["decision"] == "allow"
    assert entry["outcome"] == "ok"
    assert entry["turn_id"] == "turn_1"
    assert isinstance(entry["duration_ms"], int)
    assert entry["extra"]["skill"] == "demo.policy"
    assert entry["extra"]["version"] == "0.1.0"
    assert entry["extra"]["effects"] == ["read"]
    assert entry["extra"]["scope_decision"] == "allow"
    assert "arguments" not in entry
    raw = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert sentinel not in raw


def test_skill_invoked_audit_denial_has_category_but_no_target(tmp_path: Path) -> None:
    registry = load(
        tmp_path,
        policy_manifest(
            callable="read_path_probe",
            paths=("${WORKSPACE}/var/artifacts/**",),
            properties={"path": {"type": "string"}},
            required=("path",),
        ),
    )
    audit = AuditLog(tmp_path / "audit.jsonl")
    result = invoke(
        registry,
        arguments={"path": "var/secret/plan.txt"},
        workspace_root=tmp_path,
        audit=audit,
    )
    assert result["error"]["code"] == "policy_denied"
    entry = audit.entries(event="skill_invoked")[-1]
    assert entry["decision"] == "deny"
    assert entry["outcome"] == "policy_denied"
    assert entry["extra"]["policy_category"] == "path"
    raw = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "var/secret/plan.txt" not in raw


def test_output_validation_failure_is_audited_as_failed_invoke(tmp_path: Path) -> None:
    registry = load(
        tmp_path,
        policy_manifest(callable="bad_output"),
    )
    audit = AuditLog(tmp_path / "audit.jsonl")
    result = invoke(
        registry,
        arguments={"value": "anything"},
        workspace_root=tmp_path,
        audit=audit,
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_output"
    entry = audit.entries(event="skill_invoked")[-1]
    assert entry["outcome"] == "invalid_output"
    assert entry["decision"] == "allow"


def test_unknown_skill_is_audited(tmp_path: Path) -> None:
    registry = load(tmp_path, policy_manifest())
    audit = AuditLog(tmp_path / "audit.jsonl")
    result = invoke(
        registry,
        tool="demo.missing",
        arguments={},
        workspace_root=tmp_path,
        audit=audit,
    )
    assert result["error"]["code"] == "unknown_skill"
    entry = audit.entries(event="skill_invoked")[-1]
    assert entry["tool"] == "demo.missing"
    assert entry["outcome"] == "unknown_skill"


# ---------------------------------------------------------------------------
# Loader contract unchanged
# ---------------------------------------------------------------------------


def test_execute_effect_is_still_rejected(tmp_path: Path) -> None:
    manifest = policy_manifest(effects=("read", "execute"))
    with pytest.raises(SkillManifestError):
        load(tmp_path, manifest)

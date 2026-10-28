"""Contract tests for the skill loader and invocation runtime (KEL-5 / P0-4).

Golden manifest: ``tests/fixtures/skills/golden/manifest.json``.
One negative test per validation rule, plus runtime envelope coverage.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from skills import SkillContext, SkillManifestError, SkillRegistry
from tests.fixtures import handlers

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOLDEN_PATH = FIXTURES / "skills" / "golden" / "manifest.json"
BUILTIN_ROOT = Path(__file__).resolve().parents[1] / "skills" / "builtin"


def golden() -> dict[str, Any]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def write_root(tmp_path: Path, *manifests: Mapping[str, Any]) -> Path:
    root = tmp_path / "skills"
    root.mkdir()
    for index, manifest in enumerate(manifests):
        skill_dir = root / f"skill_{index}"
        skill_dir.mkdir()
        (skill_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def load_ok(tmp_path: Path, manifest: Mapping[str, Any]) -> SkillRegistry:
    return SkillRegistry.load(write_root(tmp_path, manifest))


def expect_violation(
    tmp_path: Path,
    manifest: Mapping[str, Any],
    field: str,
    contains: str | None = None,
) -> SkillManifestError:
    with pytest.raises(SkillManifestError) as info:
        load_ok(tmp_path, manifest)
    error = info.value
    fields = [violation.field for violation in error.violations]
    assert field in fields, f"expected violation at {field!r}, got {fields}\n{error}"
    if contains is not None:
        assert contains in str(error), str(error)
    return error


def invoke(
    registry: SkillRegistry,
    tool: str,
    arguments: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    return asyncio.run(registry.invoke(tool, arguments, **kwargs))


@pytest.fixture(autouse=True)
def _reset_handlers() -> None:
    handlers.reset()
    yield
    handlers.reset()


# ---------------------------------------------------------------------------
# Golden fixture loads
# ---------------------------------------------------------------------------


def test_golden_manifest_loads(tmp_path: Path) -> None:
    registry = load_ok(tmp_path, golden())
    assert registry.names() == ("demo.echo",)
    skill = registry.get("demo.echo")
    assert skill.tool_name == "demo.echo"
    assert skill.requires_approval is False
    assert skill.effects == ("read",)
    assert registry.root.is_dir()


# ---------------------------------------------------------------------------
# Structural rules (meta-schema)
# ---------------------------------------------------------------------------


def test_missing_required_field(tmp_path: Path) -> None:
    manifest = golden()
    del manifest["version"]
    expect_violation(tmp_path, manifest, "/version", "required property")


def test_invalid_name_format(tmp_path: Path) -> None:
    manifest = golden()
    manifest["name"] = "Demo.Echo"
    expect_violation(tmp_path, manifest, "/name")


def test_name_requires_namespace(tmp_path: Path) -> None:
    manifest = golden()
    manifest["name"] = "echo"
    expect_violation(tmp_path, manifest, "/name")


def test_invalid_version(tmp_path: Path) -> None:
    manifest = golden()
    manifest["version"] = "1.0"
    expect_violation(tmp_path, manifest, "/version")


def test_empty_triggers(tmp_path: Path) -> None:
    manifest = golden()
    manifest["triggers"] = []
    expect_violation(tmp_path, manifest, "/triggers")


def test_invalid_trigger_kind(tmp_path: Path) -> None:
    manifest = golden()
    manifest["triggers"][0]["kind"] = "cron"
    expect_violation(tmp_path, manifest, "/triggers/0/kind")


def test_empty_tools(tmp_path: Path) -> None:
    manifest = golden()
    manifest["tools"] = []
    expect_violation(tmp_path, manifest, "/tools")


def test_more_than_one_tool_rejected(tmp_path: Path) -> None:
    manifest = golden()
    manifest["tools"] = ["demo.echo", "demo.other"]
    expect_violation(tmp_path, manifest, "/tools")


def test_unknown_top_level_field(tmp_path: Path) -> None:
    manifest = golden()
    manifest["extra"] = True
    expect_violation(tmp_path, manifest, "", "Additional properties")


def test_input_schema_must_be_object(tmp_path: Path) -> None:
    manifest = golden()
    manifest["input_schema"] = {"type": "array"}
    expect_violation(tmp_path, manifest, "/input_schema/type")


def test_missing_manifest_file(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    (root / "empty_skill").mkdir(parents=True)
    with pytest.raises(SkillManifestError) as info:
        SkillRegistry.load(root)
    assert any("manifest.json is missing" in str(v) for v in info.value.violations)


def test_invalid_json_manifest(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill_dir = root / "broken"
    skill_dir.mkdir(parents=True)
    (skill_dir / "manifest.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(SkillManifestError) as info:
        SkillRegistry.load(root)
    assert "not valid JSON" in str(info.value)


def test_manifest_size_limit(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill_dir = root / "huge"
    skill_dir.mkdir(parents=True)
    (skill_dir / "manifest.json").write_text(" " * (256 * 1024 + 1), encoding="utf-8")
    with pytest.raises(SkillManifestError) as info:
        SkillRegistry.load(root)
    assert "limit is" in str(info.value)


def test_empty_skill_root(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    with pytest.raises(SkillManifestError) as info:
        SkillRegistry.load(root)
    assert "no skill directories" in str(info.value)


def test_missing_skill_root(tmp_path: Path) -> None:
    with pytest.raises(SkillManifestError) as info:
        SkillRegistry.load(tmp_path / "nope")
    assert "missing or not a directory" in str(info.value)


# ---------------------------------------------------------------------------
# Semantic / cross-field rules
# ---------------------------------------------------------------------------


def test_tool_name_must_equal_skill_name(tmp_path: Path) -> None:
    manifest = golden()
    manifest["tools"] = ["demo.other"]
    expect_violation(tmp_path, manifest, "/tools", "must equal the skill name")


def test_trigger_must_include_model_kind(tmp_path: Path) -> None:
    manifest = golden()
    manifest["triggers"] = [{"kind": "manual", "description": "Only on demand."}]
    expect_violation(tmp_path, manifest, "/triggers", "kind 'model'")


def test_invalid_input_json_schema(tmp_path: Path) -> None:
    manifest = golden()
    manifest["input_schema"] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"text": {"type": "nope"}},
    }
    error = expect_violation(tmp_path, manifest, "/input_schema/properties/text/type")
    assert "invalid JSON Schema" in str(error)


def test_input_schema_must_reject_extra_arguments(tmp_path: Path) -> None:
    manifest = golden()
    del manifest["input_schema"]["additionalProperties"]
    expect_violation(tmp_path, manifest, "/input_schema/additionalProperties")


def test_remote_schema_ref_rejected(tmp_path: Path) -> None:
    manifest = golden()
    manifest["input_schema"]["$ref"] = "https://example.com/schema.json"
    expect_violation(tmp_path, manifest, "/input_schema/$ref", "remote references")


def test_execute_effect_rejected(tmp_path: Path) -> None:
    manifest = golden()
    manifest["permissions"]["effects"] = ["read", "execute"]
    expect_violation(tmp_path, manifest, "/permissions/effects", "execute")


def test_network_effect_requires_hosts(tmp_path: Path) -> None:
    manifest = golden()
    manifest["permissions"]["effects"] = ["read", "network"]
    manifest["permissions"]["scopes"]["hosts"] = []
    expect_violation(tmp_path, manifest, "/permissions/scopes/hosts")


def test_host_must_be_bare_hostname(tmp_path: Path) -> None:
    manifest = golden()
    manifest["permissions"]["effects"] = ["read", "network"]
    manifest["permissions"]["scopes"]["hosts"] = ["https://api.example.com"]
    expect_violation(tmp_path, manifest, "/permissions/scopes/hosts/0")


def test_wildcard_host_allowed(tmp_path: Path) -> None:
    manifest = golden()
    manifest["permissions"]["effects"] = ["read", "network"]
    manifest["permissions"]["scopes"]["hosts"] = ["*.example.com"]
    load_ok(tmp_path, manifest)


def test_write_effect_requires_approval(tmp_path: Path) -> None:
    manifest = golden()
    manifest["permissions"]["effects"] = ["write"]
    manifest["permissions"]["scopes"]["paths"] = ["${WORKSPACE}/var/out/**"]
    manifest["permissions"]["requires_approval"] = False
    expect_violation(tmp_path, manifest, "/permissions/requires_approval")


def test_write_effect_requires_scope(tmp_path: Path) -> None:
    manifest = golden()
    manifest["permissions"]["effects"] = ["write"]
    manifest["permissions"]["requires_approval"] = True
    manifest["permissions"]["scopes"] = {"paths": [], "hosts": [], "repos": []}
    expect_violation(tmp_path, manifest, "/permissions/scopes")


def test_non_idempotent_requires_approval(tmp_path: Path) -> None:
    manifest = golden()
    manifest["idempotent"] = False
    expect_violation(tmp_path, manifest, "/permissions/requires_approval", "non-idempotent")


def test_path_scope_traversal_rejected(tmp_path: Path) -> None:
    manifest = golden()
    manifest["permissions"]["scopes"]["paths"] = ["../../etc/**"]
    expect_violation(tmp_path, manifest, "/permissions/scopes/paths/0")


def test_path_scope_must_be_workspace_relative(tmp_path: Path) -> None:
    manifest = golden()
    manifest["permissions"]["scopes"]["paths"] = ["/etc/**"]
    expect_violation(tmp_path, manifest, "/permissions/scopes/paths/0", "${WORKSPACE}")


def test_repo_scope_format(tmp_path: Path) -> None:
    manifest = golden()
    manifest["permissions"]["scopes"]["repos"] = ["nebius-nvidia"]
    expect_violation(tmp_path, manifest, "/permissions/scopes/repos/0")


def test_repo_star_allowed(tmp_path: Path) -> None:
    manifest = golden()
    manifest["permissions"]["scopes"]["repos"] = ["*"]
    load_ok(tmp_path, manifest)


def test_bad_secret_name(tmp_path: Path) -> None:
    manifest = golden()
    manifest["permissions"]["secrets"] = ["demo_token"]
    expect_violation(tmp_path, manifest, "/permissions/secrets/0")


def test_bad_effect_enum(tmp_path: Path) -> None:
    manifest = golden()
    manifest["permissions"]["effects"] = ["read", "fetch"]
    expect_violation(tmp_path, manifest, "/permissions/effects/1")


def test_timeout_bounds(tmp_path: Path) -> None:
    manifest = golden()
    manifest["timeout_ms"] = 5
    expect_violation(tmp_path, manifest, "/timeout_ms")


def test_cost_class_enum(tmp_path: Path) -> None:
    manifest = golden()
    manifest["cost_class"] = "cheap"
    expect_violation(tmp_path, manifest, "/cost_class")


# ---------------------------------------------------------------------------
# Entry resolution
# ---------------------------------------------------------------------------


def test_missing_entry_module(tmp_path: Path) -> None:
    manifest = golden()
    manifest["entry"]["module"] = "tests.fixtures.does_not_exist"
    expect_violation(tmp_path, manifest, "/entry/module", "cannot import")


def test_missing_entry_callable(tmp_path: Path) -> None:
    manifest = golden()
    manifest["entry"]["callable"] = "not_here"
    expect_violation(tmp_path, manifest, "/entry/callable", "no attribute")


def test_entry_must_be_async(tmp_path: Path) -> None:
    manifest = golden()
    manifest["entry"]["callable"] = "sync_only"
    expect_violation(tmp_path, manifest, "/entry/callable", "async function")


# ---------------------------------------------------------------------------
# Registry-level rules
# ---------------------------------------------------------------------------


def test_duplicate_skill_names_rejected(tmp_path: Path) -> None:
    root = write_root(tmp_path, golden(), golden())
    with pytest.raises(SkillManifestError) as info:
        SkillRegistry.load(root)
    assert any(
        violation.field == "/name" and "duplicate skill name" in violation.message
        for violation in info.value.violations
    )


def test_registry_is_all_or_nothing(tmp_path: Path) -> None:
    broken = golden()
    broken["name"] = "Demo.Broken"
    root = write_root(tmp_path, golden(), broken)
    with pytest.raises(SkillManifestError):
        SkillRegistry.load(root)


# ---------------------------------------------------------------------------
# Invocation runtime
# ---------------------------------------------------------------------------


def test_invoke_success(tmp_path: Path) -> None:
    registry = load_ok(tmp_path, golden())
    result = invoke(registry, "demo.echo", {"text": "hi"})
    assert result["ok"] is True
    assert result["skill"] == "demo.echo"
    assert result["output"] == {"echo": "hi", "chars": 2}
    assert isinstance(result["duration_ms"], int)


def test_unknown_skill(tmp_path: Path) -> None:
    registry = load_ok(tmp_path, golden())
    result = invoke(registry, "nope.nope", {})
    assert result["ok"] is False
    assert result["error"]["code"] == "unknown_skill"
    assert result["error"]["user_fixable"] is True
    assert result["error"]["retryable"] is False


def test_arguments_are_validated_before_execution(tmp_path: Path) -> None:
    registry = load_ok(tmp_path, golden())
    result = invoke(registry, "demo.echo", {"text": ""})
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"
    assert result["error"]["field"] == "/text"
    assert result["error"]["user_fixable"] is True
    assert result["error"]["details"]["errors"]


def test_unknown_argument_rejected(tmp_path: Path) -> None:
    registry = load_ok(tmp_path, golden())
    result = invoke(registry, "demo.echo", {"text": "hi", "surprise": True})
    assert result["error"]["code"] == "invalid_arguments"
    assert "surprise" in result["error"]["message"]


def test_non_object_arguments_rejected(tmp_path: Path) -> None:
    registry = load_ok(tmp_path, golden())
    result = invoke(registry, "demo.echo", "hello")
    assert result["error"]["code"] == "invalid_arguments"
    assert result["error"]["field"] == ""


def _approval_manifest() -> dict[str, Any]:
    manifest = golden()
    manifest["entry"]["callable"] = "side_effect"
    manifest["idempotent"] = False
    manifest["permissions"] = {
        "effects": ["write"],
        "scopes": {"paths": ["${WORKSPACE}/var/tasks/**"], "hosts": [], "repos": []},
        "requires_approval": True,
        "secrets": [],
    }
    manifest["output_schema"] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["created", "id"],
        "properties": {"created": {"type": "boolean"}, "id": {"type": "string"}},
    }
    return manifest


def test_approval_required_blocks_execution(tmp_path: Path) -> None:
    registry = load_ok(tmp_path, _approval_manifest())
    result = invoke(registry, "demo.echo", {"text": "do it"})
    assert result["ok"] is False
    assert result["error"]["code"] == "approval_required"
    assert result["approval"]["argument_hash"].startswith("sha256:")
    assert "arguments" not in result["approval"]
    assert handlers.CALLS == []


def test_approval_denied_leaves_no_side_effect(tmp_path: Path) -> None:
    registry = load_ok(tmp_path, _approval_manifest())

    async def deny(request: Any) -> bool:
        return False

    result = invoke(registry, "demo.echo", {"text": "do it"}, approval=deny)
    assert result["error"]["code"] == "approval_denied"
    assert handlers.CALLS == []


def test_approval_hook_failure_fails_closed(tmp_path: Path) -> None:
    registry = load_ok(tmp_path, _approval_manifest())

    async def broken(request: Any) -> bool:
        raise RuntimeError("gate down")

    result = invoke(registry, "demo.echo", {"text": "do it"}, approval=broken)
    assert result["error"]["code"] == "approval_denied"
    assert handlers.CALLS == []


def test_approved_call_executes(tmp_path: Path) -> None:
    registry = load_ok(tmp_path, _approval_manifest())
    seen: list[Any] = []

    async def approve(request: Any) -> bool:
        seen.append(request)
        return True

    result = invoke(registry, "demo.echo", {"text": "do it"}, approval=approve)
    assert result["ok"] is True
    assert result["output"]["created"] is True
    assert handlers.CALLS == [{"text": "do it"}]
    assert seen[0].effects == ("write",)
    assert seen[0].expires_at > seen[0].requested_at


def test_timeout_is_retryable(tmp_path: Path) -> None:
    manifest = golden()
    manifest["entry"]["callable"] = "slow"
    manifest["timeout_ms"] = 100
    registry = load_ok(tmp_path, manifest)
    result = invoke(registry, "demo.echo", {"text": "hi"})
    assert result["error"]["code"] == "timeout"
    assert result["error"]["retryable"] is True
    assert result["error"]["user_fixable"] is False


def test_handler_exception_is_typed(tmp_path: Path) -> None:
    manifest = golden()
    manifest["entry"]["callable"] = "raises"
    registry = load_ok(tmp_path, manifest)
    result = invoke(registry, "demo.echo", {"text": "hi"})
    assert result["error"]["code"] == "handler_error"
    assert result["error"]["retryable"] is False
    assert "Traceback" not in result["error"]["message"]


def test_invalid_output_is_rejected(tmp_path: Path) -> None:
    manifest = golden()
    manifest["entry"]["callable"] = "bad_output"
    registry = load_ok(tmp_path, manifest)
    result = invoke(registry, "demo.echo", {"text": "hi"})
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_output"
    assert result["error"]["details"]["errors"]


def test_output_must_be_an_object(tmp_path: Path) -> None:
    manifest = golden()
    manifest["entry"]["callable"] = "not_mapping"
    registry = load_ok(tmp_path, manifest)
    result = invoke(registry, "demo.echo", {"text": "hi"})
    assert result["error"]["code"] == "invalid_output"


def test_secrets_are_scrubbed_from_errors(tmp_path: Path) -> None:
    manifest = golden()
    manifest["entry"]["callable"] = "leaky"
    manifest["permissions"]["secrets"] = ["DEMO_TOKEN"]
    registry = load_ok(tmp_path, manifest)
    secret = "sk-demo-super-secret"
    result = invoke(registry, "demo.echo", {"text": "hi"}, secrets={"DEMO_TOKEN": secret})
    assert result["ok"] is False
    assert secret not in json.dumps(result)
    assert "[redacted]" in result["error"]["message"]


def test_context_repr_hides_secrets() -> None:
    context = SkillContext("demo.echo", "/tmp", {"DEMO_TOKEN": "sk-demo-super-secret"})
    assert "sk-demo-super-secret" not in repr(context)
    assert context.secret("DEMO_TOKEN") == "sk-demo-super-secret"
    assert context.scrub("value=sk-demo-super-secret") == "value=[redacted]"


# ---------------------------------------------------------------------------
# Built-in prototype skills
# ---------------------------------------------------------------------------


def test_builtin_skills_load() -> None:
    registry = SkillRegistry.load(BUILTIN_ROOT)
    assert registry.names() == (
        "docs.answer",
        "github.read",
        "schedule.create",
        "web.search",
    )
    assert registry.tools() == registry.names()
    for name in ("web.search", "github.read", "docs.answer"):
        assert "write" not in registry.get(name).effects
        assert registry.get(name).requires_approval is False
    scheduler = registry.get("schedule.create")
    assert scheduler.effects == ("write",)
    assert scheduler.requires_approval is True
    assert scheduler.idempotent is False


def test_builtin_network_skills_are_allowlisted() -> None:
    registry = SkillRegistry.load(BUILTIN_ROOT)
    assert registry.get("web.search").scopes["hosts"] == ("api.tavily.com",)
    assert registry.get("github.read").scopes["hosts"] == ("api.github.com",)
    assert registry.get("github.read").scopes["repos"] == ("Anshieee/nebius-nvidia",)


def test_builtin_stubs_return_not_implemented() -> None:
    registry = SkillRegistry.load(BUILTIN_ROOT)
    result = invoke(registry, "web.search", {"query": "nebius gpu"})
    assert result["error"]["code"] == "not_implemented"
    assert result["error"]["retryable"] is False


def test_builtin_stub_still_validates_arguments() -> None:
    registry = SkillRegistry.load(BUILTIN_ROOT)
    result = invoke(registry, "web.search", {"query": ""})
    assert result["error"]["code"] == "invalid_arguments"
    assert result["error"]["field"] == "/query"

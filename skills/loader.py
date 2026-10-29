"""Fail-fast skill loader and invocation runtime.

Contract: ``docs/SKILL_SPEC.md``. A skill is a directory containing
``manifest.json`` plus an async handler referenced by the manifest's ``entry``.
Adding a skill must not require editing this module.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import inspect
import json
import os
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Iterable, Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from openshell.policy import Decision, PolicyConfig, PolicyEngine

from .context import ApprovalRequest, SkillContext
from .errors import (
    SkillHandlerError,
    SkillManifestError,
    Violation,
)
from .policy import (
    GuardedSkillContext,
    InvocationPolicy,
    PolicyDenied,
    active_invocation,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from openshell.audit import AuditLog

__all__ = [
    "APPROVAL_TTL_SECONDS",
    "DEFAULT_SKILL_ROOT",
    "LoadedSkill",
    "SkillRegistry",
    "load_registry",
]

MANIFEST_FILENAME = "manifest.json"
MANIFEST_MAX_BYTES = 256 * 1024
SPEC_VERSION = "1.0"
APPROVAL_TTL_SECONDS = 600
DEFAULT_SKILL_ROOT = Path(__file__).resolve().parent / "builtin"
_META_SCHEMA_PATH = Path(__file__).resolve().parent / "manifest.schema.json"

ApprovalHook = Callable[[ApprovalRequest], Awaitable[bool]]

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}(\.[a-z][a-z0-9_]{1,31})+$")
_HOST_RE = re.compile(
    r"^(?:\*\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)
_REPO_RE = re.compile(r"^(?:\*|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)$")
_SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


def _load_meta_validator() -> Draft202012Validator:
    try:
        raw = json.loads(_META_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - packaging fault
        raise RuntimeError(
            f"skill manifest meta-schema unreadable at {_META_SCHEMA_PATH}: {exc}"
        ) from exc
    Draft202012Validator.check_schema(raw)
    return Draft202012Validator(raw)


_META_VALIDATOR = _load_meta_validator()


def _ptr(parts: Iterable[Any]) -> str:
    escaped = [str(p).replace("~", "~0").replace("/", "~1") for p in parts]
    return "/" + "/".join(escaped) if escaped else ""


def _error_pointer(error: Any, instance: Any) -> str:
    """Point a jsonschema error at the offending field, not its parent.

    jsonschema reports ``required`` errors at the object itself; for fail-fast
    messages the missing property name is the useful address.
    """

    path = list(error.absolute_path)
    if error.validator == "required" and isinstance(instance, Mapping):
        required = error.validator_value if isinstance(error.validator_value, list) else []
        missing = [name for name in required if name not in instance]
        if missing:
            path.append(missing[0])
    return _ptr(path)


def _ms(started: float) -> int:
    return int(round((time.perf_counter() - started) * 1000))


def _short(text: str, limit: int = 300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _label(data: Mapping[str, Any], dirname: str) -> str:
    name = data.get("name")
    return name if isinstance(name, str) and name else dirname


@dataclass(frozen=True)
class LoadedSkill:
    name: str
    version: str
    description: str
    manifest: Mapping[str, Any]
    manifest_path: Path
    handler: Callable[..., Awaitable[Mapping[str, Any]]]
    input_validator: Draft202012Validator
    output_validator: Draft202012Validator
    tool_names: tuple[str, ...]
    timeout_ms: int
    idempotent: bool
    requires_approval: bool
    effects: tuple[str, ...]
    scopes: Mapping[str, tuple[str, ...]]
    declared_secrets: tuple[str, ...]
    cost_class: str
    @property
    def tool_name(self) -> str:
        return self.tool_names[0]


def _read_manifest(path: Path, dirname: str) -> tuple[dict[str, Any] | None, list[Violation]]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        return None, [Violation(dirname, "", f"cannot stat manifest.json: {exc}")]
    if size > MANIFEST_MAX_BYTES:
        return None, [
            Violation(
                dirname,
                "",
                f"manifest.json is {size} bytes; limit is {MANIFEST_MAX_BYTES}",
            )
        ]
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, [Violation(dirname, "", f"cannot read manifest.json: {exc}")]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, [
            Violation(dirname, "", f"manifest.json is not valid JSON: {exc.msg} (line {exc.lineno})")
        ]
    if not isinstance(data, dict):
        return None, [Violation(dirname, "", "manifest.json must contain a JSON object")]
    return data, []


def _collect_refs(node: Any, pointer: str = "") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(node, Mapping):
        for key, value in node.items():
            child = f"{pointer}/{str(key).replace('~', '~0').replace('/', '~1')}"
            if key in {"$ref", "$dynamicRef"} and isinstance(value, str):
                found.append((child, value))
            found.extend(_collect_refs(value, child))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_collect_refs(value, f"{pointer}/{index}"))
    return found


def _check_schema(
    schema: Any, field_name: str, label: str, *, require_closed: bool
) -> list[Violation]:
    violations: list[Violation] = []
    if not isinstance(schema, Mapping):
        return [Violation(label, f"/{field_name}", "must be a JSON Schema object")]
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        pointer = _ptr(exc.path)
        return [
            Violation(
                label,
                f"/{field_name}{pointer}",
                f"invalid JSON Schema: {_short(exc.message)}",
            )
        ]
    for pointer, ref in _collect_refs(schema):
        if not ref.startswith("#"):
            violations.append(
                Violation(
                    label,
                    f"/{field_name}{pointer}",
                    f"remote references are not allowed ({ref!r}); inline or use local '#/' refs",
                )
            )
    if require_closed and schema.get("additionalProperties") is not False:
        violations.append(
            Violation(
                label,
                f"/{field_name}/additionalProperties",
                "input_schema must set additionalProperties=false at the root",
            )
        )
    return violations


def _check_entry(data: Mapping[str, Any], label: str) -> tuple[
    Callable[..., Awaitable[Mapping[str, Any]]] | None, list[Violation]
]:
    entry = data["entry"]
    module_name = entry["module"]
    callable_name = entry["callable"]
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - import can fail in arbitrary ways
        return None, [
            Violation(
                label,
                "/entry/module",
                f"cannot import entry module {module_name!r}: {type(exc).__name__}: {_short(exc)}",
            )
        ]
    handler = getattr(module, callable_name, None)
    if handler is None:
        return None, [
            Violation(label, "/entry/callable", f"module {module_name!r} has no attribute {callable_name!r}")
        ]
    if not inspect.iscoroutinefunction(handler):
        return None, [
            Violation(label, "/entry/callable", f"{module_name}.{callable_name} must be an async function")
        ]
    return handler, []


def _check_semantics(data: Mapping[str, Any], label: str) -> list[Violation]:
    violations: list[Violation] = []
    name = data["name"]
    tools = data["tools"]
    permissions = data["permissions"]
    effects = permissions["effects"]
    scopes = permissions["scopes"]
    requires_approval = permissions["requires_approval"]

    if name not in tools:
        violations.append(
            Violation(
                label,
                "/tools",
                f"v1.0 binds exactly one model-facing tool, and it must equal the skill name {name!r}",
            )
        )

    if "execute" in effects:
        violations.append(
            Violation(
                label,
                "/permissions/effects",
                "the 'execute' effect is forbidden in v1.0 (shell execution is deny-by-default)",
            )
        )

    if "write" in effects and not requires_approval:
        violations.append(
            Violation(
                label,
                "/permissions/requires_approval",
                "skills declaring the 'write' effect must set requires_approval=true",
            )
        )

    if "write" in effects and not (scopes["paths"] or scopes["repos"]):
        violations.append(
            Violation(
                label,
                "/permissions/scopes",
                "skills declaring the 'write' effect must allowlist at least one path or repo",
            )
        )

    if "network" in effects and not scopes["hosts"]:
        violations.append(
            Violation(
                label,
                "/permissions/scopes/hosts",
                "skills declaring the 'network' effect must allowlist at least one host",
            )
        )

    if not data["idempotent"] and not requires_approval:
        violations.append(
            Violation(
                label,
                "/permissions/requires_approval",
                "non-idempotent skills must set requires_approval=true",
            )
        )

    if not any(trigger.get("kind") == "model" for trigger in data["triggers"]):
        violations.append(
            Violation(
                label,
                "/triggers",
                "at least one trigger of kind 'model' is required so the kernel can expose the tool",
            )
        )

    for index, host in enumerate(scopes["hosts"]):
        pointer = f"/permissions/scopes/hosts/{index}"
        if "://" in host or "/" in host or ":" in host:
            violations.append(
                Violation(label, pointer, f"{host!r} must be a bare hostname (no scheme, port, or path)")
            )
        elif not _HOST_RE.match(host):
            violations.append(
                Violation(
                    label,
                    pointer,
                    f"{host!r} is not a valid hostname; only lowercase DNS names and a leading '*.' wildcard are allowed",
                )
            )

    for index, path in enumerate(scopes["paths"]):
        pointer = f"/permissions/scopes/paths/{index}"
        if "\\" in path or ".." in path.split("/"):
            violations.append(
                Violation(label, pointer, f"{path!r} must not contain '..' or backslashes")
            )
        elif not path.startswith("${WORKSPACE}/"):
            violations.append(
                Violation(
                    label,
                    pointer,
                    f"{path!r} must start with '${{WORKSPACE}}/' (workspace-relative scopes only)",
                )
            )

    for index, repo in enumerate(scopes["repos"]):
        if not _REPO_RE.match(repo):
            violations.append(
                Violation(
                    label,
                    f"/permissions/scopes/repos/{index}",
                    f"{repo!r} must be 'owner/name', '*' or a GitHub owner/repo pattern",
                )
            )

    for index, secret in enumerate(permissions["secrets"]):
        if not _SECRET_NAME_RE.match(secret):
            violations.append(
                Violation(
                    label,
                    f"/permissions/secrets/{index}",
                    f"{secret!r} must be an env var name (A-Z, 0-9, underscore; 3-64 chars)",
                )
            )

    violations.extend(_check_schema(data["input_schema"], "input_schema", label, require_closed=True))
    violations.extend(_check_schema(data["output_schema"], "output_schema", label, require_closed=False))
    return violations


class SkillRegistry:
    """Immutable set of loaded skills plus the validated invocation pipeline.

    ``policy`` and ``audit`` are optional registry-wide defaults. A per-call
    ``invoke(..., policy=…)`` lets a deployment tighten policy without a
    restart; the manifest's declared scope can never be widened.
    """

    def __init__(
        self,
        skills: Mapping[str, LoadedSkill],
        root: Path,
        *,
        policy: "PolicyConfig | PolicyEngine | None" = None,
        audit: "AuditLog | None" = None,
    ):
        self.root = Path(root)
        self._skills: dict[str, LoadedSkill] = dict(skills)
        self._by_tool: dict[str, LoadedSkill] = {
            tool: skill for skill in self._skills.values() for tool in skill.tool_names
        }
        self._policy = policy
        self._audit = audit

    # -- discovery ---------------------------------------------------------

    @classmethod
    def load(
        cls,
        root: str | Path | None = None,
        *,
        policy: "PolicyConfig | PolicyEngine | None" = None,
        audit: "AuditLog | None" = None,
    ) -> "SkillRegistry":
        root_path = Path(root) if root is not None else DEFAULT_SKILL_ROOT
        if not root_path.is_dir():
            raise SkillManifestError(
                root_path,
                [Violation("*", "", f"skill root {str(root_path)!r} is missing or not a directory")],
            )

        dirs = sorted(
            p
            for p in root_path.iterdir()
            if p.is_dir() and not p.name.startswith((".", "_"))
        )
        if not dirs:
            raise SkillManifestError(
                root_path, [Violation("*", "", f"no skill directories found under {str(root_path)!r}")]
            )

        violations: list[Violation] = []
        loaded: dict[str, LoadedSkill] = {}
        origin: dict[str, str] = {}

        for skill_dir in dirs:
            dirname = skill_dir.name
            manifest_path = skill_dir / MANIFEST_FILENAME
            if not manifest_path.is_file():
                violations.append(Violation(dirname, "", f"{MANIFEST_FILENAME} is missing"))
                continue

            data, read_violations = _read_manifest(manifest_path, dirname)
            violations.extend(read_violations)
            if data is None:
                continue

            label = _label(data, dirname)
            structural = list(_META_VALIDATOR.iter_errors(data))
            if structural:
                structural.sort(key=lambda e: (list(e.absolute_path), e.message))
                for error in structural:
                    violations.append(
                        Violation(label, _error_pointer(error, data), error.message)
                    )
                continue

            semantic = _check_semantics(data, label)
            handler, entry_violations = _check_entry(data, label)
            semantic.extend(entry_violations)
            violations.extend(semantic)
            if semantic:
                continue

            name = data["name"]
            if name in loaded:
                violations.append(
                    Violation(
                        label,
                        "/name",
                        f"duplicate skill name {name!r}; already provided by {origin[name]!r}",
                    )
                )
                continue

            permissions = data["permissions"]
            skill = LoadedSkill(
                name=name,
                version=data["version"],
                description=data["description"],
                manifest=data,
                manifest_path=manifest_path,
                handler=handler,  # type: ignore[arg-type]
                input_validator=Draft202012Validator(data["input_schema"]),
                output_validator=Draft202012Validator(data["output_schema"]),
                tool_names=tuple(data["tools"]),
                timeout_ms=int(data["timeout_ms"]),
                idempotent=bool(data["idempotent"]),
                requires_approval=bool(permissions["requires_approval"]),
                effects=tuple(permissions["effects"]),
                scopes={
                    key: tuple(values) for key, values in permissions["scopes"].items()
                },
                declared_secrets=tuple(permissions["secrets"]),
                cost_class=data["cost_class"],
            )
            loaded[name] = skill
            origin[name] = dirname

        if violations:
            raise SkillManifestError(root_path, violations)

        return cls(loaded, root_path, policy=policy, audit=audit)

    # -- introspection -----------------------------------------------------

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._skills))

    def tools(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_tool))

    def get(self, skill: str) -> LoadedSkill:
        try:
            return self._skills[skill]
        except KeyError:
            raise KeyError(f"skill {skill!r} is not loaded") from None

    def __contains__(self, skill: object) -> bool:
        return skill in self._skills

    def __len__(self) -> int:
        return len(self._skills)

    # -- runtime -----------------------------------------------------------

    async def invoke(
        self,
        tool: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        approval: ApprovalHook | None = None,
        secrets: Mapping[str, str] | None = None,
        workspace_root: str | Path | None = None,
        policy: "PolicyConfig | PolicyEngine | None" = None,
        audit: "AuditLog | None" = None,
        turn_id: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        effective_audit = audit if audit is not None else self._audit
        skill = self._by_tool.get(tool)
        if skill is None:
            _audit_invocation(
                effective_audit,
                tool=tool,
                skill_name=tool,
                version=None,
                effects=(),
                decision="deny",
                outcome="unknown_skill",
                duration_ms=_ms(started),
                turn_id=turn_id,
                actor=actor,
            )
            return _error_envelope(
                tool,
                tool,
                "unknown_skill",
                f"Unknown skill {tool!r}.",
                retryable=False,
                user_fixable=True,
                duration_ms=_ms(started),
            )

        explicit_root = Path(workspace_root) if workspace_root is not None else None
        config = _resolve_policy_config(
            policy if policy is not None else self._policy, explicit_root
        )
        invocation_policy = InvocationPolicy.from_skill(skill, config)
        env: Mapping[str, str] = os.environ if secrets is None else secrets
        inner = SkillContext(
            skill.name,
            config.workspace_root,
            {name: env[name] for name in skill.declared_secrets if name in env},
        )
        ctx = GuardedSkillContext(
            inner,
            invocation_policy,
            audit=effective_audit,
            turn_id=turn_id,
        )

        def finish(
            envelope: dict[str, Any],
            *,
            decision: str,
            outcome: str,
            policy_category: str | None = None,
            approval_hash: str | None = None,
        ) -> dict[str, Any]:
            _audit_invocation(
                effective_audit,
                tool=tool,
                skill_name=skill.name,
                version=skill.version,
                effects=skill.effects,
                decision=decision,
                outcome=outcome,
                duration_ms=_ms(started),
                turn_id=turn_id,
                actor=actor,
                policy_category=policy_category,
                approval_hash=approval_hash,
            )
            return envelope

        args: Mapping[str, Any] = {} if arguments is None else arguments
        if not isinstance(args, Mapping):
            return finish(
                _error_envelope(
                    skill.name,
                    tool,
                    "invalid_arguments",
                    f"Arguments for {tool!r} must be a JSON object.",
                    retryable=False,
                    user_fixable=True,
                    field="",
                    duration_ms=_ms(started),
                ),
                decision="allow",
                outcome="invalid_arguments",
            )

        arg_errors = _schema_errors(skill.input_validator, args)
        if arg_errors:
            first = arg_errors[0]
            return finish(
                _error_envelope(
                    skill.name,
                    tool,
                    "invalid_arguments",
                    ctx.scrub(
                        f"Invalid arguments for {tool!r}: {first['field'] or '/'}: {first['message']}"
                    ),
                    retryable=False,
                    user_fixable=True,
                    field=first["field"],
                    details={"errors": _scrub_errors(arg_errors, ctx)},
                    duration_ms=_ms(started),
                ),
                decision="allow",
                outcome="invalid_arguments",
            )

        # KEL-20: concrete arguments are checked against the manifest on every
        # call, before approval. A deny here is terminal and never produces an
        # approval request.
        try:
            verdict = invocation_policy.evaluate_arguments(args)
        except PolicyDenied as exc:
            return finish(
                _policy_denied_envelope(skill, tool, exc, ctx, _ms(started)),
                decision="deny",
                outcome="policy_denied",
                policy_category=_policy_category(exc),
            )

        if verdict.decision is Decision.DENY:
            exc = PolicyDenied(verdict.reason, verdict.blast_radius)
            return finish(
                _policy_denied_envelope(skill, tool, exc, ctx, _ms(started)),
                decision="deny",
                outcome="policy_denied",
                policy_category=_policy_category(exc),
            )

        # Policy may ask for approval (an in-scope write), but the manifest's
        # requires_approval remains the only thing that produces a prompt. A
        # policy requirement without the manifest flag is a fail-closed deny.
        if (
            verdict.decision is Decision.REQUIRE_APPROVAL
            and not skill.requires_approval
        ):
            exc = PolicyDenied(
                "policy requires approval but the skill does not declare it",
                ("approval:undeclared",),
            )
            return finish(
                _policy_denied_envelope(skill, tool, exc, ctx, _ms(started)),
                decision="deny",
                outcome="policy_denied",
                policy_category="approval",
            )

        if skill.requires_approval:
            request = _build_approval_request(skill, args)
            envelope_approval = request.to_dict()
            if approval is None:
                return finish(
                    _error_envelope(
                        skill.name,
                        tool,
                        "approval_required",
                        f"Skill {tool!r} needs approval before it can run.",
                        retryable=False,
                        user_fixable=True,
                        approval=envelope_approval,
                        duration_ms=_ms(started),
                    ),
                    decision=verdict.decision.value,
                    outcome="approval_required",
                    approval_hash=request.argument_hash,
                )
            try:
                allowed = await approval(request)
            except Exception:  # noqa: BLE001 - a broken gate must fail closed
                allowed = False
            if not allowed:
                return finish(
                    _error_envelope(
                        skill.name,
                        tool,
                        "approval_denied",
                        f"Approval for {tool!r} was denied; nothing ran.",
                        retryable=False,
                        user_fixable=False,
                        duration_ms=_ms(started),
                    ),
                    decision=verdict.decision.value,
                    outcome="approval_denied",
                    approval_hash=request.argument_hash,
                )

        try:
            with active_invocation(
                invocation_policy,
                tool=tool,
                audit=effective_audit,
                turn_id=turn_id,
            ):
                output = await asyncio.wait_for(
                    skill.handler(dict(args), ctx), timeout=skill.timeout_ms / 1000
                )
        except PolicyDenied as exc:
            return finish(
                _policy_denied_envelope(skill, tool, exc, ctx, _ms(started)),
                decision="deny",
                outcome="policy_denied",
                policy_category=_policy_category(exc),
            )
        except asyncio.TimeoutError:
            return finish(
                _error_envelope(
                    skill.name,
                    tool,
                    "timeout",
                    f"Skill {tool!r} exceeded its {skill.timeout_ms} ms timeout.",
                    retryable=True,
                    user_fixable=False,
                    duration_ms=_ms(started),
                ),
                decision=verdict.decision.value,
                outcome="timeout",
            )
        except SkillHandlerError as exc:
            return finish(
                _error_envelope(
                    skill.name,
                    tool,
                    exc.code,
                    ctx.scrub(exc.message),
                    retryable=exc.retryable,
                    user_fixable=exc.user_fixable,
                    details=_scrub_obj(exc.details, ctx) if exc.details else None,
                    duration_ms=_ms(started),
                ),
                decision=verdict.decision.value,
                outcome=exc.code,
            )
        except Exception as exc:  # noqa: BLE001 - handler failures never escape
            return finish(
                _error_envelope(
                    skill.name,
                    tool,
                    "handler_error",
                    ctx.scrub(f"Skill {tool!r} failed: {type(exc).__name__}: {_short(exc)}"),
                    retryable=False,
                    user_fixable=False,
                    duration_ms=_ms(started),
                ),
                decision=verdict.decision.value,
                outcome="handler_error",
            )

        if not isinstance(output, Mapping):
            return finish(
                _error_envelope(
                    skill.name,
                    tool,
                    "invalid_output",
                    f"Skill {tool!r} returned {type(output).__name__}; a JSON object is required.",
                    retryable=False,
                    user_fixable=False,
                    duration_ms=_ms(started),
                ),
                decision=verdict.decision.value,
                outcome="invalid_output",
            )
        try:
            json.dumps(output)
        except (TypeError, ValueError):
            return finish(
                _error_envelope(
                    skill.name,
                    tool,
                    "invalid_output",
                    f"Skill {tool!r} returned a value that is not JSON-serializable.",
                    retryable=False,
                    user_fixable=False,
                    duration_ms=_ms(started),
                ),
                decision=verdict.decision.value,
                outcome="invalid_output",
            )

        output_errors = _schema_errors(skill.output_validator, output)
        if output_errors:
            first = output_errors[0]
            return finish(
                _error_envelope(
                    skill.name,
                    tool,
                    "invalid_output",
                    ctx.scrub(
                        f"Skill {tool!r} returned output that violates its schema: "
                        f"{first['field'] or '/'}: {first['message']}"
                    ),
                    retryable=False,
                    user_fixable=False,
                    field=first["field"],
                    details={"errors": _scrub_errors(output_errors, ctx)},
                    duration_ms=_ms(started),
                ),
                decision=verdict.decision.value,
                outcome="invalid_output",
            )

        return finish(
            {
                "ok": True,
                "skill": skill.name,
                "tool": tool,
                "duration_ms": _ms(started),
                "idempotent": skill.idempotent,
                "output": dict(output),
            },
            decision=verdict.decision.value,
            outcome="ok",
        )


def _resolve_policy_config(
    policy: "PolicyConfig | PolicyEngine | None", workspace_root: Path | None
) -> PolicyConfig:
    """Normalise a per-call policy without ever widening the manifest scope."""

    if policy is None:
        return PolicyConfig(workspace_root=workspace_root or Path.cwd())
    if isinstance(policy, PolicyEngine):
        config = policy.config
    elif isinstance(policy, PolicyConfig):
        config = policy
    else:  # pragma: no cover - defensive: caller wiring bug
        raise TypeError("policy must be a PolicyConfig, PolicyEngine, or None")
    if workspace_root is not None and Path(config.workspace_root) != Path(
        workspace_root
    ):
        config = replace(config, workspace_root=workspace_root)
    # A skill's declared paths are a hard boundary. The exec gate's opt-in
    # escalation for writes outside scope must not turn a manifest escape into
    # an approvable request.
    if config.allow_write_outside_scope:
        config = replace(config, allow_write_outside_scope=False)
    return config


def _policy_category(exc: PolicyDenied) -> str:
    """Coarse category for the audit line; never the raw target."""

    for item in exc.blast_radius:
        text = str(item)
        if text.startswith(("net:", "egress:")):
            return "network"
        if text.startswith("fs:"):
            return "path"
        if text.startswith("repo:"):
            return "repo"
        if text.startswith("effect:"):
            return "effect"
        if text.startswith("approval:"):
            return "approval"
        if text.startswith("arg:"):
            return "argument"
    return "policy"


def _policy_denied_envelope(
    skill: LoadedSkill,
    tool: str,
    exc: PolicyDenied,
    ctx: SkillContext,
    duration_ms: int,
) -> dict[str, Any]:
    reason = ctx.scrub(exc.reason)
    return _error_envelope(
        skill.name,
        tool,
        "policy_denied",
        f"Policy denied {tool!r}: {reason}",
        retryable=False,
        user_fixable=False,
        details={
            "decision": "deny",
            "reason": reason,
            "blast_radius": [ctx.scrub(str(item)) for item in exc.blast_radius],
        },
        duration_ms=duration_ms,
    )


def _audit_invocation(
    audit: "AuditLog | None",
    *,
    tool: str,
    skill_name: str,
    version: str | None,
    effects: Iterable[str],
    decision: str,
    outcome: str,
    duration_ms: int,
    turn_id: str | None,
    actor: str | None,
    policy_category: str | None = None,
    approval_hash: str | None = None,
) -> None:
    """Write the content-free ``skill_invoked`` line; never fail the call."""

    if audit is None:
        return
    extra: dict[str, Any] = {
        "skill": skill_name,
        "version": version,
        "effects": list(effects),
        "scope_decision": decision,
    }
    if policy_category is not None:
        extra["policy_category"] = policy_category
    if approval_hash is not None:
        extra["approval_argument_hash"] = approval_hash
    try:
        audit.record(
            "skill_invoked",
            turn_id=turn_id,
            tool=tool,
            actor=actor,
            decision=decision,
            outcome=outcome,
            duration_ms=duration_ms,
            extra=extra,
        )
    except Exception:  # noqa: BLE001 - a full disk must not end an invocation
        return


def _schema_errors(
    validator: Draft202012Validator, instance: Any, limit: int = 10
) -> list[dict[str, str]]:
    errors = sorted(
        validator.iter_errors(instance),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    return [
        {"field": _ptr(error.absolute_path), "message": _short(error.message)}
        for error in errors[:limit]
    ]


def _scrub_errors(errors: list[dict[str, str]], ctx: SkillContext) -> list[dict[str, str]]:
    return [
        {"field": item["field"], "message": ctx.scrub(item["message"])} for item in errors
    ]


def _scrub_obj(value: Any, ctx: SkillContext) -> Any:
    if isinstance(value, str):
        return ctx.scrub(value)
    if isinstance(value, Mapping):
        return {key: _scrub_obj(item, ctx) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_obj(item, ctx) for item in value]
    return value


def _build_approval_request(skill: LoadedSkill, arguments: Mapping[str, Any]) -> ApprovalRequest:
    canonical = json.dumps(
        arguments, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    now = datetime.now(timezone.utc)
    return ApprovalRequest(
        skill=skill.name,
        tool=skill.tool_name,
        arguments=dict(arguments),
        argument_hash="sha256:" + hashlib.sha256(canonical).hexdigest(),
        effects=skill.effects,
        scopes=skill.scopes,
        manifest_version=str(skill.manifest.get("manifest_version", SPEC_VERSION)),
        requested_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=APPROVAL_TTL_SECONDS)).isoformat(),
    )


def _error_envelope(
    skill: str,
    tool: str,
    code: str,
    message: str,
    *,
    retryable: bool,
    user_fixable: bool,
    duration_ms: int,
    field: str | None = None,
    details: Mapping[str, Any] | None = None,
    approval: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "retryable": retryable,
        "user_fixable": user_fixable,
    }
    if field is not None:
        error["field"] = field
    if details:
        error["details"] = dict(details)
    envelope: dict[str, Any] = {
        "ok": False,
        "skill": skill,
        "tool": tool,
        "duration_ms": duration_ms,
        "error": error,
    }
    if approval is not None:
        envelope["approval"] = dict(approval)
    return envelope


def load_registry(root: str | Path | None = None) -> SkillRegistry:
    """Convenience wrapper used at startup: ``registry = load_registry()``."""

    return SkillRegistry.load(root)

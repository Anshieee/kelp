"""Default-deny capability policy.

A tool declares effects and scope. The engine answers allow / deny / require
approval before anything runs. Shell execution is a separate, explicitly enabled
capability: even when enabled it always requires approval.

This module makes no I/O and holds no state.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Sequence

__all__ = [
    "Capability",
    "Decision",
    "Effect",
    "PolicyConfig",
    "PolicyEngine",
    "PolicyVerdict",
    "host_allowed",
]


class Effect(str, Enum):
    READ = "read"
    WRITE = "write"
    NETWORK = "network"
    EXECUTE = "execute"


class Decision(str, Enum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


_RANK = {Decision.ALLOW: 0, Decision.REQUIRE_APPROVAL: 1, Decision.DENY: 2}


@dataclass(frozen=True)
class Capability:
    tool: str
    effects: frozenset[Effect]
    #: ``${WORKSPACE}/…`` globs, same grammar as ``docs/SKILL_SPEC.md`` §8.2.
    paths: tuple[str, ...] = ()
    hosts: tuple[str, ...] = ()
    repos: tuple[str, ...] = ()


@dataclass(frozen=True)
class PolicyConfig:
    workspace_root: Path
    allowed_hosts: frozenset[str] = frozenset()
    allowed_commands: frozenset[str] = frozenset()
    #: Shell execution stays off until a deployment opts in. Even then it needs
    #: a human approval and an allowlisted argv[0].
    allow_exec: bool = False
    #: Writes outside the declared path scope are denied by default. Turning
    #: this on converts them into an explicit approval request instead.
    allow_write_outside_scope: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_root", Path(self.workspace_root).resolve())


@dataclass(frozen=True)
class PolicyVerdict:
    decision: Decision
    reason: str
    blast_radius: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW

    @property
    def requires_approval(self) -> bool:
        return self.decision is Decision.REQUIRE_APPROVAL


def host_allowed(host: str, patterns: Iterable[str]) -> bool:
    """Exact host or a leading ``*.`` wildcard; no scheme, port, or path."""

    candidate = host.strip().lower().rstrip(".")
    if not candidate or "://" in candidate or "/" in candidate or ":" in candidate:
        return False
    for pattern in patterns:
        normalised = pattern.strip().lower().rstrip(".")
        if normalised.startswith("*."):
            suffix = normalised[2:]
            if suffix and candidate.endswith("." + suffix) and candidate != suffix:
                return True
        elif candidate == normalised:
            return True
    return False


class PolicyEngine:
    def __init__(self, config: PolicyConfig):
        self.config = config

    # -- entry point -------------------------------------------------------

    def evaluate(
        self,
        capability: Capability,
        *,
        target: str | Path | None = None,
        host: str | None = None,
        argv: Sequence[str] | None = None,
        cwd: str | Path | None = None,
    ) -> PolicyVerdict:
        effects = {_effect_value(effect) for effect in capability.effects}
        if not effects:
            return PolicyVerdict(Decision.DENY, "capability declares no effects")
        unknown = effects - {effect.value for effect in Effect}
        if unknown:
            return PolicyVerdict(
                Decision.DENY,
                f"unknown effect(s): {', '.join(sorted(unknown))}",
            )

        verdicts: list[PolicyVerdict] = []
        if Effect.READ.value in effects:
            verdicts.append(self._read(capability, target))
        if Effect.WRITE.value in effects:
            verdicts.append(self._write(capability, target))
        if Effect.NETWORK.value in effects:
            verdicts.append(self._network(capability, host))
        if Effect.EXECUTE.value in effects:
            verdicts.append(self._execute(argv, cwd))

        worst = max(verdicts, key=lambda verdict: _RANK[verdict.decision])
        blast_radius = tuple(
            dict.fromkeys(
                item for verdict in verdicts for item in verdict.blast_radius
            )
        )
        return PolicyVerdict(worst.decision, worst.reason, blast_radius)

    # -- per-effect checks -------------------------------------------------

    def _read(self, capability: Capability, target: str | Path | None) -> PolicyVerdict:
        if target is None:
            return PolicyVerdict(Decision.DENY, "read requires a target path")
        resolved = self._resolve(target)
        if resolved is None:
            return PolicyVerdict(
                Decision.DENY,
                f"read target {target!s} is outside the workspace",
                (f"fs:read:{target}",),
            )
        if not self._path_in_scope(capability.paths, resolved):
            return PolicyVerdict(
                Decision.DENY,
                f"read target {self._relative(resolved)} is outside the declared scope",
                (f"fs:read:{self._relative(resolved)}",),
            )
        return PolicyVerdict(
            Decision.ALLOW,
            f"read allowed for {self._relative(resolved)}",
            (f"fs:read:{self._relative(resolved)}",),
        )

    def _write(self, capability: Capability, target: str | Path | None) -> PolicyVerdict:
        if target is None:
            return PolicyVerdict(Decision.DENY, "write requires a target path")
        resolved = self._resolve(target)
        if resolved is None:
            return PolicyVerdict(
                Decision.DENY,
                f"write target {target!s} is outside the workspace",
                (f"fs:write:{target}",),
            )
        relative = self._relative(resolved)
        if not self._path_in_scope(capability.paths, resolved):
            if self.config.allow_write_outside_scope:
                return PolicyVerdict(
                    Decision.REQUIRE_APPROVAL,
                    f"write to {relative} is outside the declared scope and needs approval",
                    (f"fs:write:{relative}", "scope:outside"),
                )
            return PolicyVerdict(
                Decision.DENY,
                f"write to {relative} is outside the declared scope",
                (f"fs:write:{relative}",),
            )
        return PolicyVerdict(
            Decision.REQUIRE_APPROVAL,
            f"write to {relative} needs approval",
            (f"fs:write:{relative}",),
        )

    def _network(self, capability: Capability, host: str | None) -> PolicyVerdict:
        if not host:
            return PolicyVerdict(Decision.DENY, "network access requires a host")
        if not host_allowed(host, capability.hosts):
            return PolicyVerdict(
                Decision.DENY,
                f"host {host!r} is not in the declared allowlist",
                (f"net:{host}",),
            )
        return PolicyVerdict(
            Decision.ALLOW,
            f"host {host!r} is allowlisted",
            (f"net:{host}",),
        )

    def _execute(self, argv: Sequence[str] | None, cwd: str | Path | None) -> PolicyVerdict:
        if not self.config.allow_exec:
            return PolicyVerdict(
                Decision.DENY,
                "shell execution is disabled (deny-by-default)",
            )
        if not argv:
            return PolicyVerdict(Decision.DENY, "exec requires a non-empty argv")
        command = str(argv[0])
        if command not in self.config.allowed_commands:
            return PolicyVerdict(
                Decision.DENY,
                f"command {command!r} is not in the exec allowlist",
                (f"exec:{Path(command).name}",),
            )
        if cwd is None:
            return PolicyVerdict(Decision.DENY, "exec requires a working directory")
        resolved = self._resolve(cwd)
        if resolved is None:
            return PolicyVerdict(
                Decision.DENY,
                f"working directory {cwd!s} is outside the workspace",
            )
        return PolicyVerdict(
            Decision.REQUIRE_APPROVAL,
            f"command {command!r} needs approval",
            (f"exec:{Path(command).name}", f"cwd:{self._relative(resolved)}"),
        )

    # -- path helpers ------------------------------------------------------

    def _resolve(self, value: str | Path) -> Path | None:
        if "\x00" in str(value):
            return None
        candidate = Path(value)
        root = self.config.workspace_root
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            resolved = candidate.resolve(strict=False)
        except OSError:
            return None
        try:
            resolved.relative_to(root)
        except ValueError:
            return None
        return resolved

    def _relative(self, path: Path) -> str:
        try:
            return path.relative_to(self.config.workspace_root).as_posix()
        except ValueError:
            return str(path)

    def _path_in_scope(self, scopes: Sequence[str], resolved: Path) -> bool:
        relative = self._relative(resolved)
        for scope in scopes:
            if not scope.startswith("${WORKSPACE}/"):
                continue
            pattern = scope[len("${WORKSPACE}/") :]
            if not pattern:
                # "${WORKSPACE}/" admits the whole workspace.
                return True
            if fnmatch.fnmatchcase(relative, pattern):
                return True
            # "var/**" should also admit the directory itself.
            if pattern.endswith("/**") and relative == pattern[:-3]:
                return True
        return False


def _effect_value(effect: Effect | str) -> str:
    return effect.value if isinstance(effect, Effect) else str(effect)

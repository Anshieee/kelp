"""Approval-gated shell execution.

Flow: ``request`` (policy check + approval object) -> human ``approve`` ->
``execute`` (atomic single-use consume, re-check, spawn without a shell).

Rules:

- Execution is denied unless the deployment opted in and argv[0] is allowlisted.
- Even then it always requires a human approval.
- The child gets a minimal environment; secret-looking variables are never
  forwarded, even if named in the allowlist.
- stdout/stderr are bounded and scrubbed before they are returned or audited.
- There is no ``shell=True`` path.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .approvals import ApprovalRequest, ApprovalStore
from .audit import AuditLog, is_secret_env_name
from .errors import ApprovalError, PolicyDenied
from .policy import Capability, Decision, Effect, PolicyEngine

__all__ = ["ExecGate", "ExecResult"]

DEFAULT_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR")
MAX_TIMEOUT_SECONDS = 600.0


@dataclass(frozen=True)
class ExecResult:
    ok: bool
    outcome: str
    request_id: str | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "outcome": self.outcome,
            "request_id": self.request_id,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


class ExecGate:
    def __init__(
        self,
        *,
        policy: PolicyEngine,
        approvals: ApprovalStore,
        audit: AuditLog,
        env_allowlist: Sequence[str] = DEFAULT_ENV_ALLOWLIST,
        max_output_bytes: int = 65_536,
        default_timeout_s: float = 30.0,
    ):
        self._policy = policy
        self._approvals = approvals
        self._audit = audit
        self._root = Path(policy.config.workspace_root)
        self._env_allowlist = tuple(env_allowlist)
        self._max_output_bytes = int(max_output_bytes)
        self._default_timeout_s = float(default_timeout_s)
        if self._max_output_bytes < 1:
            raise ValueError("max_output_bytes must be positive")
        if not 0 < self._default_timeout_s <= MAX_TIMEOUT_SECONDS:
            raise ValueError("default_timeout_s must be in (0, 600]")
        # Make the log's redaction aware of every secret-looking env value, so
        # a value cannot reach disk even if a command were to print it.
        self._audit.add_secret_values(
            value
            for name, value in os.environ.items()
            if is_secret_env_name(name)
        )

    # -- approval lifecycle ------------------------------------------------

    def request(
        self,
        argv: Sequence[str],
        *,
        turn_id: str,
        actor: str = "owner",
        cwd: str | Path | None = None,
        timeout_s: float | None = None,
        tool: str = "shell.exec",
    ) -> ApprovalRequest:
        command = _validate_command(argv)
        cwd_path = self._cwd_path(cwd)
        if not cwd_path.is_dir():
            raise ValueError(f"cwd is not a directory: {cwd_path}")
        timeout = self._timeout(timeout_s)

        capability = Capability(
            tool=tool,
            effects=frozenset({Effect.EXECUTE}),
            paths=("${WORKSPACE}/",),
        )
        verdict = self._policy.evaluate(capability, argv=command, cwd=cwd_path)
        self._audit.record(
            "exec_requested",
            turn_id=turn_id,
            tool=tool,
            actor=actor,
            decision=verdict.decision.value,
            command=command,
            extra={
                "cwd": str(cwd_path),
                "timeout_s": timeout,
                "blast_radius": list(verdict.blast_radius),
                "reason": verdict.reason,
            },
        )
        if verdict.decision is Decision.DENY:
            raise PolicyDenied(verdict.reason, verdict.blast_radius)

        request = self._approvals.create(
            tool=tool,
            command=command,
            effects=("execute",),
            blast_radius=verdict.blast_radius,
            actor=actor,
            metadata={"cwd": str(cwd_path), "timeout_s": timeout},
        )
        self._audit.record(
            "approval_requested",
            turn_id=turn_id,
            request_id=request.id,
            tool=tool,
            actor=actor,
            decision=verdict.decision.value,
            argument_hash=request.argument_hash,
            command=command,
            extra={"expires_at": request.expires_at, "single_use": True},
        )
        return request

    def approve(
        self, request_id: str, *, actor: str = "owner", turn_id: str | None = None
    ) -> ApprovalRequest:
        request = self._approvals.approve(request_id, actor=actor)
        self._audit.record(
            "approval_granted",
            turn_id=turn_id,
            request_id=request_id,
            tool=request.tool,
            actor=actor,
            decision="approved",
            argument_hash=request.argument_hash,
            command=request.command,
        )
        return request

    def deny(
        self, request_id: str, *, actor: str = "owner", turn_id: str | None = None
    ) -> ApprovalRequest:
        request = self._approvals.deny(request_id, actor=actor)
        self._audit.record(
            "approval_denied",
            turn_id=turn_id,
            request_id=request_id,
            tool=request.tool,
            actor=actor,
            decision="denied",
            argument_hash=request.argument_hash,
            command=request.command,
        )
        return request

    # -- execution ---------------------------------------------------------

    async def execute(
        self,
        request_id: str,
        *,
        turn_id: str | None = None,
        actor: str = "owner",
    ) -> ExecResult:
        try:
            request = self._approvals.consume(request_id)
        except ApprovalError as exc:
            self._audit.record(
                "exec_blocked",
                turn_id=turn_id,
                request_id=request_id,
                actor=actor,
                outcome=exc.code,
                extra={"reason": exc.message},
            )
            return ExecResult(
                ok=False,
                outcome=exc.code,
                request_id=request_id,
                error=exc.message,
            )

        cwd = Path(str(request.metadata.get("cwd", self._root)))
        timeout = float(request.metadata.get("timeout_s", self._default_timeout_s))
        capability = Capability(
            tool=request.tool,
            effects=frozenset({Effect.EXECUTE}),
            paths=("${WORKSPACE}/",),
        )
        verdict = self._policy.evaluate(capability, argv=request.command, cwd=cwd)
        if verdict.decision is Decision.DENY:
            self._audit.record(
                "exec_blocked",
                turn_id=turn_id,
                request_id=request_id,
                tool=request.tool,
                actor=actor,
                decision=verdict.decision.value,
                outcome="policy_denied",
                extra={"reason": verdict.reason},
            )
            return ExecResult(
                ok=False,
                outcome="policy_denied",
                request_id=request_id,
                error=verdict.reason,
            )

        env = self._safe_env()
        started = time.monotonic()
        self._audit.record(
            "exec_started",
            turn_id=turn_id,
            request_id=request_id,
            tool=request.tool,
            actor=actor,
            decision="approved",
            command=request.command,
            extra={"cwd": str(cwd), "timeout_s": timeout},
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *request.command,
                cwd=str(cwd),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            duration_ms = _elapsed_ms(started)
            self._audit.record(
                "exec_finished",
                turn_id=turn_id,
                request_id=request_id,
                tool=request.tool,
                actor=actor,
                outcome="spawn_error",
                duration_ms=duration_ms,
                extra={"error": type(exc).__name__},
            )
            return ExecResult(
                ok=False,
                outcome="spawn_error",
                request_id=request_id,
                duration_ms=duration_ms,
                error=f"Could not start command ({type(exc).__name__}).",
            )

        timed_out = False
        try:
            stdout_raw, stderr_raw = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            timed_out = True
            self._terminate(process)
            try:
                stdout_raw, stderr_raw = await process.communicate()
            except Exception:  # pragma: no cover - process already gone
                stdout_raw, stderr_raw = b"", b""

        duration_ms = _elapsed_ms(started)
        if timed_out:
            outcome = "timeout"
        elif process.returncode == 0:
            outcome = "completed"
        else:
            outcome = "failed"

        stdout = self._decode(stdout_raw)
        stderr = self._decode(stderr_raw)
        self._audit.record(
            "exec_finished",
            turn_id=turn_id,
            request_id=request_id,
            tool=request.tool,
            actor=actor,
            outcome=outcome,
            exit_code=process.returncode,
            duration_ms=duration_ms,
            extra={
                "stdout_sha256": _sha256(stdout_raw),
                "stderr_sha256": _sha256(stderr_raw),
                "stdout_bytes": len(stdout_raw),
                "stderr_bytes": len(stderr_raw),
                "truncated": len(stdout_raw) > self._max_output_bytes
                or len(stderr_raw) > self._max_output_bytes,
            },
        )
        return ExecResult(
            ok=outcome == "completed",
            outcome=outcome,
            request_id=request_id,
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            error="Command exceeded its timeout." if timed_out else None,
        )

    # -- internals ---------------------------------------------------------

    def _timeout(self, value: float | None) -> float:
        timeout = self._default_timeout_s if value is None else float(value)
        if not 0 < timeout <= MAX_TIMEOUT_SECONDS:
            raise ValueError(f"timeout_s must be in (0, {MAX_TIMEOUT_SECONDS:g}]")
        return timeout

    def _cwd_path(self, value: str | Path | None) -> Path:
        if value is not None and "\x00" in str(value):
            raise ValueError("cwd contains a NUL byte")
        candidate = Path(value) if value is not None else self._root
        if not candidate.is_absolute():
            candidate = self._root / candidate
        return candidate.resolve(strict=False)

    def _safe_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        for name in self._env_allowlist:
            value = os.environ.get(name)
            if value is not None and not is_secret_env_name(name):
                env[name] = value
        env.setdefault("PATH", os.defpath)
        env["KELP_EXEC_GATE"] = "1"
        return env

    def _decode(self, raw: bytes) -> str:
        truncated = len(raw) > self._max_output_bytes
        text = raw[: self._max_output_bytes].decode("utf-8", errors="replace")
        scrubbed = self._audit.scrub(text)
        text = scrubbed if isinstance(scrubbed, str) else str(scrubbed)
        return text + ("\n[output truncated]" if truncated else "")

    @staticmethod
    def _terminate(process: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except ProcessLookupError:
                pass


def _validate_command(argv: Sequence[str]) -> tuple[str, ...]:
    if isinstance(argv, (str, bytes)):
        raise ValueError("argv must be a sequence of arguments, not a shell string")
    try:
        command = tuple(argv)
    except TypeError:
        raise ValueError("argv must be a sequence of arguments") from None
    if not command:
        raise ValueError("argv must not be empty")
    if not isinstance(command[0], str) or not command[0]:
        raise ValueError("argv[0] must be a non-empty string")
    for index, part in enumerate(command):
        if not isinstance(part, str):
            raise ValueError(f"argv[{index}] must be a string")
        if any(character in part for character in ("\x00", "\n", "\r")):
            raise ValueError(f"argv[{index}] contains a control character")
        if len(part) > 8192:
            raise ValueError(f"argv[{index}] is too long")
    return command


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _elapsed_ms(started: float) -> int:
    return max(0, int(round((time.monotonic() - started) * 1000)))

"""Append-only audit log with mandatory secret redaction.

Every decision, approval, and execution writes one JSON line. The log stores a
command, an argument hash, and outcome metadata — never environment values,
stdout/stderr bodies, or secret material. Redaction happens before serialization
and is applied to every string field.

There is no rewrite or delete API. Rotation is performed by the application's
retention pass (``chat.retention``): it renames the active file to a timestamped
segment and expires whole segments. :meth:`AuditLog.entries` reads the active
file plus retained segments, oldest first, so a rotation never hides history
that the policy still keeps.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

__all__ = ["AuditLog", "SecretScrubber"]

_REDACTED = "[redacted]"
_MIN_SECRET_LEN = 4

_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_PROVIDER_KEY_RE = re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{4,}")
_KV_RE = re.compile(
    r"(?i)\b(token|secret|password|passwd|api[_-]?key|access[_-]?key|private[_-]?key)"
    r"\b(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)"
)
_SECRET_NAME_RE = re.compile(
    r"(?i)(token|secret|password|passwd|api[_-]?key|credential|authorization|private[_-]?key)"
)


def _jsonl_paths(path: Path) -> list[Path]:
    """Active JSONL file plus its timestamped rotated segments, oldest first."""

    pattern = re.compile(
        re.escape(path.name) + r"\.\d{8}T\d{12,}Z(?:\.\d{4,})?$"
    )
    try:
        entries = list(path.parent.iterdir())
    except OSError:
        return [path]
    segments = [entry for entry in entries if pattern.fullmatch(entry.name)]
    return sorted(segments, key=lambda entry: entry.name) + [path]


def is_secret_env_name(name: str) -> bool:
    return bool(_SECRET_NAME_RE.search(name))


class SecretScrubber:
    """Best-effort redaction for text headed to a log or a model.

    Two layers: known secret *values* (exact replacement) and common credential
    patterns. Exact replacement is the strong guarantee; patterns catch values
    the process was not told about.
    """

    def __init__(self, secret_values: Iterable[str] = ()):
        values = {
            str(value)
            for value in secret_values
            if isinstance(value, str) and len(value) >= _MIN_SECRET_LEN
        }
        # Longest first so a value that contains another is fully removed.
        self._values = tuple(sorted(values, key=len, reverse=True))

    @classmethod
    def from_env(cls, names: Iterable[str]) -> "SecretScrubber":
        return cls(os.environ[name] for name in names if name in os.environ)

    def scrub_text(self, text: str) -> str:
        if not isinstance(text, str):
            text = str(text)
        for value in self._values:
            text = text.replace(value, _REDACTED)
        text = _BEARER_RE.sub("Bearer " + _REDACTED, text)
        text = _PROVIDER_KEY_RE.sub(_REDACTED, text)
        text = _KV_RE.sub(lambda match: match.group(1) + match.group(2) + _REDACTED, text)
        return text

    def add_values(self, values: Iterable[str]) -> None:
        merged = set(self._values)
        merged.update(
            str(value)
            for value in values
            if isinstance(value, str) and len(value) >= _MIN_SECRET_LEN
        )
        self._values = tuple(sorted(merged, key=len, reverse=True))

    def scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.scrub_text(value)
        if isinstance(value, Mapping):
            return {
                self.scrub_text(str(key)): self.scrub(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.scrub(item) for item in value]
        return value


class AuditLog:
    def __init__(
        self,
        path: str | Path,
        *,
        secret_values: Iterable[str] = (),
        secret_names: Iterable[str] = (),
        now_fn: Any = None,
    ):
        self.path = Path(path)
        values = list(secret_values)
        values.extend(
            os.environ[name] for name in secret_names if name in os.environ
        )
        self._scrubber = SecretScrubber(values)
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()

    @classmethod
    def from_env(
        cls,
        path: str | Path,
        *,
        secret_names: Iterable[str] = (),
        now_fn: Any = None,
    ) -> "AuditLog":
        return cls(path, secret_names=secret_names, now_fn=now_fn)

    def scrub(self, value: Any) -> Any:
        return self._scrubber.scrub(value)

    def add_secret_values(self, values: Iterable[str]) -> None:
        self._scrubber.add_values(values)

    def record(
        self,
        event: str,
        *,
        turn_id: str | None = None,
        request_id: str | None = None,
        tool: str | None = None,
        actor: str | None = None,
        decision: str | None = None,
        argument_hash: str | None = None,
        command: Iterable[str] | None = None,
        exit_code: int | None = None,
        duration_ms: int | None = None,
        outcome: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {"ts": self._now().isoformat(), "event": event}
        fields = {
            "turn_id": turn_id,
            "request_id": request_id,
            "tool": tool,
            "actor": actor,
            "decision": decision,
            "argument_hash": argument_hash,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "outcome": outcome,
        }
        for key, value in fields.items():
            if value is not None:
                entry[key] = self._scrubber.scrub(value)
        if command is not None:
            entry["command"] = [self._scrubber.scrub_text(str(part)) for part in command]
        if extra:
            entry["extra"] = self._scrubber.scrub(dict(extra))

        line = json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str)
        with self._lock:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            is_new = not self.path.exists()
            with self.path.open("a", encoding="utf-8") as handle:
                if is_new:
                    try:
                        os.chmod(self.path, 0o600)
                    except OSError:
                        pass
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return entry

    def entries(
        self,
        *,
        turn_id: str | None = None,
        event: str | None = None,
    ) -> list[dict[str, Any]]:
        parsed: list[dict[str, Any]] = []
        for path in _jsonl_paths(self.path):
            try:
                raw = path.read_text(encoding="utf-8")
            except FileNotFoundError:  # a rotation raced this read
                continue
            parsed.extend(
                json.loads(line) for line in raw.splitlines() if line.strip()
            )
        if turn_id is not None:
            parsed = [entry for entry in parsed if entry.get("turn_id") == turn_id]
        if event is not None:
            parsed = [entry for entry in parsed if entry.get("event") == event]
        return parsed

    def __repr__(self) -> str:
        return f"AuditLog(path={str(self.path)!r})"

    __str__ = __repr__

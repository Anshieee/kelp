"""Per-turn usage accounting.

Every turn produces exactly one ``UsageRecord``: tokens in/out, estimated cost,
step and tool counts, duration, and outcome. Records are appended to a JSONL
file for the M3 cost guardrail. Unknown models keep ``estimated_cost_usd=None``
so the record is visibly incomplete instead of silently free.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

__all__ = [
    "InMemoryUsageStore",
    "JsonlUsageStore",
    "UsageRecord",
    "UsageStore",
]


@dataclass(frozen=True)
class UsageRecord:
    turn_id: str
    session_id: str | None
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost_usd: float | None
    cost_known: bool
    usage_reported: bool
    steps: int
    tool_calls: int
    started_at: str
    finished_at: str
    duration_ms: int
    outcome: str
    #: Correlated HTTP request that produced the turn (KEL-14). ``None`` for
    #: turns started outside an HTTP request (CLI, tests, recovery).
    request_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class UsageStore(Protocol):
    def record(self, record: UsageRecord) -> None: ...


class InMemoryUsageStore:
    """Test/dev store."""

    def __init__(self) -> None:
        self.records: list[UsageRecord] = []
        self._lock = threading.Lock()

    def record(self, record: UsageRecord) -> None:
        with self._lock:
            self.records.append(record)

    def __len__(self) -> int:
        return len(self.records)


class JsonlUsageStore:
    """Append-only JSONL store. Safe for concurrent writers within one process."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def record(self, record: UsageRecord) -> None:
        payload = json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":"))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            is_new = not self.path.exists()
            with self.path.open("a", encoding="utf-8") as handle:
                if is_new:
                    try:
                        os.chmod(self.path, 0o600)
                    except OSError:
                        pass
                handle.write(payload + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

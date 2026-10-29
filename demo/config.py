"""Paths, model name, and constants shared by the demo package.

The demo never reads the real environment: no API keys, no network. Everything
it needs comes from ``demo/fixtures`` and a per-run state directory (default
``var/demo``, which ``.gitignore`` already excludes).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

__all__ = ["AUTH_TOKEN", "DemoPaths", "MODEL", "REPO_ROOT"]

REPO_ROOT = Path(__file__).resolve().parent.parent
# Same identifier the kernel defaults to; the fixture provider ignores it.
MODEL = "nvidia/Nemotron-3_5-Super-VL"
# Local-only bearer token so the demo drives the real authenticated surface.
AUTH_TOKEN = "demo-owner-token"


@dataclass(frozen=True)
class DemoPaths:
    """One run's on-disk layout. ``workspace`` is what skills see."""

    state: Path

    @classmethod
    def build(cls, state: str | Path | None = None) -> "DemoPaths":
        root = Path(state) if state is not None else REPO_ROOT / "var" / "demo"
        paths = cls(root.expanduser().resolve())
        for directory in (
            paths.state,
            paths.workspace / "var" / "artifacts",
            paths.workspace / "var" / "scheduler",
            paths.skills,
            paths.receipts,
        ):
            directory.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(directory, 0o700)
            except OSError:  # pragma: no cover - platform dependent
                pass
        return paths

    @property
    def workspace(self) -> Path:
        return self.state

    @property
    def db(self) -> Path:
        return self.state / "chat.db"

    @property
    def artifacts(self) -> Path:
        return self.workspace / "var" / "artifacts"

    @property
    def scheduler_tasks(self) -> Path:
        return self.workspace / "var" / "scheduler" / "tasks.jsonl"

    @property
    def audit(self) -> Path:
        return self.state / "audit.jsonl"

    @property
    def skills(self) -> Path:
        return self.state / "skills"

    @property
    def receipts(self) -> Path:
        return self.state / "receipts"

    @property
    def report(self) -> Path:
        return self.receipts / "demo-report.md"

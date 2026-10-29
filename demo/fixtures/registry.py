"""Build the demo skill registry from the real manifests, with fixture entries.

Schemas, permissions, approvals, and scopes stay exactly what ``skills/builtin``
declares — only the ``entry.module`` is rewritten to a module under
``demo.handlers``. That keeps one source of truth: a contract change in a
builtin manifest is picked up by the demo automatically and still validated by
the real loader.
"""

from __future__ import annotations

import json
from pathlib import Path

from demo.config import REPO_ROOT
from skills.loader import SkillRegistry

__all__ = ["BUILTIN_ROOT", "HANDLERS", "build_registry", "fixture_manifest"]

BUILTIN_ROOT = REPO_ROOT / "skills" / "builtin"

HANDLERS: dict[str, str] = {
    "web.search": "demo.handlers.web_search",
    "docs.answer": "demo.handlers.docs_answer",
    "github.read": "demo.handlers.github_read",
    "schedule.create": "demo.handlers.schedule_create",
}


def fixture_manifest(source: Path) -> dict[str, object]:
    data = json.loads(source.read_text(encoding="utf-8"))
    name = data.get("name")
    module = HANDLERS.get(str(name))
    if module is None:
        raise RuntimeError(
            f"builtin skill {name!r} has no demo fixture handler; add one to "
            f"demo/fixtures/registry.py or exclude the skill"
        )
    data["entry"] = {"module": module, "callable": "run"}
    return data


def build_registry(destination: Path) -> SkillRegistry:
    """Write fixture manifests under ``destination`` and load them for real."""

    destination.mkdir(parents=True, exist_ok=True)
    sources = sorted(BUILTIN_ROOT.glob("*/manifest.json"))
    if not sources:
        raise RuntimeError(f"no builtin skill manifests under {BUILTIN_ROOT}")
    expected = set(HANDLERS)
    found: set[str] = set()
    for source in sources:
        data = fixture_manifest(source)
        name = str(data["name"])
        found.add(name)
        skill_dir = destination / source.parent.name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "manifest.json").write_text(
            json.dumps(data, indent=2) + "\n", encoding="utf-8"
        )
    if found != expected:
        raise RuntimeError(
            f"fixture handlers and builtin manifests disagree: "
            f"manifests={sorted(found)} handlers={sorted(expected)}"
        )
    return SkillRegistry.load(destination)

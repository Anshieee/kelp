"""P6: the support runbook covers every admin endpoint and every branch."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from chat.admin import ADMIN_ENDPOINTS

ROOT = Path(__file__).resolve().parents[1]
ADMIN_DOC = ROOT / "docs" / "ADMIN_API.md"
RUNBOOK = ROOT / "docs" / "SUPPORT_RUNBOOK.md"

#: Directories whose backticked tokens must resolve to a real file. Runtime
#: state (`var/...`), shell expansions, and placeholders are deliberately not
#: files in a checkout and are excluded below.
SOURCE_PREFIXES = (
    "docs/",
    "chat/",
    "tests/",
    "payments/",
    "kernel/",
    "openshell/",
    "deploy/",
    "skills/",
    "tools/",
    "scripts/",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_admin_api_document_exists() -> None:
    assert ADMIN_DOC.is_file()
    assert RUNBOOK.is_file()


@pytest.mark.parametrize("endpoint", ADMIN_ENDPOINTS)
def test_every_admin_endpoint_is_documented(endpoint: str) -> None:
    assert endpoint in _read(ADMIN_DOC), endpoint
    assert endpoint in _read(RUNBOOK), endpoint


def test_every_triage_branch_has_a_command() -> None:
    text = _read(RUNBOOK)
    start = text.index("## Triage tree")
    tail = text.find("\n## ", start + len("## Triage tree"))
    section = text[start:] if tail == -1 else text[start:tail]
    branches = re.split(r"\n### ", section)[1:]
    assert len(branches) >= 5, "expected a multi-branch triage tree"
    for branch in branches:
        heading = branch.splitlines()[0]
        assert heading.startswith("Branch "), heading
        assert "```bash" in branch, f"no bash command in {heading}"
        # A fenced block must be closed, and must contain at least one command
        # line rather than only prose.
        assert branch.count("```") >= 2, heading
        fenced = re.search(r"```bash\n(.*?)\n```", branch, re.DOTALL)
        assert fenced is not None, heading
        commands = [
            line.strip()
            for line in fenced.group(1).splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert commands, f"empty command block in {heading}"


def test_runbook_and_admin_doc_reference_existing_source_paths() -> None:
    text = _read(RUNBOOK) + "\n" + _read(ADMIN_DOC)
    checked: set[str] = set()
    for span in re.findall(r"`([^`\n]+)`", text):
        token = span.strip()
        if not token or any(char in token for char in "{}$*<> "):
            continue
        if token.startswith("/"):
            continue
        looks_like_source = token.startswith(SOURCE_PREFIXES) or token in {
            "Dockerfile",
            "docker-compose.yml",
            "pyproject.toml",
            "Makefile",
        }
        if not looks_like_source:
            continue
        path = (ROOT / token).resolve()
        assert path.exists(), f"referenced path does not exist: {token}"
        checked.add(token)
    assert "docs/SERVICE_CONTRACT.md" in checked
    assert "chat/admin.py" in checked


def test_markdown_links_resolve() -> None:
    for document in (ADMIN_DOC, RUNBOOK):
        for target in re.findall(r"\]\(([^)]+)\)", _read(document)):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            path = (document.parent / target.split("#", 1)[0]).resolve()
            assert path.exists(), f"{document.name}: broken link {target}"


def test_documents_have_balanced_code_fences() -> None:
    for document in (ADMIN_DOC, RUNBOOK):
        assert _read(document).count("```") % 2 == 0, document.name


def test_runbook_declares_the_no_infrastructure_scope() -> None:
    text = _read(RUNBOOK).lower()
    assert "does not provision databases" in text
    assert "vendor-specific" in text
    assert "terraform" not in text


def test_known_p5_interactions_are_documented() -> None:
    runbook = _read(RUNBOOK).lower()
    admin_doc = _read(ADMIN_DOC).lower()
    for text in (runbook, admin_doc):
        assert "twice" in text
        assert "max_turns" in text
        assert "max_runs" in text
        assert "plan snapshot" in text or "plan snapshots" in text

"""The built wheel must carry the on-disk contracts the runtime reads.

This is an opt-in artifact check, not a build step: ordinary test runs do not
build or download a wheel. Point ``KELP_TEST_WHEEL`` at an absolute wheel built
from this checkout and the archive is inspected for every file the fail-fast
skill loader and the existing chat package data depend on:

* ``skills/manifest.schema.json`` - the manifest meta-schema, read from disk
  next to ``loader.py`` when the registry is built;
* ``skills/builtin/<name>/manifest.json`` for every built-in skill tracked in
  the checkout - the loader refuses a builtin directory without one;
* ``chat/credential_inbox.html`` - pre-existing package data kept as a control,
  so a regression in the shared ``package-data`` table is caught too.

Listing the real zip is the point: the packaging bug this guards against is
invisible to source-tree tests, because the loader reads ``Path(__file__)``
siblings that exist in a checkout and vanish in an installed wheel.
"""

from __future__ import annotations

import os
import subprocess
import zipfile
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).resolve().parents[1]
WHEEL_ENV = "KELP_TEST_WHEEL"

META_SCHEMA = "skills/manifest.schema.json"
CHAT_HTML = "chat/credential_inbox.html"
BUILTIN_DIR = ROOT / "skills" / "builtin"


def _is_builtin_manifest(rel: PurePosixPath) -> bool:
    # skills/builtin/<skill>/manifest.json - direct children only, so fixture
    # and asset manifests elsewhere in the repo are never expected in a wheel.
    return (
        len(rel.parts) == 4
        and rel.parts[0] == "skills"
        and rel.parts[1] == "builtin"
        and rel.parts[3] == "manifest.json"
    )


def _builtin_manifests_from_filesystem() -> list[str]:
    return sorted(
        path.relative_to(ROOT).as_posix()
        for path in BUILTIN_DIR.glob("*/manifest.json")
        if path.is_file()
    )


def _builtin_manifests() -> list[str]:
    """Repo-relative POSIX paths of the *tracked* builtin manifests.

    ``git ls-files`` keeps an untracked scratch skill from turning this check
    red; a plain checkout without a git directory (``git archive`` export)
    falls back to the filesystem so the test still inspects the same files.
    """

    try:
        listed = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z", "--", "skills/builtin"],
            check=True,
            capture_output=True,
        ).stdout.decode("utf-8")
    except (OSError, subprocess.CalledProcessError):
        return _builtin_manifests_from_filesystem()
    manifests = sorted(
        rel
        for rel in (PurePosixPath(raw) for raw in listed.split("\0") if raw)
        if _is_builtin_manifest(rel)
    )
    return [rel.as_posix() for rel in manifests] or _builtin_manifests_from_filesystem()


def _failure(wheel: Path, missing: list[str]) -> str:
    lines = [
        f"wheel {wheel} is missing {len(missing)} required packaged file(s):",
        *(f"  - {rel}" for rel in missing),
        "",
        "These are declared under [tool.setuptools.package-data] in pyproject.toml.",
        f"Rebuild the wheel from this checkout; a stale wheel also fails here ({WHEEL_ENV}).",
    ]
    return "\n".join(lines)


def test_wheel_ships_skill_contracts_and_chat_package_data() -> None:
    raw = os.environ.get(WHEEL_ENV, "").strip()
    if not raw:
        pytest.skip(
            f"set {WHEEL_ENV}=/abs/path/to/kelp_assistant-*.whl to check wheel package data"
        )
    wheel = Path(raw)
    if not wheel.is_absolute():
        pytest.fail(f"{WHEEL_ENV} must be an absolute wheel path, got {raw!r}")
    if not wheel.is_file():
        pytest.fail(f"{WHEEL_ENV} points at {wheel}, which is not a file")

    manifests = _builtin_manifests()
    assert manifests, (
        f"no builtin manifests found under {BUILTIN_DIR}; "
        "this checkout cannot prove the wheel packages them"
    )

    expected = [META_SCHEMA, *manifests, CHAT_HTML]
    try:
        with zipfile.ZipFile(wheel) as archive:
            shipped = set(archive.namelist())
    except zipfile.BadZipFile as exc:
        pytest.fail(f"{WHEEL_ENV} points at {wheel}, not a readable wheel archive: {exc}")

    missing = [rel for rel in expected if rel not in shipped]
    assert not missing, _failure(wheel, missing)

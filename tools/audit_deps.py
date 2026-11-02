"""Dependency audit: declared runtime dependencies, checked for known holes.

`make audit` and the `deps` job in the CI gate both run this module. The
audited set is exactly the `[project].dependencies` list in `pyproject.toml`,
read at run time so the audit cannot drift from what ships. Development tools
(`pytest`, `ruff`, `mypy`, `pip-audit`) are pinned in the workflow instead:
they never reach a user, and pinning them keeps the gate itself stable.

The list is handed to `pip-audit` as a requirements file in a temporary
directory, so nothing in the working tree is written and no virtualenv is
mutated. The exit code is the gate: any advisory that matches a dependency
makes it non-zero, and an empty dependency list fails closed rather than
reporting a clean audit of nothing.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"


def declared_dependencies(pyproject: Path = PYPROJECT) -> list[str]:
    """The runtime dependency specs, in declaration order."""

    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    specs = data.get("project", {}).get("dependencies", [])
    return [str(spec) for spec in specs]


def requirements_text(dependencies: list[str]) -> str:
    """Render specs as the requirements file `pip-audit -r` expects."""

    return "".join(f"{spec}\n" for spec in dependencies)


def pip_audit_command(requirements: Path) -> list[str]:
    """The auditor invocation: this interpreter, this requirements file."""

    return [
        sys.executable,
        "-m",
        "pip_audit",
        "--requirement",
        str(requirements),
        "--progress-spinner",
        "off",
    ]


def main() -> int:
    dependencies = declared_dependencies()
    if not dependencies:
        print("audit: pyproject.toml declares no dependencies", file=sys.stderr)
        return 1

    print(f"audit: checking {len(dependencies)} declared dependencies")
    with tempfile.TemporaryDirectory(prefix="kelp-audit-") as workspace:
        requirements = Path(workspace) / "requirements.txt"
        requirements.write_text(requirements_text(dependencies), encoding="utf-8")
        result = subprocess.run(pip_audit_command(requirements), check=False)

    if result.returncode != 0:
        print("audit: a known vulnerability affects a declared dependency", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())

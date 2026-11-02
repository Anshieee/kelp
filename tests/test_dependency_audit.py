"""The dependency audit gate: declared dependencies only, and no waivers.

The audit itself needs the network and the PyPI advisory data, so this file
does not run it. It pins the properties that decide what the gate can and
cannot see, because each one is cheap to lose in a later edit:

* the audited set comes from `pyproject.toml` at run time, so it cannot drift
  from what ships, and an empty manifest fails instead of passing vacuously;
* `make audit` and the `deps` CI job both call the same module, so a local run
  proves what CI will see;
* the auditor is pinned, and neither the workflow nor the tool carries an
  `--ignore-vuln` waiver or a `continue-on-error`, so a match always reds the
  run rather than becoming a comment.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from tools import audit_deps

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
MAKEFILE = ROOT / "Makefile"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _project_dependencies() -> list[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return list(data["project"]["dependencies"])


def test_audited_set_is_exactly_the_declared_runtime_dependencies() -> None:
    assert audit_deps.declared_dependencies() == _project_dependencies()
    assert _project_dependencies(), "an empty manifest would audit nothing at all"


def test_requirements_text_is_one_spec_per_line() -> None:
    assert audit_deps.requirements_text(["jsonschema>=4.20,<5", "httpx"]) == (
        "jsonschema>=4.20,<5\nhttpx\n"
    )


def test_auditor_runs_in_this_interpreter_against_the_rendered_file() -> None:
    command = audit_deps.pip_audit_command(Path("/tmp/kelp-requirements.txt"))
    assert command[0] == sys.executable
    assert command[1:3] == ["-m", "pip_audit"]
    assert command[command.index("--requirement") + 1] == "/tmp/kelp-requirements.txt"
    assert "--progress-spinner" in command


def test_make_audit_target_runs_the_shared_module() -> None:
    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
    phony = [line for line in lines if line.startswith(".PHONY:")]
    assert phony and "audit" in phony[0].split()

    target = lines.index("audit:")
    body = []
    for line in lines[target + 1 :]:
        if line and not line.startswith("\t"):
            break
        body.append(line.strip())
    assert "python -m tools.audit_deps" in body


def test_ci_runs_the_audit_as_a_blocking_job() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "\n  deps:\n" in workflow
    assert "pip-audit==" in workflow
    assert "python -m tools.audit_deps" in workflow
    assert "continue-on-error" not in workflow
    assert "--ignore-vuln" not in workflow


def test_no_waiver_hides_a_match_in_the_tool() -> None:
    source = Path(audit_deps.__file__).read_text(encoding="utf-8")
    assert "--ignore-vuln" not in source
    assert "--skip-editable" not in source

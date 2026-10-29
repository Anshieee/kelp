"""KEL-16 M2-F: the deployment assets are what the runbook says they are.

These are static assertions, not a build: they fail when an asset drifts away
from the security posture and the build/run split that docs/DEPLOY.md promises.
The expensive proofs (image builds, boots, restore round-trips) stay in §17 of
the runbook where a human runs them once per release; this file guards the
properties that are cheap to lose in a later edit:

* the runtime is non-root, read-only, capability-free, and resource-bounded;
* no secret is accepted at build time, defaults to a value, or is committed in
  an example file;
* Compose never builds and never pulls, so a deploy runs the tag it names;
* the systemd unit loads the same tag file and also refuses to build;
* the runbook keeps its diagnostics quiet and its commands runnable.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "docker-compose.yml"
DOCKERIGNORE = ROOT / ".dockerignore"
UNIT = ROOT / "deploy" / "kelp.service"
CONTAINER_ENV = ROOT / "deploy" / "kelp.env.example"
COMPOSE_ENV = ROOT / "deploy" / "compose.env.example"
RUNBOOK = ROOT / "docs" / "DEPLOY.md"

#: Substrings that must never appear in a build-time directive or a template.
SECRET_MARKERS = ("NEBIUS_API_KEY", "KELP_AUTH_TOKEN", "KELP_CSRF_TOKEN")


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _directives(path: Path, keyword: str) -> list[str]:
    """Logical lines of ``keyword`` (backslash continuations joined)."""

    joined: list[str] = []
    pending = ""
    for raw in _lines(path):
        stripped = raw.strip()
        if pending:
            pending = f"{pending} {stripped.rstrip(chr(92)).strip()}"
        elif stripped.upper().startswith(keyword.upper()):
            pending = stripped
        else:
            continue
        if pending.endswith("\\"):
            pending = pending[:-1]
            continue
        joined.append(pending)
        pending = ""
    if pending:
        joined.append(pending)
    return joined


# -- Dockerfile --------------------------------------------------------------


def test_runtime_is_non_root_and_locked_down() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "USER 10001:10001" in text
    # No privilege regain, no root default after the drop.
    assert not re.search(r"^USER\s+(?:root|0)(?::0)?$", text, re.MULTILINE)
    assert "no-new-privileges" not in text  # that is Compose's job, not the image's
    assert 'VOLUME ["/data"]' in text
    assert "EXPOSE 8765" in text


def test_user_directive_precedes_the_entrypoint() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert text.index("USER 10001:10001") < text.index("CMD [")
    assert "ENTRYPOINT" not in text


def test_no_secret_is_a_build_argument_or_environment_default() -> None:
    for directive in _directives(DOCKERFILE, "ARG") + _directives(DOCKERFILE, "ENV"):
        for marker in SECRET_MARKERS:
            assert marker not in directive, directive


def test_image_copies_named_paths_only() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert not re.search(r"^(?:COPY|ADD)\s+\.\s", text, re.MULTILINE)
    assert "COPY --from=builder /opt/venv /opt/venv" in text
    assert "deploy/healthcheck.py" in text


def test_healthcheck_is_the_stdlib_probe() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "HEALTHCHECK" in text
    assert "/app/deploy/healthcheck.py" in text
    assert "--live" in text


def test_runtime_stage_installs_no_toolchain() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "apt-get" not in text
    assert "curl" not in text
    assert text.count("FROM ") == 2
    # The second stage is the runtime and is the slim image.
    builder, runtime = text.split("FROM ", 2)[1:]
    assert "AS runtime" in runtime
    assert "AS builder" in builder


# -- docker-compose.yml ------------------------------------------------------


def _is_commented(line: str) -> bool:
    return line.lstrip().startswith("#")


def test_compose_never_builds_and_requires_an_explicit_tag() -> None:
    lines = [line for line in _lines(COMPOSE) if not _is_commented(line)]
    body = "\n".join(lines)
    assert not re.search(r"^\s+build:\s*$", body, re.MULTILINE)
    assert "pull_policy: never" in body
    # Required interpolation: `${KELP_IMAGE:?…}`, never `${KELP_IMAGE:-…}`.
    assert re.search(r"\$\{KELP_IMAGE:\?[^}]*\}", body)
    assert not re.search(r"\$\{KELP_IMAGE:-", body)
    assert "image: ${KELP_IMAGE" in body


def test_compose_hardening_is_present() -> None:
    body = "\n".join(line for line in _lines(COMPOSE) if not _is_commented(line))
    for expected in (
        "read_only: true",
        "cap_drop:",
        "      - ALL",
        "no-new-privileges:true",
        'user: "10001:10001"',
        "pids_limit:",
        "mem_limit:",
        "cpus:",
        "restart: unless-stopped",
        "stop_grace_period: 30s",
        "init: true",
    ):
        assert expected in body, expected


def test_compose_tmpfs_is_not_executable() -> None:
    body = "\n".join(line for line in _lines(COMPOSE) if not _is_commented(line))
    assert re.search(r"/tmp:.*noexec", body)
    assert re.search(r"/tmp:.*nosuid", body)


def test_compose_publishes_on_loopback_only() -> None:
    body = "\n".join(line for line in _lines(COMPOSE) if not _is_commented(line))
    assert '"127.0.0.1:${KELP_PUBLISH_PORT:-8765}:8765"' in body
    assert "0.0.0.0:${KELP_PUBLISH_PORT" not in body


def test_compose_reads_the_container_env_file() -> None:
    body = "\n".join(line for line in _lines(COMPOSE) if not _is_commented(line))
    assert "env_file:" in body
    assert "      - deploy/kelp.env" in body
    # The container env file carries secrets; it is not the interpolation file.
    assert "/opt/kelp/.env" not in body


def test_compose_has_no_secret_value() -> None:
    for line in _lines(COMPOSE):
        if _is_commented(line):
            continue
        for marker in SECRET_MARKERS:
            assert marker not in line, line


def test_compose_healthcheck_matches_the_image() -> None:
    body = "\n".join(line for line in _lines(COMPOSE) if not _is_commented(line))
    assert 'test: ["CMD", "python", "/app/deploy/healthcheck.py", "--live"]' in body
    assert "start_period: 40s" in body
    assert "retries: 3" in body


def test_compose_pins_the_project_and_volume_names() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert text.splitlines()[0].startswith("#") or "name: kelp" in text
    assert "name: kelp" in text
    assert "name: kelp-data" in text


# -- .dockerignore -----------------------------------------------------------


@pytest.mark.parametrize(
    "pattern",
    [
        "**/.env",
        "**/.env.*",
        "deploy/*.env",
        "payments/.env.example",
        ".git/",
        "tests/",
        "evals/",
        "docs/",
        "var/",
        "**/*.db",
        "**/*.db-wal",
        "**/*.db-shm",
        "**/*.jsonl",
        ".venv/",
        "**/__pycache__/",
    ],
)
def test_dockerignore_excludes_the_build_context(pattern: str) -> None:
    patterns = {
        line.strip() for line in _lines(DOCKERIGNORE) if line.strip() and not _is_commented(line)
    }
    assert pattern in patterns


# -- deploy/kelp.service -----------------------------------------------------


def test_unit_never_builds_and_fails_without_the_tag() -> None:
    text = UNIT.read_text(encoding="utf-8")
    assert "ExecStart=/usr/bin/docker compose up -d --no-build --remove-orphans" in text
    assert "--build" not in text.replace("--no-build", "")
    assert "EnvironmentFile=-/opt/kelp/.env" in text
    guard = [line for line in _lines(UNIT) if line.startswith("ExecStartPre=")]
    assert len(guard) == 2
    assert "KELP_IMAGE" in guard[0]
    assert "exit 1" in guard[0]
    # The second check is what turns a pruned tag into a loud failure: `up`
    # with `--no-build` cannot build it and `pull_policy: never` cannot fetch it.
    assert "docker image inspect" in guard[1]
    assert "KELP_IMAGE" in guard[1]


def test_unit_stops_the_project_and_orders_after_docker() -> None:
    text = UNIT.read_text(encoding="utf-8")
    assert "ExecStop=/usr/bin/docker compose down" in text
    assert "Requires=docker.service" in text
    assert "After=docker.service" in text
    assert "WorkingDirectory=/opt/kelp" in text
    assert "WantedBy=multi-user.target" in text


def test_unit_does_not_carry_secrets_or_container_env() -> None:
    for line in _lines(UNIT):
        assert not line.strip().startswith("Environment=NEBIUS"), line
        for marker in SECRET_MARKERS:
            assert marker not in line, line
    assert "deploy/kelp.env" in UNIT.read_text(encoding="utf-8")


# -- env templates -----------------------------------------------------------


def test_container_env_example_has_empty_secrets() -> None:
    text = CONTAINER_ENV.read_text(encoding="utf-8")
    assert "\nNEBIUS_API_KEY=\n" in text
    assert "\nKELP_AUTH_TOKEN=\n" in text
    assert not re.search(r"^NEBIUS_API_KEY=.+\S", text, re.MULTILINE)
    assert not re.search(r"^KELP_AUTH_TOKEN=.+\S", text, re.MULTILINE)


def test_container_env_example_holds_no_deployment_tag() -> None:
    text = CONTAINER_ENV.read_text(encoding="utf-8")
    assert "KELP_IMAGE" not in text
    assert "KELP_PUBLISH_PORT" not in text
    assert "compose.env.example" in text


def test_compose_env_example_carries_the_tag_and_port_only() -> None:
    text = COMPOSE_ENV.read_text(encoding="utf-8")
    assert re.search(r"^KELP_IMAGE=kelp:\S+$", text, re.MULTILINE)
    assert re.search(r"^KELP_PUBLISH_PORT=\d+$", text, re.MULTILINE)
    for marker in SECRET_MARKERS:
        assert marker not in text
    # The placeholder must not be a tag the runbook ever builds.
    assert "kelp:replace-me" in text


# -- docs/DEPLOY.md ----------------------------------------------------------


def _section(number: str) -> str:
    """The body of ``## <number>.`` up to the next ``##`` heading."""

    text = RUNBOOK.read_text(encoding="utf-8")
    start = re.search(rf"^## {re.escape(number)}\.", text, re.MULTILINE)
    assert start is not None, f"section {number} is missing"
    tail = text[start.end() :]
    end = re.search(r"^## ", tail, re.MULTILINE)
    return tail[: end.start()] if end else tail


def test_runbook_never_prints_the_merged_environment() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    # `docker compose config` echoes secrets; only `-q` is allowed.
    assert not re.search(r"docker compose config(?! -q)", text)
    assert "docker compose config -q" in text
    assert "Never paste plain `compose config`" in text


def test_runbook_keeps_the_owner_token_out_of_argv() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "-H @\"$HDRS\"" in text
    assert "umask 077" in text
    assert 'Authorization: Bearer $KELP_AUTH_TOKEN"' not in text


def test_backup_section_defines_its_own_tag() -> None:
    section = _section("9")
    assert "export TAG=$(git rev-parse --short HEAD)" in section
    assert '"$IMG"' in section or "${IMG}" in section


def test_offline_eval_section_is_runnable_on_a_fresh_host() -> None:
    section = _section("12")
    assert "python3 -m venv .venv" in section
    assert ".venv/bin/pip install -q pytest httpx jsonschema" in section
    assert ".venv/bin/python -m pytest" in section
    assert "outside the container" in section


def test_deploy_steps_never_build_via_compose() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    for match in re.finditer(r"docker compose up -d[^\n]*", text):
        assert "--no-build" in match.group(0), match.group(0)
    assert "docker compose build" not in text

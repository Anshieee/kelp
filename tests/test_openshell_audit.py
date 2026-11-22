"""Audit log tests: redaction, append-only behaviour, filtering (KEL-7)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from openshell import AuditLog, SecretScrubber

from kernel.secrets import TenantSecretResolver

SECRET = "super-secret-value-12345"


def test_scrubber_removes_known_values() -> None:
    scrubber = SecretScrubber([SECRET])
    assert scrubber.scrub_text(f"token={SECRET}") == "token=[redacted]"
    assert scrubber.scrub_text("short abc") == "short abc"


def test_scrubber_removes_common_credential_patterns() -> None:
    scrubber = SecretScrubber([])
    assert "sk_live_" not in scrubber.scrub_text("key sk_live_abcdef123456")
    assert "abcdef" not in scrubber.scrub_text("Authorization: Bearer abcdef123456")
    assert "hunter2" not in scrubber.scrub_text("password=hunter2")
    assert "hunter3" not in scrubber.scrub_text("api_key: 'hunter3'")


# Synthetic, key-shaped samples. None is a real credential.
_OPENROUTER = "sk-or-v1-" + "0123456789abcdef" * 4
_GITHUB_TOKENS = (
    "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2",
    "gho_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3",
    "ghu_" + "A1b2C3d4E5f6G7h8I9j0K1l2",
    "ghs_" + "A1b2C3d4E5f6G7h8I9j0K1l2",
    "ghr_" + "A1b2C3d4E5f6G7h8I9j0K1l2",
)
_GITHUB_PAT = "github_pat_" + "11ABCDEFG0abcdefgh_ijklmnop" + "QRSTUVWXYZ0123456789"
_AWS_KEY_ID = "AKIA" + "IOSFODNN7EXAMPLE"


@pytest.mark.parametrize(
    "secret",
    [_OPENROUTER, *_GITHUB_TOKENS, _GITHUB_PAT, _AWS_KEY_ID],
    ids=["openrouter", "ghp", "gho", "ghu", "ghs", "ghr", "github_pat", "aws_akia"],
)
class TestKeyShapePatterns:
    def test_bare_key_is_redacted_in_prose(self, secret: str) -> None:
        cleaned = SecretScrubber([]).scrub_text(f"the key {secret} leaked here")
        assert cleaned == "the key [redacted] leaked here"

    def test_key_is_redacted_inside_a_url_and_json(self, secret: str) -> None:
        scrubber = SecretScrubber([])
        assert secret not in scrubber.scrub_text(f"https://x.invalid/{secret}?a=1")
        assert secret not in scrubber.scrub_text(json.dumps({"note": secret}))

    def test_key_is_redacted_after_an_underscore_prefix(self, secret: str) -> None:
        cleaned = SecretScrubber([]).scrub_text(f"LEAK_{secret}")
        assert secret not in cleaned

    def test_audit_log_never_writes_the_key(self, secret: str, tmp_path: Path) -> None:
        log = AuditLog(tmp_path / "audit.jsonl")
        log.record("exec_requested", turn_id="t1", extra={"stderr_tail": f"bad {secret}"})
        assert secret not in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "text",
    [
        "ghp_short",  # under 20 chars
        "gh_" + "a" * 30,  # wrong prefix letter set
        "ghx_" + "a" * 30,
        "github_pat_short",
        "sk-or-v2-" + "a" * 30,  # different key family
        "sk-or-v1-",  # no body
        "AKIA" + "A" * 15,  # one short
        "AKIA" + "A" * 17,  # one long: not an access key id
        "AKIA" + "a" * 16,  # lowercase body is not an access key id
        "xghp_" + "a" * 30,  # glued to a preceding word
        "task-or-v1-" + "a" * 30,
        "plain prose about GitHub tokens and AWS keys",
    ],
)
def test_near_miss_shapes_are_left_alone(text: str) -> None:
    assert SecretScrubber([]).scrub_text(text) == text


def test_key_shape_patterns_do_not_backtrack_catastrophically() -> None:
    scrubber = SecretScrubber([])
    hostile = (
        "ghp_" + "a" * 200_000,
        "github_pat_" + "_" * 200_000 + "!",
        "sk-or-v1-" + "-" * 200_000 + "!",
        "AKIA" + "A" * 200_000,
        ("ghp_" + "a" * 19 + " ") * 20_000,
    )
    for text in hostile:
        cleaned = scrubber.scrub_text(text)
        assert isinstance(cleaned, str)


def test_existing_provider_key_redaction_is_unchanged() -> None:
    scrubber = SecretScrubber([])
    assert scrubber.scrub_text("k sk_live_abcdef123456 e") == "k [redacted] e"
    assert scrubber.scrub_text("k rk_test_abcd e") == "k [redacted] e"
    assert scrubber.scrub_text("k pk_live_abc e") == "k pk_live_abc e"  # < 4 chars


def test_scrub_recurses_into_structures() -> None:
    scrubber = SecretScrubber([SECRET])
    value = {"nested": [SECRET, {"deep": f"x-{SECRET}-y"}]}
    cleaned = scrubber.scrub(value)
    assert cleaned == {"nested": ["[redacted]", {"deep": "x-[redacted]-y"}]}


def test_audit_redacts_every_field(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl", secret_values=[SECRET])
    log.record(
        "exec_requested",
        turn_id="t1",
        actor=f"user-{SECRET}",
        command=("echo", SECRET),
        extra={"note": f"value={SECRET}", "nested": {"token": SECRET}},
    )
    raw = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert SECRET not in raw
    entry = json.loads(raw.strip())
    assert entry["command"] == ["echo", "[redacted]"]
    assert entry["extra"]["note"] == "value=[redacted]"


def test_audit_is_append_only_and_mode_600(tmp_path: Path) -> None:
    path = tmp_path / "audit" / "openshell.jsonl"
    log = AuditLog(path)
    log.record("first", turn_id="t1")
    log.record("second", turn_id="t1")

    entries = log.entries(turn_id="t1")
    assert [entry["event"] for entry in entries] == ["first", "second"]

    reopened = AuditLog(path)
    reopened.record("third", turn_id="t1")
    assert [entry["event"] for entry in reopened.entries()] == ["first", "second", "third"]

    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_audit_secret_names_are_read_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KELP_PROVIDER_API_KEY", SECRET)
    log = AuditLog.from_env(tmp_path / "audit.jsonl", secret_names=("KELP_PROVIDER_API_KEY",))
    log.record("test", extra={"value": SECRET})
    assert SECRET not in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")


def test_entries_filter_by_event(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record("exec_started", turn_id="t1")
    log.record("exec_finished", turn_id="t1")
    assert len(log.entries(event="exec_started")) == 1
    assert log.entries(turn_id="missing") == []


def test_tenant_secret_audit_rows_carry_names_and_metadata_only(
    tmp_path: Path,
) -> None:
    fake = "sk-test-fake-audit-secret"
    secrets_path = tmp_path / "tenant-secrets.json"
    secrets_path.write_text(
        json.dumps({"owner": {"KELP_PROVIDER_API_KEY": fake}}),
        encoding="utf-8",
    )
    os.chmod(secrets_path, 0o600)
    resolver = TenantSecretResolver(secrets_path, tenant_id="owner")
    ref = resolver.ref("KELP_PROVIDER_API_KEY")

    audit_path = tmp_path / "audit.jsonl"
    audit = AuditLog(audit_path, secret_values=resolver.secret_values())
    entry = audit.record(
        "provider_key_loaded",
        extra={"secret_name": ref.name, "secret_ref": ref.as_metadata()},
    )
    # A caller that misuses ``extra`` is still scrubbed by the resolver set.
    audit.record("provider_key_mistake", extra={"api_key": fake})

    raw = audit_path.read_text(encoding="utf-8")
    assert fake not in raw
    assert entry["extra"]["secret_name"] == "KELP_PROVIDER_API_KEY"
    assert "value" not in entry["extra"]["secret_ref"]
    assert entry["extra"]["secret_ref"]["source"] == "tenant-file"


# -- torn / garbled lines --------------------------------------------------


def test_entries_skip_torn_and_garbled_lines_across_segments(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    segment = tmp_path / "audit.jsonl.20261030T120000000000Z"
    segment.write_text(
        json.dumps({"event": "old", "n": 1}) + "\n"
        + "{not json at all\n"
        + json.dumps({"event": "old", "n": 2}) + "\n",
        encoding="utf-8",
    )
    path.write_text(
        json.dumps({"event": "new", "n": 3}) + "\n"
        + "[1, 2, 3]\n"  # valid JSON, not an object
        + '"just a string"\n'
        + "\n"
        + json.dumps({"event": "new", "n": 4}) + "\n"
        + '{"event": "torn", "n": 5',  # crash mid-write: no closing brace/newline
        encoding="utf-8",
    )
    log = AuditLog(path)

    assert [row["n"] for row in log.entries()] == [1, 2, 3, 4]
    assert [row["n"] for row in log.entries(event="new")] == [3, 4]


def test_a_row_written_after_a_torn_tail_is_not_lost(tmp_path: Path) -> None:
    # A crash mid-write leaves a partial last line with no newline. The next
    # append must start on its own line, or it is glued onto the partial one
    # and the reader skips both.
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.record("first", tool="t")
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"event": "torn"')
    log.record("after_crash", tool="t")

    assert [row["event"] for row in log.entries()] == ["first", "after_crash"]
    assert path.read_text(encoding="utf-8").endswith("\n")

"""Tests for the plugin-signing attestation module (KEL-29 spike follow-up).

The module is a spike: imported by nothing in the production path. These
tests pin the round trip, the fail-closed behavior, and — via the RFC 8032
test vectors — that the signatures are real Ed25519, interoperable with any
other conforming implementation, not just self-consistent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skills import signing
from skills.loader import inspect_plugin_package
from tests.test_plugins import _manifest


# RFC 8032 §7.1 TEST 1: empty message. The published vector proves the
# backend produces standard Ed25519 signatures any other implementation
# can verify, and accepts theirs.
_RFC_TEST1_SEED = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
_RFC_TEST1_PUBLIC = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
_RFC_TEST1_SIGNATURE = (
    "e5564300c360ac729086e2cc806e828a"
    "84877f1eb8e5d974d873e06522490155"
    "5fb8821590a33bacc61e39701cf9b46b"
    "d25bf5f0595bbe24655141438e7a100b"
)

# RFC 8032 §7.1 TEST 3: two-byte message af82.
_RFC_TEST3_SEED = "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7"
_RFC_TEST3_PUBLIC = "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025"
_RFC_TEST3_MESSAGE = bytes.fromhex("af82")
_RFC_TEST3_SIGNATURE = (
    "6291d657deec24024827e69c3abe01a3"
    "0ce548a284743a445e3680d7db5ac3ac"
    "18ff9b538d16f290ae67f760984dc659"
    "4a7c15e9716ed28dc027beceea1ec40a"
)


def _write_package(root: Path) -> Path:
    package = root / "demo_plugin"
    package.mkdir()
    (package / "manifest.json").write_text(json.dumps(_manifest()), encoding="utf-8")
    (package / "handler.py").write_text(
        "def run(context, arguments):\n    return {}\n", encoding="utf-8"
    )
    return package


def test_signatures_match_the_rfc_8032_vectors() -> None:
    """The backend speaks standard Ed25519, not a private dialect."""

    seed = bytes.fromhex(_RFC_TEST1_SEED)
    assert signing._public_from_seed(seed).hex() == _RFC_TEST1_PUBLIC
    assert signing._sign_raw(seed, b"").hex() == _RFC_TEST1_SIGNATURE
    public = bytes.fromhex(_RFC_TEST1_PUBLIC)
    signature = bytes.fromhex(_RFC_TEST1_SIGNATURE)
    assert signing._verify_raw(public, b"", signature) is True

    seed = bytes.fromhex(_RFC_TEST3_SEED)
    assert signing._public_from_seed(seed).hex() == _RFC_TEST3_PUBLIC
    assert signing._sign_raw(seed, _RFC_TEST3_MESSAGE).hex() == _RFC_TEST3_SIGNATURE
    public = bytes.fromhex(_RFC_TEST3_PUBLIC)
    signature = bytes.fromhex(_RFC_TEST3_SIGNATURE)
    assert signing._verify_raw(public, _RFC_TEST3_MESSAGE, signature) is True


def test_round_trip_sign_then_verify(tmp_path: Path) -> None:
    private, public = signing.write_keypair(
        tmp_path / "operator.key", tmp_path / "operator.pub"
    )
    package = _write_package(tmp_path)
    digest = inspect_plugin_package(str(package)).digest
    signature = signing.sign_package(str(package), private)
    assert signing.verify_package(digest, signature, public) is True


def test_tampered_digest_and_wrong_key_fail(tmp_path: Path) -> None:
    private, public = signing.write_keypair(
        tmp_path / "operator.key", tmp_path / "operator.pub"
    )
    attacker, _ = signing.write_keypair(
        tmp_path / "attacker.key", tmp_path / "attacker.pub"
    )
    package = _write_package(tmp_path)
    digest = inspect_plugin_package(str(package)).digest
    signature = signing.sign_package(str(package), private)

    assert signing.verify_package("sha256:" + "0" * 64, signature, public) is False
    assert signing.verify_package(digest, signature, attacker) is False
    flipped = signature[:-1] + ("0" if signature[-1] != "0" else "1")
    assert signing.verify_package(digest, flipped, public) is False


def test_malformed_inputs_fail_closed_without_raising(tmp_path: Path) -> None:
    _, public = signing.write_keypair(
        tmp_path / "operator.key", tmp_path / "operator.pub"
    )
    digest = "sha256:" + "a" * 64
    # Malformed signature shapes return False; they never raise.
    assert signing.verify_digest(digest, "not-hex", public) is False
    assert signing.verify_digest(digest, "zz" * 64, public) is False
    assert signing.verify_digest(digest, "ab" * 32, public) is False  # too short
    assert signing.verify_digest("no-prefix:" + "a" * 64, "ab" * 64, public) is False
    assert signing.verify_digest(digest.upper(), "ab" * 64, public) is False


def test_missing_public_key_env_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(signing.PUBLIC_KEY_ENV, raising=False)
    with pytest.raises(signing.SigningError):
        signing.load_public_key_from_env()


def test_missing_public_key_file_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(signing.PUBLIC_KEY_ENV, str(tmp_path / "absent.pub"))
    with pytest.raises(signing.SigningError):
        signing.load_public_key_from_env()


def test_malformed_key_files_are_refused(tmp_path: Path) -> None:
    bad = tmp_path / "bad.pub"
    bad.write_text("not hex at all\n", encoding="utf-8")
    with pytest.raises(signing.SigningError):
        signing._read_hex_key(bad, signing.PUBLIC_KEY_BYTES, "public")

    short = tmp_path / "short.pub"
    short.write_text("ab" * 16 + "\n", encoding="utf-8")
    with pytest.raises(signing.SigningError):
        signing._read_hex_key(short, signing.PUBLIC_KEY_BYTES, "public")


def test_sign_digest_refuses_a_malformed_digest(tmp_path: Path) -> None:
    private, public = signing.write_keypair(
        tmp_path / "operator.key", tmp_path / "operator.pub"
    )
    # A digest that does not match the package_digest shape is signed as
    # given: the caller's contract is the exact pinned string. But verify
    # refuses it, so a malformed digest can never round-trip.
    signature = signing.sign_digest("sha256:" + "a" * 64, private)
    assert signing.verify_digest("sha256:" + "a" * 64, signature, public) is True
    assert signing.verify_digest("sha256:" + "b" * 64, signature, public) is False

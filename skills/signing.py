"""Ed25519 attestation for plugin package digests (KEL-29 spike, real backend).

**Spike module. Imported by nothing in the production path.** The loader
(``skills/loader.py``) and the CLI (``skills/cli.py``) are untouched; this
module exists to prove the round trip before the team decides whether
attested installs are worth enforcing at boot.

Why this exists: the digest pin in ``plugins.lock.json`` is self-certified.
Anyone who can write a package can also compute its digest, so the pin proves
*unchanged-since-install*, never *written-by-a-trusted-publisher*. This module
closes that gap by signing the digest string returned by
``skills.loader.package_digest`` with an operator Ed25519 key:

- ``sign_package(path, private_key_path)`` inspects a real package, computes
  its digest, and returns a detached signature (hex) over that digest string.
- ``verify_package(digest, signature, public_key_path)`` returns whether the
  signature is valid for that digest under the trusted public key.
- ``load_public_key_from_env()`` resolves the trusted key from an env path
  (``KELP_PLUGIN_SIGNING_PUBLIC_KEY``) and refuses a missing variable or file.

Crypto backend — production grade as of the KEL-29 follow-up:

- The backend is the ``cryptography`` package's Ed25519
  (``Ed25519PrivateKey`` / ``Ed25519PublicKey``), a dependency since the
  credential-vault work made it a core requirement. The implementation is
  Rust-backed, constant-time, and audited — the pure-Python RFC 8032 sketch
  the spike shipped first is gone. Signatures are the standard 64-byte
  detached Ed25519 form, so attestations signed before the swap still verify
  (both implementations follow RFC 8032; pinned by a test vector).
- The backend is still isolated behind two internal functions,
  ``_sign_raw(seed, message)`` and ``_verify_raw(public, message,
  signature)``, so a future backend change (PyNaCl, a hardware key) touches
  only this section.
- Key files are plain 64-hex-character text (one key per file) so the
  operator workflow needs no crypto tooling. Moving to PEM/PKCS#8 via
  ``cryptography`` and a real secret store remains the production hardening
  step, and is a format change, not a signature change.

What this module deliberately does NOT do: it does not touch
``plugins.lock.json``, does not add a ``signature`` field to any pin, and does
not change boot behavior. See ``docs/SPIKE_PLUGIN_SIGNING.md`` for the design
that WOULD do those things.
"""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from typing import Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding as _Encoding,
    PublicFormat as _PublicFormat,
)

from .loader import inspect_plugin_package

__all__ = [
    "PUBLIC_KEY_ENV",
    "PRIVATE_KEY_BYTES",
    "SIGNATURE_BYTES",
    "SigningError",
    "generate_keypair",
    "load_public_key_from_env",
    "sign_digest",
    "sign_package",
    "verify_digest",
    "verify_package",
    "write_keypair",
]

#: Env var holding the path of the trusted operator public key (hex text).
PUBLIC_KEY_ENV: Final = "KELP_PLUGIN_SIGNING_PUBLIC_KEY"

#: Ed25519 seed / public key / signature sizes, per RFC 8032.
PRIVATE_KEY_BYTES: Final = 32
PUBLIC_KEY_BYTES: Final = 32
SIGNATURE_BYTES: Final = 64

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_HEX_RE = re.compile(r"[0-9a-f]+")


class SigningError(Exception):
    """A signing key or configured key path is missing or malformed."""


# ---------------------------------------------------------------------------
# Ed25519 backend (cryptography) — constant-time, Rust-backed, audited.
# Everything below routes through _sign_raw / _verify_raw and nothing else, so
# a future backend change (PyNaCl, a hardware key) touches only this section.
# ---------------------------------------------------------------------------


def _public_from_seed(seed: bytes) -> bytes:
    """The 32-byte raw Ed25519 public key derived from a 32-byte seed."""

    if len(seed) != PRIVATE_KEY_BYTES:
        raise SigningError(f"private key must be {PRIVATE_KEY_BYTES} bytes")
    private = Ed25519PrivateKey.from_private_bytes(seed)
    return private.public_key().public_bytes(
        _Encoding.Raw, _PublicFormat.Raw
    )


def _sign_raw(seed: bytes, message: bytes) -> bytes:
    """Detached 64-byte Ed25519 signature over ``message`` (RFC 8032 §5.1.6)."""

    if len(seed) != PRIVATE_KEY_BYTES:
        raise SigningError(f"private key must be {PRIVATE_KEY_BYTES} bytes")
    return Ed25519PrivateKey.from_private_bytes(seed).sign(message)


def _verify_raw(public: bytes, message: bytes, signature: bytes) -> bool:
    """RFC 8032 §5.1.7 check; False on any malformed input, never an error."""

    if len(public) != PUBLIC_KEY_BYTES or len(signature) != SIGNATURE_BYTES:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signature, message)
    except (InvalidSignature, ValueError):
        return False
    return True

# ---------------------------------------------------------------------------
# Key files and env resolution
# ---------------------------------------------------------------------------


def _read_hex_key(path: Path, expected_bytes: int, kind: str) -> bytes:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SigningError(f"cannot read {kind} key at {path}: {exc}") from exc
    text = raw.strip().lower()
    if not _HEX_RE.fullmatch(text):
        raise SigningError(f"{kind} key at {path} must be hex text")
    key = bytes.fromhex(text)
    if len(key) != expected_bytes:
        raise SigningError(
            f"{kind} key at {path} must be {expected_bytes} bytes "
            f"({expected_bytes * 2} hex chars), got {len(key)}"
        )
    return key


def _write_hex_key(path: Path, key: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key.hex() + "\n", encoding="utf-8")
    return path


def generate_keypair() -> tuple[bytes, bytes]:
    """A fresh Ed25519 ``(seed, public)`` pair as raw 32-byte values."""

    seed = secrets.token_bytes(PRIVATE_KEY_BYTES)
    return seed, _public_from_seed(seed)


def write_keypair(private_key_path: str | Path, public_key_path: str | Path) -> tuple[Path, Path]:
    """Generate a keypair and write both halves as hex text files."""

    seed, public = generate_keypair()
    return (
        _write_hex_key(Path(private_key_path), seed),
        _write_hex_key(Path(public_key_path), public),
    )


def load_public_key_from_env(
    env_var: str = PUBLIC_KEY_ENV, *, environ: "dict[str, str] | None" = None
) -> bytes:
    """Read the trusted public key from the env-named path; refuse a missing one.

    Raises :class:`SigningError` when the variable is unset, the path does not
    exist, or the file is not a 32-byte hex key. Fail-closed by design: a
    deployment without a provisioned key must not silently verify nothing.
    """

    env = os.environ if environ is None else environ
    raw = str(env.get(env_var, "")).strip()
    if not raw:
        raise SigningError(
            f"{env_var} is not set; point it at the operator public key file "
            "(64 hex chars) before verifying plugin attestations"
        )
    path = Path(raw).expanduser()
    if not path.is_file():
        raise SigningError(f"public key file {path} from {env_var} does not exist")
    return _read_hex_key(path, PUBLIC_KEY_BYTES, "public")


# ---------------------------------------------------------------------------
# Public round-trip API
# ---------------------------------------------------------------------------


def sign_digest(digest: str, private_key_path: str | Path) -> str:
    """Sign the ``package_digest`` string; returns the detached signature in hex.

    ``digest`` is the exact string returned by
    ``skills.loader.package_digest`` (``sha256:<64 hex>``), signed as UTF-8
    bytes — not the package bytes themselves.
    """

    seed = _read_hex_key(Path(private_key_path), PRIVATE_KEY_BYTES, "private")
    return _sign_raw(seed, digest.encode("utf-8")).hex()


def verify_digest(digest: str, signature: str, public_key_path: str | Path) -> bool:
    """Check a detached signature over ``digest`` under the trusted public key.

    Returns ``False`` for a malformed digest or signature. A missing or
    malformed key file raises :class:`SigningError` (fail closed).
    """

    if not _DIGEST_RE.fullmatch(digest):
        return False
    text = signature.strip().lower()
    if not _HEX_RE.fullmatch(text):
        return False
    raw_signature = bytes.fromhex(text)
    if len(raw_signature) != SIGNATURE_BYTES:
        return False
    public = _read_hex_key(Path(public_key_path), PUBLIC_KEY_BYTES, "public")
    return _verify_raw(public, digest.encode("utf-8"), raw_signature)


def sign_package(path: str | Path, private_key_path: str | Path) -> str:
    """Inspect a real package and sign its actual digest.

    Uses ``skills.loader.inspect_plugin_package`` so the signed string is the
    same digest the CLI pins and the loader checks — never an invented one.
    """

    return sign_digest(inspect_plugin_package(path).digest, private_key_path)


def verify_package(digest: str, signature: str, public_key_path: str | Path) -> bool:
    """Verify a detached signature over a package digest (see :func:`verify_digest`)."""

    return verify_digest(digest, signature, public_key_path)

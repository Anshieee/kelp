"""Prototype Ed25519 attestation for plugin package digests (KEL-29 spike).

**Prototype-grade. Imported by nothing in the production path.** The loader
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

Crypto backend — read this before shipping any of it to production:

- The venv has only ``jsonschema`` and ``httpx``; neither ``cryptography`` nor
  PyNaCl is installed, and this spike adds no dependency. The Ed25519 below is
  a small pure-Python RFC 8032 implementation. It is **not constant-time**,
  has no side-channel hardening, and has not been audited. It is correct
  enough for a round-trip spike and wrong for a hostile environment.
- The entire backend is isolated behind two internal functions,
  ``_sign_raw(seed, message)`` and ``_verify_raw(public, message,
  signature)``, so the production swap is a two-function change. The intended
  production dependency is ``cryptography`` (``Ed25519PrivateKey``/
  ``Ed25519PublicKey``) or PyNaCl (``nacl.signing.SigningKey``); both speak
  the same 64-byte detached-signature format used here.
- Key files are plain 64-hex-character text (one key per file). Production
  would move to PEM/PKCS#8 via ``cryptography`` and a real secret store.

What this module deliberately does NOT do: it does not touch
``plugins.lock.json``, does not add a ``signature`` field to any pin, and does
not change boot behavior. See ``docs/SPIKE_PLUGIN_SIGNING.md`` for the design
that WOULD do those things.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from pathlib import Path
from typing import Final

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
# Ed25519 backend (pure Python, RFC 8032) — prototype-grade, not constant-time.
# Everything below routes through _sign_raw / _verify_raw and nothing else, so
# the production swap to `cryptography` or PyNaCl touches only this section.
# ---------------------------------------------------------------------------

_P: Final = 2**255 - 19
_L: Final = 2**252 + 27742317777372353535851937790883648493
_D: Final = (-121665 * pow(121666, _P - 2, _P)) % _P
_SQRT_M1: Final = pow(2, (_P - 1) // 4, _P)

_Point = tuple[int, int, int, int]  # extended coordinates (X, Y, Z, T)
_IDENTITY: Final[_Point] = (0, 1, 1, 0)


def _inverse(value: int) -> int:
    return pow(value, _P - 2, _P)


def _xrecover(y: int) -> int:
    """One x with ``x^2 = (y^2 - 1) / (d y^2 + 1)`` (mod p), if it exists."""

    xx = ((y * y - 1) * _inverse(_D * y * y + 1)) % _P
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = (x * _SQRT_M1) % _P
    if (x * x - xx) % _P != 0:
        raise ValueError("point is not on the curve")
    if x & 1:
        x = _P - x
    return x


_BY: Final = (4 * _inverse(5)) % _P
_BX: Final = _xrecover(_BY)
_BASE: Final[_Point] = (_BX % _P, _BY % _P, 1, (_BX * _BY) % _P)


def _edwards_add(first: _Point, second: _Point) -> _Point:
    x1, y1, z1, t1 = first
    x2, y2, z2, t2 = second
    a = ((y1 - x1) * (y2 - x2)) % _P
    b = ((y1 + x1) * (y2 + x2)) % _P
    c = (2 * t1 * t2 * _D) % _P
    d = (2 * z1 * z2) % _P
    e = b - a
    f = d - c
    g = d + c
    h = b + a
    return ((e * f) % _P, (g * h) % _P, (f * g) % _P, (e * h) % _P)


def _scalarmult(point: _Point, scalar: int) -> _Point:
    result = _IDENTITY
    addend = point
    while scalar:
        if scalar & 1:
            result = _edwards_add(result, addend)
        addend = _edwards_add(addend, addend)
        scalar >>= 1
    return result


def _compress(point: _Point) -> bytes:
    x, y, z, _ = point
    z_inv = _inverse(z)
    x = (x * z_inv) % _P
    y = (y * z_inv) % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(encoded: bytes) -> _Point:
    value = int.from_bytes(encoded, "little")
    sign = (value >> 255) & 1
    y = value & ((1 << 255) - 1)
    if y >= _P:
        raise ValueError("non-canonical y coordinate")
    x = _xrecover(y)
    if (x & 1) != sign:
        x = _P - x
    if (y * y - x * x - 1 - _D * x * x * y * y) % _P != 0:
        raise ValueError("point is not on the curve")
    return (x, y, 1, (x * y) % _P)


def _clamp_scalar(seed: bytes) -> int:
    digest = hashlib.sha512(seed).digest()
    scalar = int.from_bytes(digest[:32], "little")
    scalar &= (1 << 254) - 8
    scalar |= 1 << 254
    return scalar


def _public_from_seed(seed: bytes) -> bytes:
    return _compress(_scalarmult(_BASE, _clamp_scalar(seed)))


def _sign_raw(seed: bytes, message: bytes) -> bytes:
    """RFC 8032 §5.1.6: detached 64-byte Ed25519 signature over ``message``."""

    if len(seed) != PRIVATE_KEY_BYTES:
        raise SigningError(f"private key must be {PRIVATE_KEY_BYTES} bytes")
    digest = hashlib.sha512(seed).digest()
    scalar = _clamp_scalar(seed)
    prefix = digest[32:]
    public = _compress(_scalarmult(_BASE, scalar))
    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % _L
    r_encoded = _compress(_scalarmult(_BASE, r))
    k = int.from_bytes(hashlib.sha512(r_encoded + public + message).digest(), "little") % _L
    s = (r + k * scalar) % _L
    return r_encoded + int.to_bytes(s, 32, "little")


def _verify_raw(public: bytes, message: bytes, signature: bytes) -> bool:
    """RFC 8032 §5.1.7 check ``s·B == R + k·A``; False on any malformed input."""

    if len(public) != PUBLIC_KEY_BYTES or len(signature) != SIGNATURE_BYTES:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    try:
        r_point = _decompress(signature[:32])
        a_point = _decompress(public)
    except ValueError:
        return False
    k = int.from_bytes(hashlib.sha512(signature[:32] + public + message).digest(), "little") % _L
    left = _scalarmult(_BASE, s)
    right = _edwards_add(r_point, _scalarmult(a_point, k))
    return _compress(left) == _compress(right)


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

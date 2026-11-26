"""Dedicated v1 channel between a local supervisor and the KELP sidecar.

The supervisor owns a connected ``AF_UNIX``/``SOCK_STREAM`` pair. The child
receives exactly one config frame on its stdin socket, replies with exactly one
ready or error frame, and then treats any further byte or EOF as a request to
stop. This module contains no startup policy beyond the wire format: the
Linux-only entrypoint lives in :mod:`chat.local_serve`.

Framing is a four-byte big-endian unsigned length followed by that many UTF-8
JSON bytes. The config frame is bounded to
:data:`CONFIG_MAX_BYTES`; status frames are bounded to
:data:`STATUS_MAX_BYTES`. Both reads and writes use an absolute deadline so a
stalled peer cannot hold the process open.
"""

from __future__ import annotations

import json
import re
import socket
import struct
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

__all__ = [
    "CONFIG_DEADLINE_SECONDS",
    "CONFIG_MAX_BYTES",
    "ERROR_CODES",
    "LOCAL_STATE_ENV",
    "LocalConfig",
    "ProtocolError",
    "SECRET_MAX_BYTES",
    "STATUS_DEADLINE_SECONDS",
    "STATUS_MAX_BYTES",
    "compose_environ",
    "encode_frame",
    "error_payload",
    "parse_config",
    "read_frame",
    "ready_payload",
    "send_error",
    "send_ready",
    "write_frame",
]

PROTOCOL_VERSION = 1
CONFIG_MAX_BYTES = 65536
STATUS_MAX_BYTES = 4096
SECRET_MAX_BYTES = 1024
CONFIG_DEADLINE_SECONDS = 30.0
STATUS_DEADLINE_SECONDS = 2.0

LOCAL_STATE_ENV = "KELP_LOCAL_STATE_DIR"

REQUIRED_SECRETS = ("KELP_AUTH_TOKEN", "KELP_PROVIDER_API_KEY")
OPTIONAL_SECRETS = ("KELP_CSRF_TOKEN", "KELP_SESSION_SECRET")
ALLOWED_SECRETS = frozenset((*REQUIRED_SECRETS, *OPTIONAL_SECRETS))
TOP_LEVEL_KEYS = frozenset({"v", "type", "nonce", "secrets"})
NONCE_RE = re.compile(r"^[0-9a-f]{32}$")

#: Inherited process environment names that must never supply a credential.
INHERITED_SECRET_NAMES = frozenset((*ALLOWED_SECRETS,))
#: Configuration that would bypass the channel or start a credential worker.
INHERITED_UNSUPPORTED_NAMES = frozenset(
    {
        "KELP_TENANT_SECRETS_FILE",
        "KELP_CREDENTIALS_ENABLED",
        "KELP_VAULT_KEY",
        "KELP_BROWSER_WORKER_TOKEN",
        "KELP_BROWSER_ALLOWED_ORIGINS",
        "KELP_CREDENTIAL_SIGNING_KEY",
        # Latent ``SkillRegistry.reload`` paths fall back to the real
        # ``os.environ`` for this pin bypass; the local entrypoint can never
        # support it, so refuse it up front instead of silently dropping it.
        "KELP_PLUGINS_ALLOW_UNPINNED",
    }
)
#: The complete inherited nonsecret allowlist for this entrypoint.
NONSECRET_ALLOWLIST = ("KELP_PROVIDER_BASE_URL", "KELP_PRIMARY_MODEL")

#: Fixed machine-readable codes; no free-form message ever crosses the channel.
ERROR_CODES = frozenset(
    {
        "bad_frame",
        "bad_length",
        "duplicate_key",
        "inherited_secret",
        "internal_error",
        "invalid_config",
        "invalid_json",
        "invalid_nonce",
        "invalid_secret",
        "invalid_state_dir",
        "invalid_utf8",
        "missing_secret",
        "oversize_secret",
        "startup_failed",
        "timeout",
        "unknown_key",
        "unknown_secret",
        "unsupported_inherited_config",
    }
)


class ProtocolError(Exception):
    """A bounded, non-secret failure that maps to one wire code."""

    def __init__(self, code: str, nonce: str | None = None):
        self.code = code if code in ERROR_CODES else "internal_error"
        self.nonce = nonce
        super().__init__(self.code)


@dataclass(frozen=True)
class LocalConfig:
    """Validated config frame. Secret values are excluded from ``repr``."""

    nonce: str
    secrets: Mapping[str, str] = field(repr=False)

    def __repr__(self) -> str:
        return (
            "LocalConfig("
            f"nonce={self.nonce!r}, "
            f"secret_names={tuple(sorted(self.secrets))!r})"
        )

    __str__ = __repr__


# -- framing ---------------------------------------------------------------


def _length(payload: bytes, max_bytes: int) -> int:
    size = len(payload)
    if not 1 <= size <= max_bytes:
        raise ProtocolError("bad_length")
    return size


def encode_frame(payload: bytes, *, max_bytes: int) -> bytes:
    """Prefix one non-empty payload with its unsigned 32-bit length."""

    data = bytes(payload)
    size = _length(data, max_bytes)
    return struct.pack(">I", size) + data


def _remaining(deadline_at: float) -> float:
    remaining = deadline_at - time.monotonic()
    if remaining <= 0:
        raise ProtocolError("timeout")
    return remaining


def _recv_exact(connection: socket.socket, size: int, deadline_at: float) -> bytes:
    buffer = bytearray()
    while len(buffer) < size:
        remaining = _remaining(deadline_at)
        connection.settimeout(remaining)
        try:
            chunk = connection.recv(size - len(buffer))
        except TimeoutError:
            raise ProtocolError("timeout") from None
        except OSError:
            raise ProtocolError("bad_frame") from None
        if not chunk:
            raise ProtocolError("bad_frame")
        buffer.extend(chunk)
    return bytes(buffer)


def read_frame(
    connection: socket.socket, *, max_bytes: int, deadline: float
) -> bytes:
    """Read one bounded frame, treating EOF and truncation as ``bad_frame``."""

    deadline_at = time.monotonic() + max(0.0, float(deadline))
    header = _recv_exact(connection, 4, deadline_at)
    size = struct.unpack(">I", header)[0]
    if not 1 <= size <= max_bytes:
        raise ProtocolError("bad_length")
    return _recv_exact(connection, size, deadline_at)


def write_frame(
    connection: socket.socket,
    payload: bytes,
    *,
    max_bytes: int,
    deadline: float,
) -> None:
    """Write one bounded frame within a short absolute deadline."""

    packet = encode_frame(payload, max_bytes=max_bytes)
    connection.settimeout(max(0.001, float(deadline)))
    try:
        connection.sendall(packet)
    except TimeoutError:
        raise ProtocolError("timeout") from None
    except OSError:
        raise ProtocolError("bad_frame") from None


# -- status replies --------------------------------------------------------


def ready_payload(nonce: str, port: int) -> dict[str, Any]:
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("port must be an integer between 1 and 65535")
    return {
        "v": PROTOCOL_VERSION,
        "type": "ready",
        "nonce": nonce,
        "host": "127.0.0.1",
        "port": port,
    }


def error_payload(nonce: str | None, code: str) -> dict[str, Any]:
    return {
        "v": PROTOCOL_VERSION,
        "type": "error",
        "nonce": nonce,
        "code": code if code in ERROR_CODES else "internal_error",
    }


def _status_bytes(payload: Mapping[str, Any]) -> bytes:
    data = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":")
    ).encode("utf-8")
    if len(data) > STATUS_MAX_BYTES:
        raise ProtocolError("bad_length")
    return data


def send_ready(
    connection: socket.socket,
    nonce: str,
    port: int,
    *,
    deadline: float = STATUS_DEADLINE_SECONDS,
) -> None:
    write_frame(
        connection,
        _status_bytes(ready_payload(nonce, port)),
        max_bytes=STATUS_MAX_BYTES,
        deadline=deadline,
    )


def send_error(
    connection: socket.socket,
    nonce: str | None,
    code: str,
    *,
    deadline: float = STATUS_DEADLINE_SECONDS,
) -> None:
    write_frame(
        connection,
        _status_bytes(error_payload(nonce, code)),
        max_bytes=STATUS_MAX_BYTES,
        deadline=deadline,
    )


# -- config parsing --------------------------------------------------------


class _DuplicateKey(ValueError):
    pass


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _has_control(value: str) -> bool:
    # Unicode "Other" categories: Cc (C0/C1 controls), Cf (format/bidi
    # overrides), Cs (surrogates), Co (private use), and Cn (unassigned).
    # Secret values are credentials, not display text.
    return any(unicodedata.category(character).startswith("C") for character in value)


def parse_config(payload: bytes) -> LocalConfig:
    """Validate the frozen config shape and return non-secret-safe values."""

    if not isinstance(payload, (bytes, bytearray)):
        raise ProtocolError("invalid_config")
    try:
        text = bytes(payload).decode("utf-8")
    except UnicodeDecodeError:
        raise ProtocolError("invalid_utf8") from None
    try:
        data: Any = json.loads(text, object_pairs_hook=_no_duplicates)
    except _DuplicateKey:
        raise ProtocolError("duplicate_key") from None
    except Exception:  # noqa: BLE001 - malformed input must not crash startup
        raise ProtocolError("invalid_json") from None

    if not isinstance(data, dict):
        raise ProtocolError("invalid_config")

    raw_nonce = data.get("nonce")
    nonce = (
        raw_nonce
        if isinstance(raw_nonce, str) and NONCE_RE.fullmatch(raw_nonce)
        else None
    )

    def fail(code: str) -> None:
        raise ProtocolError(code, nonce)

    if set(data) - TOP_LEVEL_KEYS:
        fail("unknown_key")
    if TOP_LEVEL_KEYS - set(data):
        fail("invalid_config")

    version = data["v"]
    if type(version) is not int or version != PROTOCOL_VERSION:
        fail("invalid_config")
    if data["type"] != "config":
        fail("invalid_config")
    if nonce is None:
        raise ProtocolError("invalid_nonce", None)

    raw_secrets = data["secrets"]
    if not isinstance(raw_secrets, dict):
        fail("invalid_config")
    if set(raw_secrets) - ALLOWED_SECRETS:
        fail("unknown_secret")

    secrets: dict[str, str] = {}
    for name, value in raw_secrets.items():
        if not isinstance(name, str) or not isinstance(value, str):
            fail("invalid_secret")
        if not value:
            fail("invalid_secret")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            fail("invalid_secret")
        if len(encoded) > SECRET_MAX_BYTES:
            fail("oversize_secret")
        if _has_control(value):
            fail("invalid_secret")
        secrets[name] = value

    for required in REQUIRED_SECRETS:
        if required not in secrets:
            fail("missing_secret")

    return LocalConfig(nonce=nonce, secrets=MappingProxyType(secrets))


# -- host mapping ----------------------------------------------------------


def _state_root(inherited: Mapping[str, str]) -> Path:
    raw = inherited.get(LOCAL_STATE_ENV)
    if not isinstance(raw, str) or not raw.strip():
        raise ProtocolError("invalid_state_dir")
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        raise ProtocolError("invalid_state_dir")
    try:
        resolved = path.resolve()
    except OSError:
        raise ProtocolError("invalid_state_dir") from None
    if resolved == Path(resolved.anchor):
        raise ProtocolError("invalid_state_dir")
    try:
        if resolved.exists() and not resolved.is_dir():
            raise ProtocolError("invalid_state_dir")
    except OSError:
        raise ProtocolError("invalid_state_dir") from None
    return resolved


def compose_environ(
    inherited: Mapping[str, str], config: LocalConfig
) -> dict[str, str]:
    """Build the mapping passed to ``ServerConfig.from_env`` and ``serve``.

    The caller's process environment is read, never mutated. Only two
    non-secret provider knobs survive; every state path is derived from the
    required local state root, and loopback, scheduler, and credential
    broker settings are forced.
    """

    for name in INHERITED_SECRET_NAMES:
        if name in inherited:
            raise ProtocolError("inherited_secret")
    for name in INHERITED_UNSUPPORTED_NAMES:
        if name in inherited:
            raise ProtocolError("unsupported_inherited_config")

    state = _state_root(inherited)
    env: dict[str, str] = {
        LOCAL_STATE_ENV: str(state),
        "KELP_DB_PATH": str(state / "db" / "chat.db"),
        "KELP_ARTIFACT_DIR": str(state / "artifacts"),
        "KELP_AUDIT_DIR": str(state / "audit"),
        "KELP_USAGE_DIR": str(state / "usage"),
        "KELP_WORKSPACE_ROOT": str(state / "workspace"),
        "KELP_PLUGIN_ROOT": str(state / "plugins"),
        "KELP_PLUGIN_STORE": str(state / "skill-store"),
        "KELP_HOST": "127.0.0.1",
        "KELP_PORT": "0",
        "KELP_SCHEDULER_ENABLED": "0",
        "KELP_CREDENTIALS_ENABLED": "0",
    }
    for name in NONSECRET_ALLOWLIST:
        value = inherited.get(name)
        if isinstance(value, str) and value.strip():
            env[name] = value.strip()
    env.update(config.secrets)
    return env

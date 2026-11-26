"""Dedicated local-supervisor channel: framing, config validation, composition.

The wire contract is deliberately small and fixed. These tests pin the
machine-readable failure codes, the byte bounds, and the rule that inherited
process configuration never supplies credentials.
"""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest

from chat.local_channel import (
    CONFIG_MAX_BYTES,
    SECRET_MAX_BYTES,
    STATUS_MAX_BYTES,
    LocalConfig,
    ProtocolError,
    compose_environ,
    encode_frame,
    error_payload,
    parse_config,
    read_frame,
    ready_payload,
    send_error,
    send_ready,
)
from chat.server import ServerConfig

NONCE = "0123456789abcdef0123456789abcdef"
AUTH = "fake-auth-token-0123456789"
API_KEY = "fake-provider-key-0123456789"


def _config(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "v": 1,
        "type": "config",
        "nonce": NONCE,
        "secrets": {"KELP_AUTH_TOKEN": AUTH, "KELP_PROVIDER_API_KEY": API_KEY},
    }
    payload.update(overrides)
    return payload


def _bytes(payload: object) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _error(payload: bytes | dict[str, object]) -> ProtocolError:
    with pytest.raises(ProtocolError) as caught:
        parse_config(payload if isinstance(payload, bytes) else _bytes(payload))
    return caught.value


def _inherited(**overrides: str) -> dict[str, str]:
    env = {
        "KELP_LOCAL_STATE_DIR": "/tmp/kelp-local-state",
        "KELP_PROVIDER_BASE_URL": "http://127.0.0.1:9/v1",
        "KELP_PRIMARY_MODEL": "test-model",
    }
    env.update(overrides)
    return env


# -- framing ---------------------------------------------------------------


def test_ready_payload_is_the_frozen_shape() -> None:
    payload = ready_payload(NONCE, 43210)
    assert payload == {
        "v": 1,
        "type": "ready",
        "nonce": NONCE,
        "host": "127.0.0.1",
        "port": 43210,
    }
    encoded = encode_frame(json.dumps(payload).encode(), max_bytes=STATUS_MAX_BYTES)
    left, right = socket.socketpair()
    try:
        left.sendall(encoded)
        assert read_frame(right, max_bytes=STATUS_MAX_BYTES, deadline=2.0) == json.dumps(
            payload
        ).encode()
    finally:
        left.close()
        right.close()


def test_error_payload_echoes_nonce_or_uses_null() -> None:
    assert error_payload(NONCE, "invalid_config") == {
        "v": 1,
        "type": "error",
        "nonce": NONCE,
        "code": "invalid_config",
    }
    assert error_payload(None, "invalid_json") == {
        "v": 1,
        "type": "error",
        "nonce": None,
        "code": "invalid_json",
    }


def test_send_helpers_write_exactly_one_bounded_frame() -> None:
    left, right = socket.socketpair()
    try:
        send_ready(left, NONCE, 1234)
        send_error(left, NONCE, "invalid_config")
        ready = json.loads(read_frame(right, max_bytes=STATUS_MAX_BYTES, deadline=2.0))
        error = json.loads(read_frame(right, max_bytes=STATUS_MAX_BYTES, deadline=2.0))
        assert ready["type"] == "ready"
        assert error["type"] == "error"
    finally:
        left.close()
        right.close()


@pytest.mark.parametrize("length", [0, CONFIG_MAX_BYTES + 1])
def test_read_frame_rejects_lengths_outside_the_contract(length: int) -> None:
    left, right = socket.socketpair()
    try:
        left.sendall(length.to_bytes(4, "big"))
        with pytest.raises(ProtocolError) as caught:
            read_frame(right, max_bytes=CONFIG_MAX_BYTES, deadline=1.0)
        assert caught.value.code == "bad_length"
    finally:
        left.close()
        right.close()


def test_encode_frame_rejects_empty_and_oversize_payloads() -> None:
    with pytest.raises(ProtocolError) as empty:
        encode_frame(b"", max_bytes=CONFIG_MAX_BYTES)
    assert empty.value.code == "bad_length"
    with pytest.raises(ProtocolError) as huge:
        encode_frame(b"x" * (CONFIG_MAX_BYTES + 1), max_bytes=CONFIG_MAX_BYTES)
    assert huge.value.code == "bad_length"


def test_read_frame_enforces_one_absolute_deadline() -> None:
    left, right = socket.socketpair()
    try:
        started = time.monotonic()
        with pytest.raises(ProtocolError) as caught:
            read_frame(right, max_bytes=CONFIG_MAX_BYTES, deadline=0.2)
        elapsed = time.monotonic() - started
        assert caught.value.code == "timeout"
        assert elapsed < 2.0
    finally:
        left.close()
        right.close()


def test_read_frame_treats_eof_as_a_bad_frame() -> None:
    left, right = socket.socketpair()
    try:
        left.close()
        with pytest.raises(ProtocolError) as caught:
            read_frame(right, max_bytes=CONFIG_MAX_BYTES, deadline=1.0)
        assert caught.value.code == "bad_frame"
    finally:
        right.close()


# -- config parsing --------------------------------------------------------


def test_parse_config_accepts_required_and_optional_secrets() -> None:
    parsed = parse_config(
        _bytes(
            _config(
                secrets={
                    "KELP_AUTH_TOKEN": AUTH,
                    "KELP_PROVIDER_API_KEY": API_KEY,
                    "KELP_CSRF_TOKEN": "csrf-token",
                    "KELP_SESSION_SECRET": "session-secret",
                }
            )
        )
    )
    assert isinstance(parsed, LocalConfig)
    assert parsed.nonce == NONCE
    assert parsed.secrets["KELP_AUTH_TOKEN"] == AUTH
    assert "fake-provider-key-0123456789" not in repr(parsed)


def test_parse_config_rejects_duplicate_keys() -> None:
    payload = (
        '{"v":1,"v":1,"type":"config","nonce":"'
        + NONCE
        + '","secrets":{"KELP_AUTH_TOKEN":"a","KELP_PROVIDER_API_KEY":"b"}}'
    ).encode()
    error = _error(payload)
    assert error.code == "duplicate_key"
    assert error.nonce is None

    nested = (
        '{"v":1,"type":"config","nonce":"'
        + NONCE
        + '","secrets":{"KELP_AUTH_TOKEN":"a","KELP_AUTH_TOKEN":"b",'
        '"KELP_PROVIDER_API_KEY":"c"}}'
    ).encode()
    assert _error(nested).code == "duplicate_key"


def test_parse_config_rejects_unknown_top_level_and_secret_keys() -> None:
    error = _error(_config(extra=True))
    assert error.code == "unknown_key"
    assert error.nonce == NONCE

    error = _error(
        _config(
            secrets={
                "KELP_AUTH_TOKEN": AUTH,
                "KELP_PROVIDER_API_KEY": API_KEY,
                "KELP_VAULT_KEY": "nope",
            }
        )
    )
    assert error.code == "unknown_secret"
    assert error.nonce == NONCE


@pytest.mark.parametrize("version", [True, False, "1", 1.0, 2, None])
def test_parse_config_requires_integer_version_one(version: object) -> None:
    error = _error(_config(v=version))
    assert error.code == "invalid_config"
    assert error.nonce == NONCE


@pytest.mark.parametrize("kind", ["ready", "error", "", None, 1])
def test_parse_config_requires_the_config_type(kind: object) -> None:
    error = _error(_config(type=kind))
    assert error.code == "invalid_config"


@pytest.mark.parametrize(
    "nonce",
    ["A" * 32, "a" * 31, "a" * 33, "g" * 32, 1, None],
)
def test_parse_config_rejects_invalid_nonces(nonce: object) -> None:
    error = _error(_config(nonce=nonce))
    assert error.code == "invalid_nonce"
    assert error.nonce is None


@pytest.mark.parametrize(
    "payload",
    [b"\xff", b"{", b"[]", b"null", b'"x"'],
)
def test_parse_config_rejects_invalid_utf8_and_json(payload: bytes) -> None:
    error = _error(payload)
    assert error.code in {"invalid_utf8", "invalid_json", "invalid_config"}
    assert error.nonce is None


@pytest.mark.parametrize("missing", ["KELP_AUTH_TOKEN", "KELP_PROVIDER_API_KEY"])
def test_parse_config_requires_both_frame_secrets(missing: str) -> None:
    secrets = {"KELP_AUTH_TOKEN": AUTH, "KELP_PROVIDER_API_KEY": API_KEY}
    secrets.pop(missing)
    error = _error(_config(secrets=secrets))
    assert error.code == "missing_secret"
    assert error.nonce == NONCE


@pytest.mark.parametrize("value", ["", "x" * (SECRET_MAX_BYTES + 1)])
def test_parse_config_rejects_empty_and_oversize_secrets(value: str) -> None:
    error = _error(
        _config(
            secrets={
                "KELP_AUTH_TOKEN": value,
                "KELP_PROVIDER_API_KEY": API_KEY,
            }
        )
    )
    assert error.code in {"invalid_secret", "oversize_secret"}


@pytest.mark.parametrize(
    "value",
    ["a\nb", "a\x00b", "a\x7fb", "a\u0085b", "a\u202eb"],
)
def test_parse_config_rejects_utf8_control_characters_in_secrets(value: str) -> None:
    error = _error(
        _config(
            secrets={
                "KELP_AUTH_TOKEN": AUTH,
                "KELP_PROVIDER_API_KEY": value,
            }
        )
    )
    assert error.code == "invalid_secret"


def test_parse_config_accepts_secret_at_the_byte_bound() -> None:
    value = "x" * SECRET_MAX_BYTES
    parsed = parse_config(
        _bytes(
            _config(
                secrets={
                    "KELP_AUTH_TOKEN": AUTH,
                    "KELP_PROVIDER_API_KEY": value,
                }
            )
        )
    )
    assert parsed.secrets["KELP_PROVIDER_API_KEY"] == value


# -- composition -----------------------------------------------------------


def test_compose_environ_derives_state_and_forces_loopback() -> None:
    config = parse_config(_bytes(_config()))
    inherited = _inherited()
    snapshot = dict(inherited)
    env = compose_environ(inherited, config)
    state = Path("/tmp/kelp-local-state")
    assert inherited == snapshot
    assert env["KELP_HOST"] == "127.0.0.1"
    assert env["KELP_PORT"] == "0"
    assert env["KELP_SCHEDULER_ENABLED"] == "0"
    assert env["KELP_CREDENTIALS_ENABLED"] == "0"
    assert env["KELP_DB_PATH"] == str(state / "db" / "chat.db")
    assert env["KELP_ARTIFACT_DIR"] == str(state / "artifacts")
    assert env["KELP_AUDIT_DIR"] == str(state / "audit")
    assert env["KELP_USAGE_DIR"] == str(state / "usage")
    assert env["KELP_WORKSPACE_ROOT"] == str(state / "workspace")
    assert env["KELP_PLUGIN_ROOT"] == str(state / "plugins")
    assert env["KELP_PLUGIN_STORE"] == str(state / "skill-store")
    assert env["KELP_AUTH_TOKEN"] == AUTH
    assert env["KELP_PROVIDER_API_KEY"] == API_KEY
    assert env["KELP_PROVIDER_BASE_URL"] == "http://127.0.0.1:9/v1"
    assert env["KELP_PRIMARY_MODEL"] == "test-model"
    assert "KELP_TENANT_SECRETS_FILE" not in env


@pytest.mark.parametrize(
    "name",
    ["KELP_AUTH_TOKEN", "KELP_PROVIDER_API_KEY", "KELP_CSRF_TOKEN", "KELP_SESSION_SECRET"],
)
def test_compose_environ_refuses_inherited_credential_names(name: str) -> None:
    config = parse_config(_bytes(_config()))
    with pytest.raises(ProtocolError) as caught:
        compose_environ(_inherited(**{name: "inherited"}), config)
    assert caught.value.code == "inherited_secret"


@pytest.mark.parametrize(
    "name",
    [
        "KELP_TENANT_SECRETS_FILE",
        "KELP_VAULT_KEY",
        "KELP_BROWSER_WORKER_TOKEN",
        "KELP_BROWSER_ALLOWED_ORIGINS",
        "KELP_CREDENTIAL_SIGNING_KEY",
        "KELP_CREDENTIALS_ENABLED",
        "KELP_PLUGINS_ALLOW_UNPINNED",
    ],
)
def test_compose_environ_refuses_tenant_and_credential_config(name: str) -> None:
    config = parse_config(_bytes(_config()))
    with pytest.raises(ProtocolError) as caught:
        compose_environ(_inherited(**{name: "x"}), config)
    assert caught.value.code == "unsupported_inherited_config"


@pytest.mark.parametrize("state", ["", "relative/state", "/"])
def test_compose_environ_requires_an_absolute_non_root_state_dir(state: str) -> None:
    config = parse_config(_bytes(_config()))
    inherited = _inherited()
    if state:
        inherited["KELP_LOCAL_STATE_DIR"] = state
    else:
        inherited.pop("KELP_LOCAL_STATE_DIR")
    with pytest.raises(ProtocolError) as caught:
        compose_environ(inherited, config)
    assert caught.value.code == "invalid_state_dir"


def test_compose_environ_ignores_unlisted_inherited_kelp_config() -> None:
    config = parse_config(_bytes(_config()))
    env = compose_environ(
        _inherited(
            KELP_DB_PATH="/tmp/evil.db",
            KELP_HOST="0.0.0.0",
            KELP_PORT="9999",
            KELP_PLUGIN_ROOT="/tmp/evil-plugins",
            KELP_LOG_LEVEL="debug",
        ),
        config,
    )
    assert env["KELP_DB_PATH"].startswith("/tmp/kelp-local-state/")
    assert env["KELP_HOST"] == "127.0.0.1"
    assert env["KELP_PORT"] == "0"
    assert env["KELP_PLUGIN_ROOT"].startswith("/tmp/kelp-local-state/")
    assert "KELP_LOG_LEVEL" not in env


def test_compose_environ_passes_optional_secrets() -> None:
    config = parse_config(
        _bytes(
            _config(
                secrets={
                    "KELP_AUTH_TOKEN": AUTH,
                    "KELP_PROVIDER_API_KEY": API_KEY,
                    "KELP_CSRF_TOKEN": "csrf",
                    "KELP_SESSION_SECRET": "session",
                }
            )
        )
    )
    env = compose_environ(_inherited(), config)
    assert env["KELP_CSRF_TOKEN"] == "csrf"
    assert env["KELP_SESSION_SECRET"] == "session"


def test_compose_environ_omits_absent_optional_values() -> None:
    config = parse_config(_bytes(_config()))
    inherited = _inherited()
    inherited.pop("KELP_PRIMARY_MODEL")
    env = compose_environ(inherited, config)
    assert "KELP_PRIMARY_MODEL" not in env
    assert "KELP_PROVIDER_BASE_URL" in env


def test_composed_mapping_is_accepted_by_server_config(tmp_path: Path) -> None:
    state = tmp_path / "state"
    config = parse_config(_bytes(_config()))
    env = compose_environ(
        _inherited(KELP_LOCAL_STATE_DIR=str(state)),
        config,
    )

    server_config = ServerConfig.from_env(env)

    assert server_config.host == "127.0.0.1"
    assert server_config.port == 0
    assert server_config.skill_root is None
    assert server_config.db_path == state / "db" / "chat.db"
    assert server_config.artifact_dir == state / "artifacts"
    assert server_config.audit_dir == state / "audit"
    assert server_config.usage_dir == state / "usage"
    assert server_config.workspace_root == state / "workspace"
    assert server_config.auth_token == AUTH
    assert server_config.kernel.api_key == API_KEY
    assert server_config.kernel.base_url == "http://127.0.0.1:9/v1"

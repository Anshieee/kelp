"""Process-level acceptance for the Linux local-supervisor entrypoint.

These tests spawn ``python -I -B -m chat.local_serve`` with a real
``AF_UNIX``/``SOCK_STREAM`` stdin socketpair, exchange the dedicated frames,
and prove the listener is loopback-only and owner-authenticated. They never
inspect or parse stdout/stderr for control; captured output is used only to
assert that fake credentials did not leak.
"""

from __future__ import annotations

import http.client
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from chat.local_channel import (
    CONFIG_MAX_BYTES,
    STATUS_MAX_BYTES,
    ProtocolError,
    read_frame,
    write_frame,
)

ROOT = Path(__file__).resolve().parents[1]
NONCE = "fedcba9876543210fedcba9876543210"
AUTH = "fake-local-auth-token-0123456789"
API_KEY = "fake-local-provider-key-0123456789"
CSRF = "fake-local-csrf-token"
SESSION = "fake-local-session-secret"
BASE_URL = "http://127.0.0.1:9/v1"


@pytest.fixture(scope="module")
def sidecar_python(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A disposable interpreter where ``-I -B -m chat.local_serve`` resolves.

    ``-I`` removes the current directory from ``sys.path``, so the contract
    command cannot import the checkout by itself. The fixture builds an
    isolated venv whose ``.pth`` names this checkout and the parent test
    environment's site-packages; it does not modify either installation.
    """

    if not sys.platform.startswith("linux"):
        pytest.skip("the local supervisor is Linux-only")
    base = tmp_path_factory.mktemp("sidecar-python")
    venv = base / "venv"
    created = subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if created.returncode != 0:
        pytest.skip(f"cannot build disposable interpreter: {created.stderr[-200:]}")
    python = venv / "bin" / "python"
    site_dir = Path(
        subprocess.check_output(
            [str(python), "-c", "import site; print(site.getsitepackages()[0])"],
            text=True,
            timeout=30,
        ).strip()
    )
    parent_sites = subprocess.check_output(
        [
            sys.executable,
            "-c",
            "import site; print('\\n'.join(site.getsitepackages()))",
        ],
        text=True,
        timeout=30,
    ).splitlines()
    (site_dir / "kelp_local_sidecar_test.pth").write_text(
        "\n".join([str(ROOT), *parent_sites]) + "\n",
        encoding="utf-8",
    )
    probe = subprocess.run(
        [str(python), "-I", "-B", "-c", "import chat, kernel, openshell, skills"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if probe.returncode != 0:
        pytest.skip(f"disposable interpreter cannot import the checkout: {probe.stderr}")
    return str(python)


class Child:
    def __init__(self, process: subprocess.Popen, out: Path, err: Path):
        self.process = process
        self.out = out
        self.err = err

    def wait(self, timeout: float = 15.0) -> int:
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
            raise AssertionError("local sidecar did not exit") from None

    def output(self) -> bytes:
        return self.out.read_bytes() + self.err.read_bytes()


def _spawn(
    python: str,
    tmp_path: Path,
    child_socket: socket.socket,
    env: dict[str, str],
) -> Child:
    out = tmp_path / "stdout.log"
    err = tmp_path / "stderr.log"
    with out.open("wb") as out_handle, err.open("wb") as err_handle:
        process = subprocess.Popen(
            [python, "-I", "-B", "-m", "chat.local_serve"],
            cwd=ROOT,
            env=env,
            stdin=child_socket,
            stdout=out_handle,
            stderr=err_handle,
        )
    child_socket.close()
    return Child(process, out, err)


def _env(state: Path | None, tmp_path: Path, **overrides: str) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "KELP_PROVIDER_BASE_URL": BASE_URL,
        "KELP_PRIMARY_MODEL": "test-model",
    }
    if state is not None:
        env["KELP_LOCAL_STATE_DIR"] = str(state)
    env.update(overrides)
    return env


def _config_payload(**overrides: object) -> bytes:
    payload: dict[str, object] = {
        "v": 1,
        "type": "config",
        "nonce": NONCE,
        "secrets": {
            "KELP_AUTH_TOKEN": AUTH,
            "KELP_PROVIDER_API_KEY": API_KEY,
            "KELP_CSRF_TOKEN": CSRF,
            "KELP_SESSION_SECRET": SESSION,
        },
    }
    payload.update(overrides)
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _send(parent: socket.socket, payload: bytes) -> None:
    write_frame(parent, payload, max_bytes=CONFIG_MAX_BYTES, deadline=5.0)


def _status(parent: socket.socket, timeout: float = 30.0) -> dict[str, object]:
    parent.settimeout(timeout)
    frame = read_frame(parent, max_bytes=STATUS_MAX_BYTES, deadline=timeout)
    return json.loads(frame)


def _http(port: int, path: str, token: str | None = None) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _port_refused(port: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                pass
        except OSError:
            return True
        time.sleep(0.05)
    return False


def _error_run(
    python: str,
    tmp_path: Path,
    env: dict[str, str],
    *,
    frame: bytes | None,
    close: bool = False,
) -> tuple[dict[str, object] | None, int, bytes]:
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    sidecar = _spawn(python, tmp_path, child, env)
    status: dict[str, object] | None = None
    try:
        if close:
            parent.close()
        else:
            assert frame is not None
            _send(parent, frame)
            status = _status(parent)
    finally:
        if parent.fileno() >= 0:
            parent.close()
        code = sidecar.wait()
    return status, code, sidecar.output()


def test_ready_serves_authed_health_and_eof_shuts_down(
    sidecar_python: str, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    sidecar = _spawn(
        sidecar_python,
        tmp_path,
        child,
        _env(state, tmp_path),
    )
    port: int | None = None
    try:
        _send(parent, _config_payload())
        ready = _status(parent)
        assert ready["v"] == 1
        assert ready["type"] == "ready"
        assert ready["nonce"] == NONCE
        assert ready["host"] == "127.0.0.1"
        assert isinstance(ready["port"], int) and 1 <= ready["port"] <= 65535
        port = int(ready["port"])

        # Bootstrap replaced stdin with /dev/null and kept the channel on a
        # different, non-inheritable descriptor.
        assert os.readlink(f"/proc/{sidecar.process.pid}/fd/0") == os.devnull
        channels = [
            entry
            for entry in Path(f"/proc/{sidecar.process.pid}/fd").iterdir()
            if entry.name != "0"
            and stat.S_ISSOCK(os.stat(entry).st_mode)
        ]
        assert channels

        # Frame secrets never enter argv or the inherited environment.
        environ = Path(f"/proc/{sidecar.process.pid}/environ").read_bytes()
        argv = Path(f"/proc/{sidecar.process.pid}/cmdline").read_bytes()
        for secret in (AUTH, API_KEY, CSRF, SESSION):
            assert secret.encode() not in environ
            assert secret.encode() not in argv

        status, body = _http(port, "/api/health")
        assert status == 401
        assert json.loads(body)["error"]["code"] == "unauthorized"

        status, body = _http(port, "/api/health", token=AUTH)
        assert status == 200
        assert json.loads(body) == {"ok": True}
    finally:
        parent.close()
        code = sidecar.wait()

    assert code == 0
    assert port is not None
    assert _port_refused(port)
    logs = sidecar.output()
    for secret in (AUTH, API_KEY, CSRF, SESSION):
        assert secret.encode() not in logs


def test_sigterm_and_eof_cleanup_allow_the_same_state_root(
    sidecar_python: str, tmp_path: Path
) -> None:
    state = tmp_path / "state"

    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    first = _spawn(sidecar_python, tmp_path, child, _env(state, tmp_path))
    first_port: int | None = None
    try:
        _send(parent, _config_payload())
        first_port = int(_status(parent)["port"])
        os.kill(first.process.pid, signal.SIGTERM)
        code = first.wait()
    finally:
        if parent.fileno() >= 0:
            parent.close()
    assert code == 0
    assert first_port is not None
    assert _port_refused(first_port)

    # The database lock, listener, and watcher must be gone before a second
    # process can adopt the very same isolated state root.
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    second = _spawn(sidecar_python, tmp_path, child, _env(state, tmp_path))
    second_port: int | None = None
    try:
        _send(parent, _config_payload())
        second_port = int(_status(parent)["port"])
        parent.close()
        code = second.wait()
    finally:
        if parent.fileno() >= 0:
            parent.close()
    assert code == 0
    assert second_port is not None
    assert _port_refused(second_port)
    assert AUTH.encode() not in second.output()


def test_non_socket_stdin_is_refused_before_boot(
    sidecar_python: str, tmp_path: Path
) -> None:
    result = subprocess.run(
        [sidecar_python, "-I", "-B", "-m", "chat.local_serve"],
        cwd=ROOT,
        env=_env(tmp_path / "state", tmp_path),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr == b"kelp-local: invalid_channel\n"
    assert not (tmp_path / "state").exists()


def test_unexpected_byte_after_ready_shuts_down(
    sidecar_python: str, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    sidecar = _spawn(sidecar_python, tmp_path, child, _env(state, tmp_path))
    port: int | None = None
    try:
        _send(parent, _config_payload())
        ready = _status(parent)
        port = int(ready["port"])
        parent.sendall(b"x")
    finally:
        if parent.fileno() >= 0:
            parent.close()
        code = sidecar.wait()

    assert code == 0
    assert port is not None
    assert _port_refused(port)
    logs = sidecar.output()
    assert AUTH.encode() not in logs
    assert API_KEY.encode() not in logs


def test_trailing_byte_before_ready_is_a_refused_startup(
    sidecar_python: str, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    sidecar = _spawn(sidecar_python, tmp_path, child, _env(state, tmp_path))
    try:
        _send(parent, _config_payload())
        # One byte after the complete config frame, before readiness was ever
        # announced. The child must refuse to become ready, report exactly one
        # error frame, and exit non-zero.
        parent.sendall(b"x")
        status = _status(parent)
        with pytest.raises(ProtocolError):
            read_frame(parent, max_bytes=STATUS_MAX_BYTES, deadline=10.0)
    finally:
        if parent.fileno() >= 0:
            parent.close()
        code = sidecar.wait()
    assert status == {
        "v": 1,
        "type": "error",
        "nonce": NONCE,
        "code": "bad_frame",
    }
    assert code == 2
    assert not state.exists()


def test_eof_before_ready_never_reports_ready(
    sidecar_python: str, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    sidecar = _spawn(sidecar_python, tmp_path, child, _env(state, tmp_path))
    try:
        _send(parent, _config_payload())
        # Half-close: the parent read side stays open, so a status frame can
        # still be delivered if the child chooses to send one.
        parent.shutdown(socket.SHUT_WR)
        status = _status(parent)
        with pytest.raises(ProtocolError):
            read_frame(parent, max_bytes=STATUS_MAX_BYTES, deadline=10.0)
    finally:
        if parent.fileno() >= 0:
            parent.close()
        code = sidecar.wait()
    assert status["type"] == "error"
    assert status["code"] == "bad_frame"
    assert status["nonce"] == NONCE
    assert code == 2
    assert not state.exists()


def test_closed_channel_after_config_exits_nonzero(
    sidecar_python: str, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    sidecar = _spawn(sidecar_python, tmp_path, child, _env(state, tmp_path))
    try:
        _send(parent, _config_payload())
        parent.close()
        code = sidecar.wait()
    finally:
        if parent.fileno() >= 0:
            parent.close()
    assert code == 2
    assert not state.exists()


def test_malformed_frame_fails_without_state_or_ready(
    sidecar_python: str, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    status, code, logs = _error_run(
        sidecar_python,
        tmp_path,
        _env(state, tmp_path),
        frame=b"{",
    )
    assert status == {"v": 1, "type": "error", "nonce": None, "code": "invalid_json"}
    assert code == 2
    assert not state.exists()
    assert b"Traceback" not in logs
    assert AUTH.encode() not in logs
    assert API_KEY.encode() not in logs


def test_closed_channel_fails_without_state(
    sidecar_python: str, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    status, code, logs = _error_run(
        sidecar_python,
        tmp_path,
        _env(state, tmp_path),
        frame=None,
        close=True,
    )
    assert status is None
    assert code == 2
    assert not state.exists()
    assert b"Traceback" not in logs


def test_missing_state_dir_fails_with_echoed_nonce(
    sidecar_python: str, tmp_path: Path
) -> None:
    status, code, logs = _error_run(
        sidecar_python,
        tmp_path,
        _env(None, tmp_path),
        frame=_config_payload(),
    )
    assert status == {
        "v": 1,
        "type": "error",
        "nonce": NONCE,
        "code": "invalid_state_dir",
    }
    assert code == 2
    assert AUTH.encode() not in logs


@pytest.mark.parametrize("state_text", ["relative-state", "/"])
def test_unsafe_state_dir_is_refused(
    sidecar_python: str, tmp_path: Path, state_text: str
) -> None:
    status, code, _logs = _error_run(
        sidecar_python,
        tmp_path,
        _env(tmp_path / "state", tmp_path, KELP_LOCAL_STATE_DIR=state_text),
        frame=_config_payload(),
    )
    assert status is not None
    assert status["type"] == "error"
    assert status["code"] == "invalid_state_dir"
    assert code == 2


def test_inherited_credentials_are_refused(
    sidecar_python: str, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    status, code, logs = _error_run(
        sidecar_python,
        tmp_path,
        _env(state, tmp_path, KELP_AUTH_TOKEN="inherited-secret"),
        frame=_config_payload(),
    )
    assert status == {
        "v": 1,
        "type": "error",
        "nonce": NONCE,
        "code": "inherited_secret",
    }
    assert code == 2
    assert not state.exists()
    assert b"inherited-secret" not in logs


def test_tenant_secrets_file_is_refused(
    sidecar_python: str, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    status, code, _logs = _error_run(
        sidecar_python,
        tmp_path,
        _env(
            state,
            tmp_path,
            KELP_TENANT_SECRETS_FILE=str(tmp_path / "secrets.json"),
        ),
        frame=_config_payload(),
    )
    assert status is not None
    assert status["code"] == "unsupported_inherited_config"
    assert code == 2
    assert not state.exists()


def test_frame_missing_required_secret_echoes_nonce(
    sidecar_python: str, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    payload = _config_payload(
        secrets={"KELP_AUTH_TOKEN": AUTH},
    )
    status, code, _logs = _error_run(
        sidecar_python,
        tmp_path,
        _env(state, tmp_path),
        frame=payload,
    )
    assert status == {
        "v": 1,
        "type": "error",
        "nonce": NONCE,
        "code": "missing_secret",
    }
    assert code == 2
    assert not state.exists()


def test_unknown_secret_is_refused(
    sidecar_python: str, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    payload = _config_payload(
        secrets={
            "KELP_AUTH_TOKEN": AUTH,
            "KELP_PROVIDER_API_KEY": API_KEY,
            "KELP_VAULT_KEY": "would-bypass",
        }
    )
    status, code, logs = _error_run(
        sidecar_python,
        tmp_path,
        _env(state, tmp_path),
        frame=payload,
    )
    assert status is not None
    assert status["code"] == "unknown_secret"
    assert code == 2
    assert not state.exists()
    assert b"would-bypass" not in logs


def test_invalid_nonce_is_reported_without_echo(
    sidecar_python: str, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    payload = _config_payload(nonce="A" * 32)
    status, code, _logs = _error_run(
        sidecar_python,
        tmp_path,
        _env(state, tmp_path),
        frame=payload,
    )
    assert status == {
        "v": 1,
        "type": "error",
        "nonce": None,
        "code": "invalid_nonce",
    }
    assert code == 2
    assert not state.exists()


def test_duplicate_keys_are_reported_without_echo(
    sidecar_python: str, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    payload = (
        '{"v":1,"v":1,"type":"config","nonce":"'
        + NONCE
        + '","secrets":{"KELP_AUTH_TOKEN":"'
        + AUTH
        + '","KELP_PROVIDER_API_KEY":"'
        + API_KEY
        + '"}}'
    ).encode()
    status, code, _logs = _error_run(
        sidecar_python,
        tmp_path,
        _env(state, tmp_path),
        frame=payload,
    )
    assert status == {
        "v": 1,
        "type": "error",
        "nonce": None,
        "code": "duplicate_key",
    }
    assert code == 2
    assert not state.exists()


def test_missing_provider_url_fails_before_state_or_ready(
    sidecar_python: str, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    env = _env(state, tmp_path)
    env.pop("KELP_PROVIDER_BASE_URL")
    status, code, _logs = _error_run(
        sidecar_python,
        tmp_path,
        env,
        frame=_config_payload(),
    )
    assert status is not None
    assert status["type"] == "error"
    assert status["code"] == "invalid_config"
    assert code == 2
    assert not state.exists()


def _unit_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_serve: object,
) -> tuple[int, dict[str, object] | None, bool]:
    """Exercise ``_run`` in-process with a fake ``serve`` implementation.

    Returns ``(exit_code, first_status_frame, second_frame_present)``. The
    second read proves the child never emits a second frame: ``_run`` closes
    the channel before returning, so the read sees EOF instead.
    """

    import chat.local_serve as local_serve
    import chat.server as server_module

    state = tmp_path / "unit-state"
    monkeypatch.setenv("KELP_LOCAL_STATE_DIR", str(state))
    monkeypatch.setenv("KELP_PROVIDER_BASE_URL", BASE_URL)
    monkeypatch.setenv("KELP_PRIMARY_MODEL", "test-model")
    for name in (
        "KELP_AUTH_TOKEN",
        "KELP_PROVIDER_API_KEY",
        "KELP_CSRF_TOKEN",
        "KELP_SESSION_SECRET",
        "KELP_PLUGINS_ALLOW_UNPINNED",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(server_module, "serve", fake_serve)

    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    status: dict[str, object] | None = None
    second = False
    try:
        _send(parent, _config_payload())
        code = local_serve._run(child)
        if parent.fileno() >= 0:
            try:
                status = _status(parent, timeout=5.0)
            except ProtocolError:
                status = None
            try:
                read_frame(parent, max_bytes=STATUS_MAX_BYTES, deadline=2.0)
                second = True
            except ProtocolError:
                second = False
    finally:
        if parent.fileno() >= 0:
            parent.close()
    return code, status, second


def test_invalid_bound_endpoint_is_startup_failed_not_bad_frame(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_serve(config, *, environ=None, on_listening=None, stop_event=None):
        assert on_listening is not None
        # A listener that did not bind the advertised loopback endpoint is a
        # server fault, never a protocol violation.
        on_listening("0.0.0.0", 4321, object())
        return 0

    code, status, second = _unit_run(monkeypatch, tmp_path, fake_serve)
    assert status == {
        "v": 1,
        "type": "error",
        "nonce": NONCE,
        "code": "startup_failed",
    }
    assert code == 1
    assert second is False


def test_bind_failure_reports_startup_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_serve(config, *, environ=None, on_listening=None, stop_event=None):
        raise OSError("cannot bind")

    code, status, second = _unit_run(monkeypatch, tmp_path, fake_serve)
    assert status == {
        "v": 1,
        "type": "error",
        "nonce": NONCE,
        "code": "startup_failed",
    }
    assert code == 1
    assert second is False


def test_ready_write_failure_cannot_exit_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import chat.local_serve as local_serve

    def fail_ready(*_args: object, **_kwargs: object) -> None:
        raise ProtocolError("bad_frame")

    def fake_serve(config, *, environ=None, on_listening=None, stop_event=None):
        assert on_listening is not None
        on_listening("127.0.0.1", 4567, object())
        return 0

    # A real closed peer is refused earlier as a pre-ready EOF, so inject the
    # write failure at the ready seam: the accounting under test is that a
    # ready frame which never completed consumes the slot, forbids a second
    # frame, and keeps the exit status non-zero.
    monkeypatch.setattr(local_serve, "send_ready", fail_ready)
    code, status, second = _unit_run(monkeypatch, tmp_path, fake_serve)
    assert status is None
    assert code == 1
    assert second is False


def test_serve_returning_without_readiness_cannot_exit_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_serve(config, *, environ=None, on_listening=None, stop_event=None):
        return 0

    code, status, second = _unit_run(monkeypatch, tmp_path, fake_serve)
    assert status == {
        "v": 1,
        "type": "error",
        "nonce": NONCE,
        "code": "startup_failed",
    }
    assert code == 1
    assert second is False


def test_complete_ready_send_allows_exit_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_serve(config, *, environ=None, on_listening=None, stop_event=None):
        assert on_listening is not None
        on_listening("127.0.0.1", 4567, object())
        return 0

    code, status, second = _unit_run(monkeypatch, tmp_path, fake_serve)
    assert status == {
        "v": 1,
        "type": "ready",
        "nonce": NONCE,
        "host": "127.0.0.1",
        "port": 4567,
    }
    assert code == 0
    assert second is False


def test_watcher_survives_a_stale_config_read_timeout() -> None:
    import chat.local_serve as local_serve

    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    # ``read_frame`` leaves its absolute config deadline on the channel. The
    # watcher must use its own bounded read, not inherit that timeout.
    child.settimeout(30.0)
    stop = threading.Event()
    watcher = local_serve._Watcher(child, stop)
    try:
        watcher.start()
        # A clean channel must be confirmed promptly even while the watcher
        # loop is polling: readiness must not wait on its select cycle.
        started = time.monotonic()
        assert watcher.intact() is True
        assert time.monotonic() - started < 1.0
        started = time.monotonic()
        parent.close()
        watcher.join()
        assert stop.is_set()
        assert watcher.reason() == "eof"
        assert time.monotonic() - started < 2.0
    finally:
        watcher.close()
        if child.fileno() >= 0:
            child.close()


class _ShutdownSpy:
    def __init__(self) -> None:
        self.calls = 0
        self.service_actions = None

    def shutdown(self) -> None:
        self.calls += 1


def test_local_signals_only_request_stop_until_the_server_loop_observes_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import chat.server as server_module

    spy = _ShutdownSpy()

    class InlineThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self) -> None:
            self.target()

    monkeypatch.setattr(server_module.threading, "Thread", InlineThread)
    channel_stop = threading.Event()
    previous = server_module._install_stop_handlers(
        spy, threading.Event(), stop_event=channel_stop
    )
    try:
        signal.raise_signal(signal.SIGTERM)
        assert spy.calls == 0
        assert spy.service_actions is not None
        channel_stop.set()
        spy.service_actions()
        signal.raise_signal(signal.SIGINT)
        spy.service_actions()
        assert spy.calls == 1
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def test_paired_signal_and_channel_stop_dispatch_once() -> None:
    import chat.server as server_module

    spy = _ShutdownSpy()
    server_stop = threading.Event()
    channel_stop = threading.Event()
    previous = server_module._install_stop_handlers(
        spy, server_stop, stop_event=channel_stop
    )
    try:
        channel_stop.set()
        assert spy.service_actions is not None
        spy.service_actions()
        signal.raise_signal(signal.SIGTERM)
        deadline = time.monotonic() + 2.0
        while spy.calls == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.2)  # a second dispatch would have landed by now
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    assert spy.calls == 1


def test_inherited_allow_unpinned_is_refused(
    sidecar_python: str, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    status, code, _logs = _error_run(
        sidecar_python,
        tmp_path,
        _env(state, tmp_path, KELP_PLUGINS_ALLOW_UNPINNED="1"),
        frame=_config_payload(),
    )
    assert status == {
        "v": 1,
        "type": "error",
        "nonce": NONCE,
        "code": "unsupported_inherited_config",
    }
    assert code == 2
    assert not state.exists()


def test_hosted_cli_check_is_unchanged(tmp_path: Path) -> None:
    state = tmp_path / "hosted"
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("KELP_")
    }
    env.update(
        {
            "PYTHONPATH": str(ROOT),
            "KELP_PROVIDER_API_KEY": API_KEY,
            "KELP_PROVIDER_BASE_URL": BASE_URL,
            "KELP_AUTH_TOKEN": AUTH,
            "KELP_DB_PATH": str(state / "db" / "chat.db"),
            "KELP_ARTIFACT_DIR": str(state / "artifacts"),
            "KELP_AUDIT_DIR": str(state / "audit"),
            "KELP_USAGE_DIR": str(state / "usage"),
            "KELP_WORKSPACE_ROOT": str(state / "workspace"),
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "chat.server", "--check"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "configuration ok" in result.stdout
    assert AUTH not in result.stdout + result.stderr
    assert API_KEY not in result.stdout + result.stderr
    assert not state.exists()

"""Process-level acceptance for the Linux local-supervisor entrypoint.

These tests spawn ``python -I -B -m chat.local_serve`` with a real
``AF_UNIX``/``SOCK_STREAM`` stdin socketpair, exchange the dedicated frames,
and prove the listener selects the sealed Local profile: L1 loopback headers,
the exact chat target profile, an authorized turn over the real provider
transport, and event-log SSE replay. They never inspect or parse stdout/stderr
for control; captured output is used only to assert that fake credentials did
not leak.
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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


def _origin(port: int) -> str:
    """The only Origin value the Local L1 guard accepts for this listener."""

    return f"http://127.0.0.1:{port}"


def _http(
    port: int,
    path: str,
    token: str | None = None,
    *,
    origin: str | None = None,
    csrf: str | None = None,
    method: str = "GET",
    body: bytes | None = None,
    timeout: float = 5.0,
) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if origin is not None:
        headers["Origin"] = origin
    if csrf is not None:
        headers["X-CSRF-Token"] = csrf
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _sse_request(
    port: int,
    path: str,
    *,
    origin: str | None,
    token: str | None,
    needle: bytes | None = None,
    timeout: float = 10.0,
) -> tuple[int, bytes, bytes]:
    """Send one raw SSE request and read through ``needle`` (or EOF).

    Using a raw socket keeps the test independent of ``http.client``'s
    close-delimited response bookkeeping while still proving the exact bytes
    the listener put on the wire.
    """

    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: 127.0.0.1:{port}",
        "Accept: text/event-stream",
        "Connection: close",
    ]
    if origin is not None:
        lines.append(f"Origin: {origin}")
    if token is not None:
        lines.append(f"Authorization: Bearer {token}")
    request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(request)
        sock.settimeout(timeout)
        raw = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle is not None and needle in raw:
                break
            try:
                chunk = sock.recv(65536)
            except TimeoutError:
                break
            if not chunk:
                break
            raw.extend(chunk)
        head, _separator, body = bytes(raw).partition(b"\r\n\r\n")
        status = int(head.split()[1]) if head else 0
        return status, head, body


PROVIDER_TEXT = "synthetic-provider-text-9f3a"


def _provider_sse() -> bytes:
    """A minimal OpenAI-compatible streaming answer with usage and ``[DONE]``."""

    def chunk(delta: dict[str, Any], finish: str | None = None) -> str:
        payload = {
            "id": "chatcmpl-local-test",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "test-model",
            "choices": [
                {"index": 0, "delta": delta, "finish_reason": finish}
            ],
        }
        return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"

    usage = {
        "id": "chatcmpl-local-test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test-model",
        "choices": [],
        "usage": {
            "prompt_tokens": 7,
            "completion_tokens": 3,
            "total_tokens": 10,
        },
    }
    return (
        chunk({"role": "assistant", "content": ""})
        + chunk({"content": "synthetic-provider-text-"})
        + chunk({"content": "9f3a"})
        + chunk({}, "stop")
        + f"data: {json.dumps(usage, separators=(',', ':'))}\n\n"
        + "data: [DONE]\n\n"
    ).encode("utf-8")


class _ProviderStub:
    """A loopback OpenAI-compatible endpoint owned by the test process."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.paths: list[str] = []
        provider = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - stdlib spelling
                length = int(self.headers.get("Content-Length") or "0")
                raw = self.rfile.read(length)
                provider.paths.append(self.path)
                provider.requests.append(json.loads(raw.decode("utf-8")))
                body = _provider_sse()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()

            def log_message(self, _format: str, *args: object) -> None:
                return None

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="fake-openai-provider",
            daemon=True,
        )

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)


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

        status, body = _http(
            port, "/api/health", origin=_origin(port)
        )
        assert status == 401
        assert json.loads(body)["error"]["code"] == "unauthorized"

        status, body = _http(
            port, "/api/health", AUTH, origin=_origin(port)
        )
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


def test_local_l1_healthz_separates_origin_and_bearer(
    sidecar_python: str, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    sidecar = _spawn(sidecar_python, tmp_path, child, _env(state, tmp_path))
    try:
        _send(parent, _config_payload())
        port = int(_status(parent)["port"])
        origin = _origin(port)

        status, body = _http(port, "/api/healthz", AUTH, origin=origin)
        assert status == 200, body
        assert json.loads(body)["ok"] is True

        # Origin is checked independently of the bearer: a wrong or missing
        # Origin is 403 even when the owner token is correct.
        for bad_origin in (
            f"http://localhost:{port}",
            "http://127.0.0.1:1",
            None,
        ):
            status, body = _http(
                port, "/api/healthz", AUTH, origin=bad_origin
            )
            assert status == 403, (bad_origin, body)
            assert json.loads(body)["error"]["code"] == "invalid_origin"

        # A missing or wrong bearer with a valid Origin is a distinct 401.
        for token in (None, "wrong-local-bearer"):
            status, body = _http(port, "/api/healthz", token, origin=origin)
            assert status == 401, (token, body)
            assert json.loads(body)["error"]["code"] == "unauthorized"
    finally:
        parent.close()
        code = sidecar.wait()

    assert code == 0
    assert AUTH.encode() not in sidecar.output()


def test_local_profile_refuses_authenticated_off_profile_routes(
    sidecar_python: str, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    sidecar = _spawn(sidecar_python, tmp_path, child, _env(state, tmp_path))
    try:
        _send(parent, _config_payload())
        port = int(_status(parent)["port"])
        for path in (
            "/api/runs",
            "/api/auth/session",
            "/api/readyz",
            "/credential-requests",
            "/phone/",
        ):
            status, body = _http(
                port, path, AUTH, origin=_origin(port)
            )
            assert status == 404, (path, body)
            assert json.loads(body)["error"]["code"] == "not_found"
            assert AUTH.encode() not in body
    finally:
        parent.close()
        code = sidecar.wait()

    assert code == 0


def _message_text(message: dict[str, Any]) -> str:
    return "".join(
        str(block.get("text", ""))
        for block in message.get("blocks", [])
        if isinstance(block, dict) and block.get("type") == "text"
    )


def test_local_authorized_turn_reaches_provider_and_replays_over_sse(
    sidecar_python: str, tmp_path: Path
) -> None:
    provider = _ProviderStub()
    provider.start()
    state = tmp_path / "state"
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    sidecar = _spawn(
        sidecar_python,
        tmp_path,
        child,
        _env(
            state,
            tmp_path,
            KELP_PROVIDER_BASE_URL=provider.base_url,
        ),
    )
    try:
        _send(parent, _config_payload())
        ready = _status(parent)
        port = int(ready["port"])
        origin = _origin(port)

        status, body = _http(port, "/api/healthz", AUTH, origin=origin)
        assert status == 200, body

        status, body = _http(
            port,
            "/api/threads",
            AUTH,
            origin=origin,
            csrf=CSRF,
            method="POST",
            body=b"{}",
        )
        assert status == 201, body
        thread_id = json.loads(body)["thread"]["id"]

        message = "hello local synthetic provider"
        status, body = _http(
            port,
            f"/api/threads/{thread_id}/turns",
            AUTH,
            origin=origin,
            csrf=CSRF,
            method="POST",
            body=json.dumps(
                {"client_turn_id": "ct-local-sse", "text": message},
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        assert status == 202, body
        assert json.loads(body)["turn_id"]

        snapshot: dict[str, Any] | None = None
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            status, body = _http(
                port, f"/api/threads/{thread_id}", AUTH, origin=origin
            )
            assert status == 200, body
            snapshot = json.loads(body)
            assistant = [
                item
                for item in snapshot["messages"]
                if item["role"] == "assistant"
            ]
            if (
                assistant
                and assistant[-1]["status"] == "complete"
                and PROVIDER_TEXT in _message_text(assistant[-1])
            ):
                break
            time.sleep(0.05)
        else:
            pytest.fail(f"turn did not complete; last snapshot: {snapshot}")

        assistant = [
            item for item in snapshot["messages"] if item["role"] == "assistant"
        ][-1]
        assert _message_text(assistant) == PROVIDER_TEXT

        # The provider saw the turn through the sealed Local transport and the
        # Local runtime offered no tools, so neither key is on the wire.
        assert provider.paths == ["/v1/chat/completions"]
        wire = provider.requests[0]
        assert "tools" not in wire
        assert "tool_choice" not in wire
        assert wire["stream"] is True
        assert wire["model"] == "test-model"
        assert [item["role"] for item in wire["messages"]] == ["user"]
        assert wire["messages"][0]["content"] == message

        status, head, raw = _sse_request(
            port,
            f"/api/threads/{thread_id}/stream",
            origin=origin,
            token=AUTH,
            needle=b"9f3a",
        )
        assert status == 200, head
        assert b"Content-Type: text/event-stream" in head
        assert PROVIDER_TEXT.encode() not in raw
        assert b"9f3a" in raw
        assert b"synthetic-provider-text-" in raw
        assert b"event: token_delta" in raw

        # The same guard protects SSE: no Origin is refused before routing.
        status, _head, body = _sse_request(
            port,
            f"/api/threads/{thread_id}/stream",
            origin=None,
            token=AUTH,
        )
        assert status == 403
        assert json.loads(body)["error"]["code"] == "invalid_origin"
    finally:
        parent.close()
        code = sidecar.wait()
        provider.close()

    assert code == 0
    assert PROVIDER_TEXT.encode() not in sidecar.output()


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


@pytest.mark.parametrize("local_only", [True, False])
def test_serve_forwards_local_only_to_profile_and_adapter(
    monkeypatch: pytest.MonkeyPatch, local_only: bool
) -> None:
    import chat.server as server_module

    calls: dict[str, Any] = {}
    observed: list[tuple[str, int]] = []

    class FakeApp:
        api = object()

        def close(self) -> None:
            calls["app_closed"] = True

    class FakeServer:
        server_address = ("127.0.0.1", 4567)
        daemon_threads = False

        def serve_forever(self, poll_interval: float = 0.5) -> None:
            calls["poll_interval"] = poll_interval

        def server_close(self) -> None:
            calls["server_closed"] = True

        def shutdown(self) -> None:
            pass

    def fake_build_app(
        config: object,
        *,
        provider: object = None,
        environ: object = None,
        local_only: bool = False,
    ) -> FakeApp:
        calls["build"] = (provider, environ, local_only)
        return FakeApp()

    def fake_make_server(
        api: object,
        host: str,
        port: int,
        *,
        local_only: bool = False,
    ) -> FakeServer:
        calls["make"] = (host, port, local_only)
        return FakeServer()

    monkeypatch.setattr(server_module, "build_app", fake_build_app)
    monkeypatch.setattr(server_module, "make_server", fake_make_server)
    monkeypatch.setattr(
        server_module, "_install_stop_handlers", lambda *args, **kwargs: {}
    )
    environ = {"KELP_HOST": "127.0.0.1"}
    config = SimpleNamespace(host="127.0.0.1", port=0)

    code = server_module.serve(
        config,
        environ=environ,
        local_only=local_only,
        on_listening=lambda host, port, _server: observed.append((host, port)),
    )

    assert code == 0
    assert calls["build"] == (None, environ, local_only)
    assert calls["make"] == ("127.0.0.1", 0, local_only)
    assert observed == [("127.0.0.1", 4567)]
    assert calls["app_closed"] is True
    assert calls["server_closed"] is True


def test_invalid_bound_endpoint_is_startup_failed_not_bad_frame(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_serve(
        config,
        *,
        environ=None,
        local_only=False,
        on_listening=None,
        stop_event=None,
    ):
        assert local_only is True
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
    def fake_serve(
        config,
        *,
        environ=None,
        local_only=False,
        on_listening=None,
        stop_event=None,
    ):
        assert local_only is True
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

    def fake_serve(
        config,
        *,
        environ=None,
        local_only=False,
        on_listening=None,
        stop_event=None,
    ):
        assert local_only is True
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
    def fake_serve(
        config,
        *,
        environ=None,
        local_only=False,
        on_listening=None,
        stop_event=None,
    ):
        assert local_only is True
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
    def fake_serve(
        config,
        *,
        environ=None,
        local_only=False,
        on_listening=None,
        stop_event=None,
    ):
        assert local_only is True
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

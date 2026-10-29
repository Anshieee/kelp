"""KEL-11 server boot, restart recovery, locking, and shutdown."""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import os
import re
import select
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from chat.server import (
    ConfigError,
    DatabaseLocked,
    ServerConfig,
    build_app,
    main,
)
from chat.service import ChatService
from chat.store import ChatStore

ROOT = Path(__file__).resolve().parents[1]
AUTH_TOKEN = "owner-token-1234567890"
API_KEY = "test-api-key-never-printed-12345"
BASE_URL = "http://127.0.0.1:9/v1"


class NoopProvider:
    """A provider that must never be asked to stream during these tests."""

    async def stream(self, request):  # pragma: no cover - assertions protect it
        raise AssertionError("no turn should run")
        yield  # pragma: no cover - makes this an async generator


class SlowProvider:
    """A provider that streams long enough for a stop signal to land mid-turn."""

    def __init__(self, deltas: int = 300, delay: float = 0.02):
        self.deltas = deltas
        self.delay = delay

    async def stream(self, request):
        from kernel.provider import StreamEnd, TextDelta, UsageReport

        for index in range(self.deltas):
            await asyncio.sleep(self.delay)
            yield TextDelta(f"token-{index} ")
        yield UsageReport(10, self.deltas, self.deltas + 10)
        yield StreamEnd("stop")


def _env(root: Path, **overrides: str) -> dict[str, str]:
    env = {
        "NEBIUS_API_KEY": API_KEY,
        "NEBIUS_TOKEN_FACTORY_BASE_URL": BASE_URL,
        "KELP_AUTH_TOKEN": AUTH_TOKEN,
        "KELP_DB_PATH": str(root / "db" / "chat.db"),
        "KELP_ARTIFACT_DIR": str(root / "artifacts"),
        "KELP_AUDIT_DIR": str(root / "audit"),
        "KELP_USAGE_DIR": str(root / "usage"),
        "KELP_WORKSPACE_ROOT": str(root / "workspace"),
        "KELP_HOST": "127.0.0.1",
        "KELP_PORT": "0",
    }
    env.update(overrides)
    return env


def _subprocess_env(root: Path, **overrides: str) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("KELP_")
        and key
        not in {
            "NEBIUS_API_KEY",
            "NEBIUS_BASE_URL",
            "NEBIUS_TOKEN_FACTORY_BASE_URL",
        }
    }
    env.update(_env(root, **overrides))
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(ROOT)
    return env


def _read_line(proc: subprocess.Popen[str], timeout: float = 15.0) -> str:
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return proc.stdout.readline()
        ready, _write, _errors = select.select([proc.stdout], [], [], 0.1)
        if ready:
            line = proc.stdout.readline()
            if line:
                return line
    raise AssertionError("the server did not print a startup line")


def _stop(proc: subprocess.Popen[str], sig: int = signal.SIGTERM) -> tuple[int, str, str]:
    if proc.poll() is None:
        proc.send_signal(sig)
    try:
        code = proc.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - regression guard
        proc.kill()
        proc.wait(timeout=5)
        raise AssertionError("server did not exit after SIGTERM")
    assert proc.stdout is not None and proc.stderr is not None
    return code, proc.stdout.read(), proc.stderr.read()


def _port_from_line(line: str) -> int:
    match = re.search(r"http://[^\s:]+:(\d+)", line)
    assert match is not None, line
    return int(match.group(1))


def _get(port: int, path: str, token: str | None = None) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _post(
    port: int,
    path: str,
    payload: dict[str, object],
    *,
    token: str | None = None,
    csrf: str | None = None,
) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if csrf:
        headers["X-CSRF-Token"] = csrf
    try:
        connection.request("POST", path, body=json.dumps(payload), headers=headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


_ACCESS_LOG = re.compile(r'^127\.0\.0\.1 - - \[[^\]]+\] "[^"]*" \d{3} -$')


def _shutdown_noise(stderr: str) -> str:
    """``stderr`` minus the HTTP adapter's access log (one line per request)."""

    return "\n".join(
        line
        for line in stderr.splitlines()
        if line.strip() and not _ACCESS_LOG.match(line)
    )


def _tree_snapshot(root: Path) -> list[tuple[str, int, int, str]]:
    """Every entry under ``root``: name, mode, size, content digest."""

    entries: list[tuple[str, int, int, str]] = []
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_dir():
            entries.append((relative, _mode(path), 0, ""))
        else:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            entries.append((relative, _mode(path), path.stat().st_size, digest))
    return entries


def _wait_for_token(db_path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                count = connection.execute(
                    "SELECT COUNT(*) FROM events WHERE type = 'token_delta'"
                ).fetchone()[0]
            finally:
                connection.close()
            if count:
                return
        except sqlite3.Error as error:  # DB/WAL may still be settling
            last_error = error
        time.sleep(0.05)
    raise AssertionError(f"no token was persisted before the kill: {last_error}")


# -- configuration ---------------------------------------------------------


def test_config_reads_documented_environment_and_defaults(tmp_path: Path) -> None:
    config = ServerConfig.from_env(_env(tmp_path))
    assert config.db_path == (tmp_path / "db" / "chat.db").resolve()
    assert config.artifact_dir == (tmp_path / "artifacts").resolve()
    assert config.audit_path == (tmp_path / "audit" / "audit.jsonl").resolve()
    assert config.usage_path == (tmp_path / "usage" / "usage.jsonl").resolve()
    assert config.host == "127.0.0.1"
    assert config.port == 0
    assert config.kernel.api_key == API_KEY
    assert config.auth_token == AUTH_TOKEN

    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in (
        "KELP_DB_PATH",
        "KELP_ARTIFACT_DIR",
        "KELP_AUDIT_DIR",
        "KELP_USAGE_DIR",
        "KELP_PORT",
        "KELP_AUTH_TOKEN",
    ):
        assert name in example


def test_config_uses_defaults_when_optional_paths_are_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = ServerConfig.from_env(
        {
            "NEBIUS_API_KEY": API_KEY,
            "KELP_AUTH_TOKEN": AUTH_TOKEN,
        }
    )
    assert config.db_path == (tmp_path / "var" / "chat.db").resolve()
    assert config.artifact_dir == (tmp_path / "var" / "workspace" / "var" / "artifacts").resolve()
    assert config.audit_path == (tmp_path / "var" / "audit" / "audit.jsonl").resolve()
    assert config.usage_path == (tmp_path / "var" / "usage" / "usage.jsonl").resolve()
    assert config.port == 8765


def test_missing_api_key_fails_fast_without_echoing_anything(
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = {"KELP_AUTH_TOKEN": AUTH_TOKEN}
    code = main(["--check"], env=env)
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert captured.err.count("\n") == 1
    assert "missing_api_key" in captured.err
    assert "NEBIUS_API_KEY" in captured.err


def test_missing_api_key_process_exits_nonzero_with_one_line(tmp_path: Path) -> None:
    env = _subprocess_env(tmp_path)
    env.pop("NEBIUS_API_KEY", None)
    result = subprocess.run(
        [sys.executable, "-m", "chat.server", "--check"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert len([line for line in result.stderr.splitlines() if line]) == 1
    assert "missing_api_key" in result.stderr
    assert "NEBIUS_API_KEY" in result.stderr


def test_invalid_auth_token_and_port_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as missing_token:
        ServerConfig.from_env({"NEBIUS_API_KEY": API_KEY})
    assert "KELP_AUTH_TOKEN" in str(missing_token.value)

    with pytest.raises(ConfigError) as bad_port:
        ServerConfig.from_env(_env(tmp_path, KELP_PORT="not-a-port"))
    assert "KELP_PORT" in str(bad_port.value)

    with pytest.raises(ConfigError) as out_of_range:
        ServerConfig.from_env(_env(tmp_path, KELP_PORT="70000"))
    assert "KELP_PORT" in str(out_of_range.value)


def test_config_repr_never_contains_credentials(tmp_path: Path) -> None:
    config = ServerConfig.from_env(_env(tmp_path))
    rendered = repr(config) + str(config)
    assert API_KEY not in rendered
    assert AUTH_TOKEN not in rendered
    assert "auth_token_configured=True" in rendered
    assert "api_key_configured=True" in rendered


def test_check_mode_is_a_dry_run_with_no_secret_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _env(tmp_path)
    code = main(["--check"], env=env)
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.strip() == "kelp: configuration ok"
    assert captured.err == ""
    assert API_KEY not in captured.out + captured.err
    assert AUTH_TOKEN not in captured.out + captured.err
    assert not Path(env["KELP_DB_PATH"]).exists()
    assert not Path(env["KELP_DB_PATH"] + ".lock").exists()


def test_check_mode_leaves_the_tree_byte_identical(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _env(tmp_path)
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    db_dir.chmod(0o755)
    (db_dir / "seed.txt").write_text("untouched", encoding="utf-8")
    before = _tree_snapshot(tmp_path)

    assert main(["--check"], env=env) == 0
    capsys.readouterr()

    assert _tree_snapshot(tmp_path) == before
    assert not (tmp_path / "artifacts").exists()
    assert not (tmp_path / "audit").exists()
    assert not (tmp_path / "usage").exists()
    assert not (tmp_path / "workspace").exists()
    assert _mode(db_dir) == 0o755


def test_ensure_directories_never_rechmods_existing_directories(tmp_path: Path) -> None:
    config = ServerConfig.from_env(_env(tmp_path))
    preexisting = [tmp_path / "db", tmp_path / "artifacts"]
    for directory in preexisting:
        directory.mkdir(parents=True)
        directory.chmod(0o755)

    config.ensure_directories()

    for directory in preexisting:
        assert _mode(directory) == 0o755
    for created in ("audit", "usage", "workspace"):
        # Only what this call created is tightened to owner-only.
        assert _mode(tmp_path / created) == 0o700


# -- documented command, auth, and clean SIGTERM ---------------------------


def test_documented_command_serves_authed_health_and_exits_on_sigterm(
    tmp_path: Path,
) -> None:
    env = _subprocess_env(tmp_path)
    proc = subprocess.Popen(
        [sys.executable, "-m", "chat.server"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        line = _read_line(proc)
        assert "listening" in line, line
        port = _port_from_line(line)

        status, body = _get(port, "/api/health")
        assert status == 401
        assert json.loads(body)["error"]["code"] == "unauthorized"

        status, body = _get(port, "/api/health", token=AUTH_TOKEN)
        assert status == 200
        assert json.loads(body) == {"ok": True}
    finally:
        code, out, err = _stop(proc)

    assert code == 0
    assert API_KEY not in out + err
    assert AUTH_TOKEN not in out + err


def test_sigint_closes_the_server_cleanly(tmp_path: Path) -> None:
    env = _subprocess_env(tmp_path)
    proc = subprocess.Popen(
        [sys.executable, "-m", "chat.server"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        assert "listening" in _read_line(proc)
    finally:
        code, out, err = _stop(proc, signal.SIGINT)
    assert code == 0
    assert API_KEY not in out + err
    assert AUTH_TOKEN not in out + err


def test_preexisting_directory_mode_survives_check_and_serve(tmp_path: Path) -> None:
    """The process adopts an existing 0755 layout; it never re-permissions it."""

    env = _subprocess_env(tmp_path)
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    db_dir.chmod(0o755)

    checked = subprocess.run(
        [sys.executable, "-m", "chat.server", "--check"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert checked.returncode == 0, checked.stderr
    assert _mode(db_dir) == 0o755
    assert not (tmp_path / "audit").exists()

    proc = subprocess.Popen(
        [sys.executable, "-m", "chat.server"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        line = _read_line(proc)
        assert "listening" in line, line
        status, _body = _get(_port_from_line(line), "/api/health", token=AUTH_TOKEN)
        assert status == 200
    finally:
        code, out, err = _stop(proc)

    assert code == 0
    assert API_KEY not in out + err
    assert _mode(db_dir) == 0o755
    # Directories this process created are still owner-only.
    assert _mode(tmp_path / "audit") == 0o700
    assert _mode(tmp_path / "usage") == 0o700


# -- shutdown settles in-flight turns (§10.4) ------------------------------

CSRF_TOKEN = "csrf-token-for-shutdown-tests"

_CHILD_SLOW_SERVE = r'''
import asyncio, sys

import chat.server as server
from kernel.provider import StreamEnd, TextDelta, UsageReport


class SlowProvider:
    async def stream(self, request):
        for index in range(600):
            await asyncio.sleep(0.02)
            yield TextDelta(f"token-{index} ")
        yield UsageReport(10, 600, 610)
        yield StreamEnd("stop")


server.TokenFactoryClient = lambda config: SlowProvider()
sys.exit(server.main([]))
'''


def test_application_close_cancels_an_active_turn_as_interrupted(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    config = ServerConfig.from_env(env)
    app = build_app(config, provider=SlowProvider(), environ=env)
    thread = app.service.create_thread()
    payload, created = app.api.runner.run(
        app.service.send(
            thread["id"],
            text="hold this turn open",
            client_turn_id="ct-close",
        )
    )
    assert created is True
    _wait_for_token(config.db_path)

    app.close()

    # The turn task was cancelled *and* awaited: nothing is left to be
    # destroyed when the loop closes.
    assert app.service._tasks == {}  # noqa: SLF001 - shutdown invariant
    turn_id = payload["turn_id"]

    store = ChatStore(config.db_path)
    try:
        turn = store.get_turn(turn_id)
        assert turn is not None
        assert turn["state"] == "finished"
        assert turn["reason"] == "interrupted"
        message = store.get_message(payload["assistant_message_id"])
        assert message is not None
        assert message["status"] == "interrupted"
        assert any(
            block.get("type") == "text" and block.get("text")
            for block in message["blocks"]
        )
        events = store.events_after(thread["id"])
        finished = [event for event in events if event["type"] == "turn_finished"]
        assert len(finished) == 1
        assert finished[0]["reason"] == "interrupted"
        assert store.list_approvals(status="pending") == []
    finally:
        store.close()


def test_sigterm_mid_turn_ends_interrupted_without_destroyed_tasks(
    tmp_path: Path,
) -> None:
    env = _subprocess_env(tmp_path, KELP_CSRF_TOKEN=CSRF_TOKEN)
    script = tmp_path / "serve_slow.py"
    script.write_text(_CHILD_SLOW_SERVE, encoding="utf-8")
    db_path = Path(env["KELP_DB_PATH"])

    proc = subprocess.Popen(
        [sys.executable, str(script)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        line = _read_line(proc)
        assert "listening" in line, line
        port = _port_from_line(line)

        status, body = _post(
            port, "/api/threads", {"title": "shutdown"}, token=AUTH_TOKEN, csrf=CSRF_TOKEN
        )
        assert status == 201, body
        thread_id = json.loads(body)["thread"]["id"]

        status, body = _post(
            port,
            f"/api/threads/{thread_id}/turns",
            {"text": "hold this turn open", "client_turn_id": "ct-sigterm"},
            token=AUTH_TOKEN,
            csrf=CSRF_TOKEN,
        )
        assert status == 202, body
        started = json.loads(body)
        _wait_for_token(db_path)
    finally:
        code, out, err = _stop(proc)

    assert code == 0
    assert API_KEY not in out + err
    assert AUTH_TOKEN not in out + err
    # Clean stderr: no traceback, no "Task was destroyed but it is pending!",
    # no "coroutine was never awaited" from the cancelled provider stream. The
    # adapter's access log is noise, not a shutdown warning.
    assert _shutdown_noise(err) == "", err
    assert "Task was destroyed" not in out + err
    assert "never awaited" not in out + err
    assert "Traceback" not in out + err

    store = ChatStore(db_path)
    try:
        turn = store.get_turn(started["turn_id"])
        assert turn is not None
        assert turn["state"] == "finished"
        assert turn["reason"] == "interrupted"
        message = store.get_message(started["assistant_message_id"])
        assert message is not None
        assert message["status"] == "interrupted"
        events = store.events_after(thread_id)
        finished = [event for event in events if event["type"] == "turn_finished"]
        assert len(finished) == 1
        assert finished[0]["reason"] == "interrupted"
    finally:
        store.close()


def test_second_instance_refuses_the_same_database(tmp_path: Path) -> None:
    env = _subprocess_env(tmp_path)
    first = subprocess.Popen(
        [sys.executable, "-m", "chat.server"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        assert "listening" in _read_line(first)
        second = subprocess.run(
            [sys.executable, "-m", "chat.server"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert second.returncode == 3
        assert "database_locked" in second.stderr
        assert "Another KELP server" in second.stderr
        assert API_KEY not in second.stdout + second.stderr
    finally:
        code, _out, _err = _stop(first)
    assert code == 0


def test_build_app_refuses_a_second_lock_in_the_same_process(tmp_path: Path) -> None:
    env = _env(tmp_path)
    first = build_app(ServerConfig.from_env(env), provider=NoopProvider(), environ=env)
    try:
        with pytest.raises(DatabaseLocked) as locked:
            build_app(ServerConfig.from_env(env), provider=NoopProvider(), environ=env)
        assert locked.value.code == "database_locked"
        assert (str(Path(env["KELP_DB_PATH"])) + ".lock") in str(locked.value)
    finally:
        first.close()


# -- recovery --------------------------------------------------------------


def _seed_crashed_turn(
    db_path: Path,
) -> tuple[str, str, str, str]:
    store = ChatStore(db_path)
    try:
        thread = store.create_thread(title="crash")
        user = store.create_message(
            thread_id=thread["id"],
            turn_id="turn_crash",
            role="user",
            status="complete",
            blocks=[{"type": "text", "text": "run the task"}],
        )
        assistant = store.create_message(
            thread_id=thread["id"],
            turn_id="turn_crash",
            role="assistant",
            status="streaming",
            blocks=[
                {"type": "text", "text": "partial answer"},
                {
                    "type": "tool_call",
                    "call_id": "call_running",
                    "tool": "schedule.create",
                    "label": "Schedule a task",
                    "summary": "",
                    "status": "running",
                    "duration_ms": None,
                    "effects": ["write"],
                    "approval_id": None,
                    "detail": {
                        "arguments": {},
                        "result_summary": None,
                        "result": None,
                        "error": None,
                    },
                },
                {
                    "type": "tool_call",
                    "call_id": "call_waiting",
                    "tool": "schedule.create",
                    "label": "Schedule a task",
                    "summary": "",
                    "status": "waiting_approval",
                    "duration_ms": None,
                    "effects": ["write"],
                    "approval_id": "apr_crash",
                    "detail": {
                        "arguments": {},
                        "result_summary": None,
                        "result": None,
                        "error": None,
                    },
                },
                {
                    "type": "approval",
                    "approval": {
                        "id": "apr_crash",
                        "thread_id": thread["id"],
                        "turn_id": "turn_crash",
                        "call_id": "call_waiting",
                        "source": "skill",
                        "tool": "schedule.create",
                        "status": "pending",
                        "requested_at": "2026-10-29T09:00:00Z",
                        "expires_at": "2099-01-01T00:00:00Z",
                        "decided_at": None,
                        "consumed_at": None,
                    },
                },
            ],
        )
        store.create_turn(
            thread_id=thread["id"],
            user_message_id=user["id"],
            assistant_message_id=assistant["id"],
            client_turn_id="ct-crash",
            budget={},
            turn_id="turn_crash",
        )
        store.update_turn("turn_crash", state="running", started_at="2026-10-29T09:00:00Z")
        store.save_approval(
            {
                "id": "apr_crash",
                "thread_id": thread["id"],
                "turn_id": "turn_crash",
                "call_id": "call_waiting",
                "source": "skill",
                "tool": "schedule.create",
                "status": "pending",
                "requested_at": "2026-10-29T09:00:00Z",
                "expires_at": "2099-01-01T00:00:00Z",
            }
        )
        return thread["id"], assistant["id"], user["id"], "turn_crash"
    finally:
        store.close()


def test_boot_recovery_fails_tool_blocks_expires_approvals_and_audits(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    config = ServerConfig.from_env(env)
    config.ensure_directories()
    thread_id, assistant_id, _user_id, turn_id = _seed_crashed_turn(config.db_path)

    app = build_app(config, provider=NoopProvider(), environ=env)
    try:
        assert app.recovery.closed_turns == (turn_id,)
        assert app.recovery.failed_tool_calls == 2
        assert app.recovery.expired_approvals == ("apr_crash",)

        message = app.store.get_message(assistant_id)
        assert message is not None
        assert message["status"] == "interrupted"
        tools = [block for block in message["blocks"] if block.get("type") == "tool_call"]
        assert [block["status"] for block in tools] == ["failed", "failed"]
        assert all(
            block["detail"]["error"]["code"] == "interrupted" for block in tools
        )
        approval_blocks = [
            block for block in message["blocks"] if block.get("type") == "approval"
        ]
        assert approval_blocks[0]["approval"]["status"] == "expired"

        card = app.store.get_approval("apr_crash")
        assert card is not None and card["status"] == "expired"
        assert card["decided_at"]

        turn = app.store.get_turn(turn_id)
        assert turn is not None
        assert turn["state"] == "finished"
        assert turn["reason"] == "interrupted"

        events = app.store.events_after(thread_id)
        finished = [event for event in events if event["type"] == "turn_finished"]
        assert len(finished) == 1
        assert finished[0]["reason"] == "interrupted"
        resolved = [event for event in events if event["type"] == "approval_resolved"]
        assert len(resolved) == 1
        assert resolved[0]["approval_id"] == "apr_crash"
        assert resolved[0]["status"] == "expired"

        expiries = app.audit.entries(event="approval_expired")
        assert len(expiries) == 1
        assert expiries[0]["request_id"] == "apr_crash"
        assert expiries[0]["turn_id"] == turn_id
        assert expiries[0]["decision"] == "expired"
        assert expiries[0]["outcome"] == "interrupted"
        audit_text = config.audit_path.read_text(encoding="utf-8")
        assert API_KEY not in audit_text
        assert AUTH_TOKEN not in audit_text

        # A second boot is a no-op: no duplicate turn_finished or expiry.
        app.close()
        second = build_app(config, provider=NoopProvider(), environ=env)
        try:
            assert second.recovery.closed_turns == ()
            assert second.recovery.expired_approvals == ()
            events = second.store.events_after(thread_id)
            assert len([event for event in events if event["type"] == "turn_finished"]) == 1
            assert (
                len([event for event in events if event["type"] == "approval_resolved"])
                == 1
            )
            assert len(second.audit.entries(event="approval_expired")) == 1
        finally:
            second.close()
    finally:
        app.close()


def test_boot_calls_recover_interrupted_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path)
    calls: list[str] = []
    original = ChatService.recover_interrupted

    def counted(self: ChatService) -> list[str]:
        calls.append("called")
        return original(self)

    monkeypatch.setattr(ChatService, "recover_interrupted", counted)
    app = build_app(ServerConfig.from_env(env), provider=NoopProvider(), environ=env)
    try:
        assert calls == ["called"]
    finally:
        app.close()


# -- kill -9 and real restart ---------------------------------------------


_CHILD_A = r"""
import asyncio, json, time
from chat.server import ServerConfig, build_app
from kernel.provider import StreamEnd, TextDelta, UsageReport

class SlowProvider:
    async def stream(self, request):
        for index in range(200):
            await asyncio.sleep(0.02)
            yield TextDelta(f"token-{index} ")
        yield UsageReport(10, 200, 210)
        yield StreamEnd("stop")

app = build_app(ServerConfig.from_env(), provider=SlowProvider())
thread = app.service.create_thread()
payload, _created = app.api.runner.run(
    app.service.send(
        thread["id"],
        text="kill this turn",
        client_turn_id="ct-kill9",
    )
)
print(json.dumps({
    "ready": True,
    "thread_id": thread["id"],
    "turn_id": payload["turn_id"],
    "assistant_message_id": payload["assistant_message_id"],
}), flush=True)
time.sleep(120)
"""


def test_kill_9_mid_turn_then_restart_settles_the_turn(tmp_path: Path) -> None:
    env = _subprocess_env(tmp_path)
    script = tmp_path / "kill_mid_turn.py"
    script.write_text(_CHILD_A, encoding="utf-8")

    child = subprocess.Popen(
        [sys.executable, str(script)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        ready = json.loads(_read_line(child))
        assert ready["ready"] is True
        db_path = Path(env["KELP_DB_PATH"])
        _wait_for_token(db_path)
    finally:
        child.kill()
        child.wait(timeout=10)

    restarted = subprocess.Popen(
        [sys.executable, "-m", "chat.server"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        line = _read_line(restarted)
        assert "recovered=1" in line, line
        port = _port_from_line(line)
        status, body = _get(port, "/api/health", token=AUTH_TOKEN)
        assert status == 200
        assert json.loads(body) == {"ok": True}
    finally:
        code, out, err = _stop(restarted)
    assert code == 0
    assert API_KEY not in out + err
    assert AUTH_TOKEN not in out + err

    store = ChatStore(db_path)
    try:
        turn = store.get_turn(ready["turn_id"])
        assert turn is not None
        assert turn["state"] == "finished"
        assert turn["reason"] == "interrupted"
        message = store.get_message(ready["assistant_message_id"])
        assert message is not None
        assert message["status"] == "interrupted"
        assert any(
            block.get("type") == "text" and block.get("text")
            for block in message["blocks"]
        )
        events = store.events_after(ready["thread_id"])
        finished = [event for event in events if event["type"] == "turn_finished"]
        assert len(finished) == 1
        assert finished[0]["reason"] == "interrupted"
        assert store.list_approvals(status="pending") == []
    finally:
        store.close()

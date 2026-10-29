"""KEL-16 M2-F: the deployment health probe.

The probe is the only new executable surface in this slice, so it is tested
directly: liveness against a real loopback server, readiness parsing of a
degraded body, the exit-code contract, and the rule that no env value or
server-supplied text reaches the output.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "healthcheck.py"

SECRET = "owner-token-never-printed-0123456789"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("deploy_healthcheck", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


healthcheck = _load()


class _Handler(BaseHTTPRequestHandler):
    status = 200
    body: Any = {"ok": True}
    seen: list[dict[str, str]] = []

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        type(self).seen.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization", ""),
            }
        )
        payload = json.dumps(self.body).encode("utf-8")
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:  # noqa: D401 - silence
        return None


@pytest.fixture
def server() -> Iterator[tuple[str, type[_Handler]]]:
    _Handler.seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[0], int(httpd.server_address[1])
    try:
        yield f"http://{host}:{port}", _Handler
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _set_response(handler: type[_Handler], status: int, body: Any) -> None:
    handler.status = status
    handler.body = body


@pytest.fixture
def garbage_server() -> Iterator[str]:
    """A socket that accepts one connection and answers with a non-HTTP line."""

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = int(listener.getsockname()[1])

    def serve() -> None:
        try:
            conn, _ = listener.accept()
            with conn:
                conn.recv(1024)
                conn.sendall(b"not-http\r\n\r\n")
        except OSError:  # the client hung up first; the test still asserts below
            pass
        finally:
            listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        listener.close()
        thread.join(timeout=5)


# -- URL construction --------------------------------------------------------


@pytest.mark.parametrize(
    "host,port,expected",
    [
        ("", 8765, "http://127.0.0.1:8765"),
        ("0.0.0.0", 8765, "http://127.0.0.1:8765"),
        ("::", 1, "http://127.0.0.1:1"),
        ("[::]", 2, "http://127.0.0.1:2"),
        ("10.0.0.5", 8765, "http://10.0.0.5:8765"),
        ("fd00::1", 8765, "http://[fd00::1]:8765"),
    ],
)
def test_base_url_is_dialable(host: str, port: int, expected: str) -> None:
    assert healthcheck.base_url(host, port) == expected


# -- body parsing ------------------------------------------------------------


def test_summarise_accepts_a_healthy_body() -> None:
    ready, failing, version, commit = healthcheck.summarise(
        {
            "ok": True,
            "status": "ok",
            "version": "0.1.0",
            "commit": "abc1234",
            "checks": {
                "store": {"ok": True},
                "provider": {"ok": True},
            },
        }
    )
    assert ready is True
    assert failing == []
    assert (version, commit) == ("0.1.0", "abc1234")


def test_summarise_names_failing_checks_and_drops_unknown_detail() -> None:
    ready, failing, version, commit = healthcheck.summarise(
        {
            "ok": False,
            "status": "degraded",
            "version": "0.1.0",
            "commit": "abc1234",
            "checks": {
                "store": {"ok": False, "detail": "unavailable"},
                "provider": {"ok": False, "detail": "/data/chat.db is missing"},
                "audit": {"ok": True},
                "x" * 80: {"ok": False},
            },
        }
    )
    assert ready is False
    assert failing == ["provider=unknown", "store=unavailable"]
    assert "/data" not in " ".join(failing)


def test_summarise_never_returns_an_unbounded_version() -> None:
    _, _, version, commit = healthcheck.summarise(
        {"ok": True, "version": "v" * 200, "commit": "c" * 200}
    )
    assert version == "unknown"
    assert commit == "unknown"


def test_summarise_treats_a_non_json_body_as_degraded() -> None:
    assert healthcheck.summarise(None) == (False, ["body"], "unknown", "unknown")


# -- liveness ----------------------------------------------------------------


def test_live_returns_zero_when_healthz_is_ok(
    server: tuple[str, type[_Handler]], capsys: pytest.CaptureFixture[str]
) -> None:
    url, handler = server
    _set_response(
        handler, 200, {"ok": True, "status": "ok", "version": "1.2.3", "commit": "abc1234"}
    )

    assert healthcheck.main(["--live", "--url", url]) == 0
    out = capsys.readouterr().out
    assert "healthz ok" in out
    assert "abc1234" in out
    assert handler.seen[-1]["path"] == "/api/healthz"
    assert handler.seen[-1]["authorization"] == ""


@pytest.mark.parametrize("status", [401, 404, 500, 503])
def test_live_fails_on_any_non_ok_answer(
    server: tuple[str, type[_Handler]],
    capsys: pytest.CaptureFixture[str],
    status: int,
) -> None:
    url, handler = server
    _set_response(handler, status, {"ok": False})

    assert healthcheck.main(["--live", "--url", url]) == 1
    assert f"http_status={status}" in capsys.readouterr().err


def test_live_fails_when_nothing_is_listening(capsys: pytest.CaptureFixture[str]) -> None:
    assert healthcheck.main(["--live", "--url", "http://127.0.0.1:9"]) == 1
    err = capsys.readouterr().err
    assert "healthz unreachable" in err


def test_live_survives_a_malformed_http_answer(
    garbage_server: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # `http.client` raises BadStatusLine (an HTTPException, not a URLError) for
    # this; the probe must report and exit 1 rather than die with a traceback.
    assert healthcheck.main(["--live", "--url", garbage_server]) == 1
    err = capsys.readouterr().err
    assert "healthz unreachable" in err
    assert "Traceback" not in err


def test_ready_survives_a_malformed_http_answer(
    garbage_server: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("KELP_AUTH_TOKEN", SECRET)

    assert healthcheck.main(["--ready", "--url", garbage_server]) == 1
    captured = capsys.readouterr()
    assert "readyz unreachable" in captured.err
    assert SECRET not in captured.out + captured.err


# -- readiness ---------------------------------------------------------------


def test_ready_uses_the_token_and_prints_no_secret(
    server: tuple[str, type[_Handler]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    url, handler = server
    _set_response(
        handler, 200, {"ok": True, "version": "1.2.3", "commit": "abc1234", "checks": {}}
    )
    monkeypatch.setenv("KELP_AUTH_TOKEN", SECRET)

    assert healthcheck.main(["--ready", "--url", url]) == 0
    captured = capsys.readouterr()
    assert handler.seen[-1]["path"] == "/api/readyz"
    assert handler.seen[-1]["authorization"] == f"Bearer {SECRET}"
    assert SECRET not in captured.out + captured.err


def test_ready_reports_failing_checks_on_503(
    server: tuple[str, type[_Handler]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    url, handler = server
    _set_response(
        handler,
        503,
        {
            "ok": False,
            "status": "degraded",
            "checks": {
                "store": {"ok": True},
                "provider": {"ok": False, "detail": "timeout"},
            },
        },
    )
    monkeypatch.setenv("KELP_AUTH_TOKEN", SECRET)

    assert healthcheck.main(["--ready", "--url", url]) == 1
    err = capsys.readouterr().err
    assert "readyz degraded: provider=timeout" in err
    assert SECRET not in err


def test_ready_without_a_token_is_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("KELP_AUTH_TOKEN", raising=False)

    assert healthcheck.main(["--ready", "--url", "http://127.0.0.1:9"]) == 2
    assert "KELP_AUTH_TOKEN is required" in capsys.readouterr().err


def test_ready_refuses_a_non_http_url(capsys: pytest.CaptureFixture[str]) -> None:
    assert healthcheck.main(["--ready", "--url", "file:///etc/passwd"]) == 2
    assert "must start with http:// or https://" in capsys.readouterr().err


def test_json_mode_is_machine_readable(
    server: tuple[str, type[_Handler]], capsys: pytest.CaptureFixture[str]
) -> None:
    url, handler = server
    _set_response(handler, 200, {"ok": True, "version": "1.2.3", "commit": "abc1234"})

    assert healthcheck.main(["--live", "--url", url, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["mode"] == "live"


# -- environment defaults ----------------------------------------------------


def test_port_must_be_an_integer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("KELP_PORT", "eighty")

    assert healthcheck.main(["--live"]) == 2
    assert "KELP_PORT must be an integer" in capsys.readouterr().err


def test_probe_defaults_to_loopback_for_a_wildcard_bind(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("KELP_HOST", "0.0.0.0")
    monkeypatch.setenv("KELP_PORT", "9")

    assert healthcheck.main(["--live"]) == 1
    # Nothing listens on port 9; the point is that it dialled 127.0.0.1.
    assert "unreachable" in capsys.readouterr().err

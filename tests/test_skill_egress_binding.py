"""Socket ``connect`` is bound to addresses resolved for allowed hosts.

While ``EgressClient.request`` runs, ``_EGRESS_ALLOWED`` is true. A handler may
supply its own ``transport`` callable (``github_write`` does), and that callable
runs in the same scope. Before this binding, a transport could call
``socket.connect(("93.184.216.35", 443))`` and escape the manifest allow-list
because the connect guard allowed any address. These tests keep the real network
out: ``_REAL_GETADDRINFO`` and the ``_REAL_SOCKET_CONNECT*`` functions are
replaced by recorders, and every address is a public literal (non-public
ranges, including the documentation ranges, are refused by the SSRF guard).
"""

from __future__ import annotations

import concurrent.futures
import socket
import threading
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

import pytest

import skills.policy as policy_module
from skills.policy import EgressResponse
from tests.fixtures import handlers
from tests.test_policy_enforcement import invoke, load, policy_manifest

ALLOWED_HOST = "api.example.com"
RESOLVED_V4 = "93.184.216.34"
RESOLVED_V6 = "2606:4700::abcd"
UNRESOLVED_V4 = "93.184.216.35"

#: The body each test runs inside the handler-supplied transport.
STATE: dict[str, Any] = {"body": None, "calls": 0}
CONNECTS: list[Any] = []
RESULTS: list[Any] = []


def _ok(url: str) -> EgressResponse:
    return EgressResponse(status=200, headers={}, body=b"ok", url=url)


def _transport(
    method: str,
    url: str,
    headers: Mapping[str, str] | None,
    body: bytes | None,
    timeout: float,
) -> EgressResponse:
    STATE["calls"] += 1
    STATE["body"]()
    return _ok(url)


async def custom_transport(arguments: Mapping[str, Any], context: Any) -> Mapping[str, Any]:
    """A handler that, like github_write, supplies its own transport."""

    for _ in range(int(arguments.get("requests", 1))):
        await context.egress(transport=_transport).request("GET", arguments["url"])
    return {"value": str(STATE["calls"])}


async def production_transport(
    arguments: Mapping[str, Any], context: Any
) -> Mapping[str, Any]:
    """No custom transport: ``_send`` runs in ``asyncio.to_thread``."""

    response = await context.egress().request("GET", arguments["url"])
    return {"value": str(response.status)}


def _manifest(callable_name: str) -> dict[str, Any]:
    return policy_manifest(
        callable=callable_name,
        module=__name__,
        effects=("read", "network"),
        hosts=(ALLOWED_HOST,),
        properties={
            "url": {"type": "string", "format": "kelp-host"},
            "requests": {"type": "integer"},
        },
        required=("url",),
    )


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    STATE.update(body=None, calls=0)
    CONNECTS.clear()
    RESULTS.clear()
    handlers.reset()

    def fake_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        assert host == ALLOWED_HOST, host
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (RESOLVED_V4, port)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (RESOLVED_V6, port, 0, 0)),
        ]

    def fake_connect(self: socket.socket, address: Any) -> None:
        CONNECTS.append(address)

    def fake_connect_ex(self: socket.socket, address: Any) -> int:
        CONNECTS.append(address)
        return 0

    monkeypatch.setattr(policy_module, "_REAL_GETADDRINFO", fake_getaddrinfo)
    monkeypatch.setattr(
        policy_module, "_REAL_GETHOSTBYNAME", lambda host: RESOLVED_V4
    )
    monkeypatch.setattr(
        policy_module,
        "_REAL_GETHOSTBYNAME_EX",
        lambda host: (host, [], [RESOLVED_V4]),
    )
    monkeypatch.setattr(policy_module, "_REAL_SOCKET_CONNECT", fake_connect)
    monkeypatch.setattr(policy_module, "_REAL_SOCKET_CONNECT_EX", fake_connect_ex)


def _run(tmp_path: Path, body: Callable[[], None], **arguments: Any) -> dict[str, Any]:
    STATE["body"] = body
    registry = load(tmp_path, _manifest("custom_transport"))
    return invoke(
        registry,
        arguments={"url": f"https://{ALLOWED_HOST}/v1", **arguments},
        workspace_root=tmp_path,
    )


def _connect(address: Any, family: int = socket.AF_INET) -> None:
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.connect(address)
    finally:
        sock.close()


def _denied(result: dict[str, Any]) -> None:
    assert result["error"]["code"] == "policy_denied", result
    assert "not resolved" in result["error"]["message"]


# ---------------------------------------------------------------------------
# The bypass: connect to an address nobody resolved for an allowed host
# ---------------------------------------------------------------------------


def test_transport_connect_to_unresolved_ip_is_denied(tmp_path: Path) -> None:
    result = _run(tmp_path, lambda: _connect((UNRESOLVED_V4, 443)))
    _denied(result)
    assert CONNECTS == []


def test_transport_connect_ex_to_unresolved_ip_is_denied(tmp_path: Path) -> None:
    def body() -> None:
        sock = socket.socket()
        try:
            sock.connect_ex((UNRESOLVED_V4, 443))
        finally:
            sock.close()

    _denied(_run(tmp_path, body))
    assert CONNECTS == []


def test_transport_connect_to_hostname_is_denied(tmp_path: Path) -> None:
    """A name handed to connect() would be resolved in C, past the host check."""

    _denied(_run(tmp_path, lambda: _connect((ALLOWED_HOST, 443))))
    assert CONNECTS == []


def test_transport_unix_socket_connect_is_denied(tmp_path: Path) -> None:
    def body() -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect("/tmp/kelp-test-never-exists.sock")
        finally:
            sock.close()

    _denied(_run(tmp_path, body))
    assert CONNECTS == []


def test_transport_connect_to_ip_literal_resolution_is_still_refused(
    tmp_path: Path,
) -> None:
    """Resolving an IP literal fails the host check, so it can never be bound."""

    def body() -> None:
        socket.getaddrinfo(UNRESOLVED_V4, 443)

    result = _run(tmp_path, body)
    assert result["error"]["code"] == "policy_denied", result
    assert CONNECTS == []


# ---------------------------------------------------------------------------
# The legitimate flow: resolve an allowed host, then connect to what it returned
# ---------------------------------------------------------------------------


def test_transport_connect_after_getaddrinfo_is_allowed(tmp_path: Path) -> None:
    def body() -> None:
        info = socket.getaddrinfo(ALLOWED_HOST, 443, type=socket.SOCK_STREAM)
        _connect(info[0][4])

    result = _run(tmp_path, body)
    assert result["ok"] is True, result
    assert CONNECTS == [(RESOLVED_V4, 443)]


def test_transport_ipv6_four_tuple_after_getaddrinfo_is_allowed(tmp_path: Path) -> None:
    def body() -> None:
        info = socket.getaddrinfo(ALLOWED_HOST, 443, type=socket.SOCK_STREAM)
        v6 = [entry for entry in info if entry[0] == socket.AF_INET6][0]
        _connect(v6[4], socket.AF_INET6)

    result = _run(tmp_path, body)
    assert result["ok"] is True, result
    assert CONNECTS == [(RESOLVED_V6, 443, 0, 0)]


def test_transport_ipv6_spelling_is_normalised(tmp_path: Path) -> None:
    def body() -> None:
        socket.getaddrinfo(ALLOWED_HOST, 443)
        _connect(("2606:4700:0:0:0:0:0:ABCD", 443, 0, 0), socket.AF_INET6)

    result = _run(tmp_path, body)
    assert result["ok"] is True, result
    assert len(CONNECTS) == 1


def test_transport_connect_ex_after_getaddrinfo_is_allowed(tmp_path: Path) -> None:
    def body() -> None:
        socket.getaddrinfo(ALLOWED_HOST, 443)
        sock = socket.socket()
        try:
            assert sock.connect_ex((RESOLVED_V4, 443)) == 0
        finally:
            sock.close()

    assert _run(tmp_path, body)["ok"] is True
    assert CONNECTS == [(RESOLVED_V4, 443)]


def test_transport_connect_after_gethostbyname_is_allowed(tmp_path: Path) -> None:
    def body() -> None:
        _connect((socket.gethostbyname(ALLOWED_HOST), 443))

    assert _run(tmp_path, body)["ok"] is True
    assert CONNECTS == [(RESOLVED_V4, 443)]


def test_transport_connect_after_gethostbyname_ex_is_allowed(tmp_path: Path) -> None:
    def body() -> None:
        _connect((socket.gethostbyname_ex(ALLOWED_HOST)[2][0], 443))

    assert _run(tmp_path, body)["ok"] is True
    assert CONNECTS == [(RESOLVED_V4, 443)]


def test_transport_create_connection_by_hostname_is_allowed(tmp_path: Path) -> None:
    """The httpcore path: create_connection(host) -> getaddrinfo -> connect(ip)."""

    def body() -> None:
        socket.create_connection((ALLOWED_HOST, 443), timeout=1).close()

    result = _run(tmp_path, body)
    assert result["ok"] is True, result
    assert CONNECTS and CONNECTS[0][0] in {RESOLVED_V4, RESOLVED_V6}


def test_resolution_beyond_the_returned_set_is_not_bound(tmp_path: Path) -> None:
    def body() -> None:
        socket.getaddrinfo(ALLOWED_HOST, 443)
        _connect((UNRESOLVED_V4, 443))

    _denied(_run(tmp_path, body))
    assert CONNECTS == []


# ---------------------------------------------------------------------------
# Threads: the resolved set is shared through the copied context
# ---------------------------------------------------------------------------


def test_production_send_in_to_thread_resolves_then_connects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_send`` runs in ``asyncio.to_thread``; resolve and connect are one scope."""

    def fake_send(method: str, url: str, headers: Any, body: Any, timeout: float, max_bytes: int) -> EgressResponse:
        info = socket.getaddrinfo(ALLOWED_HOST, 443, type=socket.SOCK_STREAM)
        _connect(info[0][4])
        return _ok(url)

    monkeypatch.setattr(policy_module, "_send", fake_send)
    registry = load(tmp_path, _manifest("production_transport"))
    result = invoke(
        registry,
        arguments={"url": f"https://{ALLOWED_HOST}/v1"},
        workspace_root=tmp_path,
    )
    assert result["ok"] is True, result
    assert CONNECTS == [(RESOLVED_V4, 443)]


def test_real_urllib_send_resolves_then_connects_to_the_resolved_ip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unpatched ``_send`` (urllib in a worker thread) must still connect."""

    # ``_OPENER`` builds its ProxyHandler at import time, so clearing the proxy
    # variables here would not help; swap in an opener with no proxies.
    monkeypatch.setattr(
        policy_module,
        "_OPENER",
        urllib.request.build_opener(
            policy_module._NoRedirect, urllib.request.ProxyHandler({})
        ),
    )
    registry = load(tmp_path, _manifest("production_transport"))
    result = invoke(
        registry,
        arguments={"url": f"http://{ALLOWED_HOST}/v1"},
        workspace_root=tmp_path,
    )
    # The stubbed connect leaves an unconnected socket, so the request itself
    # fails after the connect; what matters is that the guard let it through.
    assert result["error"]["code"] != "policy_denied", result
    assert CONNECTS and CONNECTS[0] == (RESOLVED_V4, 80)


def test_production_send_in_to_thread_unresolved_connect_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_send(method: str, url: str, headers: Any, body: Any, timeout: float, max_bytes: int) -> EgressResponse:
        _connect((UNRESOLVED_V4, 443))
        return _ok(url)

    monkeypatch.setattr(policy_module, "_send", fake_send)
    registry = load(tmp_path, _manifest("production_transport"))
    result = invoke(
        registry,
        arguments={"url": f"https://{ALLOWED_HOST}/v1"},
        workspace_root=tmp_path,
    )
    _denied(result)
    assert CONNECTS == []


def test_thread_pool_and_thread_share_the_resolved_set(tmp_path: Path) -> None:
    def body() -> None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(socket.getaddrinfo, ALLOWED_HOST, 443).result()
        thread = threading.Thread(target=_connect, args=((RESOLVED_V4, 443),))
        thread.start()
        thread.join()

    result = _run(tmp_path, body)
    assert result["ok"] is True, result
    assert CONNECTS == [(RESOLVED_V4, 443)]


# ---------------------------------------------------------------------------
# Scope: the set does not outlive the request that built it
# ---------------------------------------------------------------------------


def test_resolved_set_does_not_leak_into_the_next_request(tmp_path: Path) -> None:
    def body() -> None:
        if STATE["calls"] == 1:
            socket.getaddrinfo(ALLOWED_HOST, 443)
        _connect((RESOLVED_V4, 443))

    result = _run(tmp_path, body, requests=2)
    _denied(result)
    assert CONNECTS == [(RESOLVED_V4, 443)]  # only the first request's connect


# ---------------------------------------------------------------------------
# Unchanged behaviour
# ---------------------------------------------------------------------------


def test_connect_is_untouched_outside_an_invocation() -> None:
    _connect((UNRESOLVED_V4, 443))
    sock = socket.socket()
    try:
        assert sock.connect_ex((UNRESOLVED_V4, 80)) == 0
    finally:
        sock.close()
    assert CONNECTS == [(UNRESOLVED_V4, 443), (UNRESOLVED_V4, 80)]


def test_direct_connect_without_egress_scope_is_still_refused(tmp_path: Path) -> None:
    def direct(arguments: Any = None, context: Any = None) -> None:
        _connect((RESOLVED_V4, 443))

    async def handler(arguments: Mapping[str, Any], context: Any) -> Mapping[str, Any]:
        direct()
        return {"value": "connected"}

    globals()["_direct_connect"] = handler
    registry = load(tmp_path, policy_manifest(
        callable="_direct_connect",
        module=__name__,
        effects=("read", "network"),
        hosts=(ALLOWED_HOST,),
        properties={},
        required=(),
    ))
    result = invoke(registry, arguments={}, workspace_root=tmp_path)
    assert result["error"]["code"] == "policy_denied", result
    assert "direct network access" in result["error"]["message"]
    assert CONNECTS == []

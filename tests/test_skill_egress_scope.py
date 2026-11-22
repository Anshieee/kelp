"""Egress scope hardening: non-public addresses, stale scopes, real httpx.

Three properties of the socket guard in :mod:`skills.policy`:

* a host on the allowlist that *resolves* to a non-public address (loopback,
  RFC 1918, link-local/metadata, CGNAT, ...) is refused, so DNS cannot be used
  to reach internal services (SSRF);
* the per-request egress scope ends when ``EgressClient.request`` returns, so a
  thread started inside a transport cannot use it afterwards;
* a real ``httpx`` client driven through ``context.egress(transport=...)``
  works end to end against a local server when the address predicate allows it.

Tests in the first two groups stub ``_REAL_GETADDRINFO`` and the connect
functions, so nothing leaves the process. The last group opens real loopback
sockets to a server it starts itself.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator, Mapping

import pytest

import skills.policy as policy_module
from openshell.errors import PolicyDenied
from openshell.policy import PolicyConfig
from skills.httpx_transport import request as httpx_request
from skills.policy import (
    EgressResponse,
    GuardedSkillContext,
    InvocationPolicy,
    active_invocation,
)
from tests.fixtures import handlers
from tests.test_policy_enforcement import invoke, load, policy_manifest

ALLOWED_HOST = "api.example.com"
PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:4700::1111"

NON_PUBLIC = [
    "127.0.0.1",
    "10.0.0.5",
    "169.254.169.254",
    "100.64.0.1",
    "::1",
    "fe80::1",
    "::ffff:127.0.0.1",
    "::ffff:169.254.169.254",
    "0.0.0.0",
]

#: Addresses the stubbed resolver returns for ``ALLOWED_HOST`` in this test.
ADDRS: list[str] = []
CONNECTS: list[Any] = []
CAUGHT: list[Any] = []
STATE: dict[str, Any] = {}


def _ok(url: str) -> EgressResponse:
    return EgressResponse(status=200, headers={}, body=b"ok", url=url)


def _sockaddr(address: str, port: Any) -> tuple[Any, ...]:
    return (address, port, 0, 0) if ":" in address else (address, port)


def _family(address: str) -> int:
    return socket.AF_INET6 if ":" in address else socket.AF_INET


def _connect(address: str, port: int = 443) -> None:
    sock = socket.socket(_family(address), socket.SOCK_STREAM)
    try:
        sock.connect(_sockaddr(address, port))
    finally:
        sock.close()


def _attempt(call: Callable[[], Any]) -> BaseException | None:
    """Return the exception ``call`` raised, or ``None``."""

    try:
        call()
    except BaseException as exc:  # noqa: BLE001 - the test inspects it
        return exc
    return None


@pytest.fixture
def stub_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve ``ALLOWED_HOST`` to ``ADDRS`` and record connects; no network."""

    ADDRS.clear()
    CONNECTS.clear()
    CAUGHT.clear()
    STATE.clear()
    handlers.reset()

    def fake_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        assert host == ALLOWED_HOST, host
        return [
            (_family(a), socket.SOCK_STREAM, 6, "", _sockaddr(a, port)) for a in ADDRS
        ]

    def fake_connect(self: socket.socket, address: Any) -> None:
        CONNECTS.append(address)

    def fake_connect_ex(self: socket.socket, address: Any) -> int:
        CONNECTS.append(address)
        return 0

    monkeypatch.setattr(policy_module, "_REAL_GETADDRINFO", fake_getaddrinfo)
    monkeypatch.setattr(policy_module, "_REAL_GETHOSTBYNAME", lambda host: ADDRS[0])
    monkeypatch.setattr(
        policy_module, "_REAL_GETHOSTBYNAME_EX", lambda host: (host, [], list(ADDRS))
    )
    monkeypatch.setattr(policy_module, "_REAL_SOCKET_CONNECT", fake_connect)
    monkeypatch.setattr(policy_module, "_REAL_SOCKET_CONNECT_EX", fake_connect_ex)


async def probe(arguments: Mapping[str, Any], context: Any) -> Mapping[str, Any]:
    """Run ``STATE['body']`` inside a handler-supplied transport."""

    def transport(
        method: str,
        url: str,
        headers: Mapping[str, str] | None,
        body: bytes | None,
        timeout: float,
    ) -> EgressResponse:
        STATE["body"]()
        return _ok(url)

    for _ in range(int(arguments.get("requests", 1))):
        await context.egress(transport=transport).request("GET", arguments["url"])
    return {"value": "done"}


async def stale_thread(arguments: Mapping[str, Any], context: Any) -> Mapping[str, Any]:
    """Start a thread inside a request that only acts after the request ends."""

    release = threading.Event()

    def worker() -> None:
        if not release.wait(timeout=10):
            CAUGHT.append(("timeout", TimeoutError("never released")))
            return
        CAUGHT.append(
            ("getaddrinfo", _attempt(lambda: socket.getaddrinfo(ALLOWED_HOST, 443)))
        )
        CAUGHT.append(("connect", _attempt(lambda: _connect(PUBLIC_V4))))

    def transport(
        method: str,
        url: str,
        headers: Mapping[str, str] | None,
        body: bytes | None,
        timeout: float,
    ) -> EgressResponse:
        socket.getaddrinfo(ALLOWED_HOST, 443)  # resolves PUBLIC_V4 in scope
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        STATE["thread"] = thread
        return _ok(url)

    await context.egress(transport=transport).request("GET", arguments["url"])
    release.set()
    STATE["thread"].join(timeout=10)
    STATE["joined"] = not STATE["thread"].is_alive()
    return {"value": "done"}


async def scope_less(arguments: Mapping[str, Any], context: Any) -> Mapping[str, Any]:
    """Egress flag set with no scope at all, as a stale context would have."""

    token = policy_module._EGRESS_ALLOWED.set(True)
    try:
        CAUGHT.append(
            ("getaddrinfo", _attempt(lambda: socket.getaddrinfo(ALLOWED_HOST, 443)))
        )
        CAUGHT.append(("connect", _attempt(lambda: _connect(PUBLIC_V4))))
    finally:
        policy_module._EGRESS_ALLOWED.reset(token)
    return {"value": "done"}


async def loopback_fetch(arguments: Mapping[str, Any], context: Any) -> Mapping[str, Any]:
    """Mirror ``github_write``: a sync httpx call as the egress transport."""

    port = STATE["port"]

    def transport(
        method: str,
        url: str,
        headers: Mapping[str, str] | None,
        body: bytes | None,
        timeout: float,
    ) -> EgressResponse:
        # The egress policy only admits ports 80/443, so the request is checked
        # as ``http://localhost/...`` and the transport dials the test server.
        assert url == "http://localhost/ping", url
        return httpx_request(
            method, f"http://localhost:{port}/ping", headers, body, timeout
        )

    response = await context.egress(transport=transport).request("GET", arguments["url"])
    return {"value": response.body.decode("utf-8")}


def _manifest(callable_name: str, host: str = ALLOWED_HOST) -> dict[str, Any]:
    return policy_manifest(
        callable=callable_name,
        module=__name__,
        effects=("read", "network"),
        hosts=(host,),
        properties={
            "url": {"type": "string", "format": "kelp-host"},
            "requests": {"type": "integer"},
        },
        required=("url",),
    )


def _run(
    tmp_path: Path,
    callable_name: str = "probe",
    body: Callable[[], None] | None = None,
    *,
    host: str = ALLOWED_HOST,
    url: str | None = None,
    **arguments: Any,
) -> dict[str, Any]:
    STATE["body"] = body
    registry = load(tmp_path, _manifest(callable_name, host))
    return invoke(
        registry,
        arguments={"url": url or f"https://{host}/v1", **arguments},
        workspace_root=tmp_path,
    )


def _lookup_getaddrinfo(host: str) -> Any:
    return socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)


LOOKUPS: dict[str, Callable[[str], Any]] = {
    "getaddrinfo": _lookup_getaddrinfo,
    "gethostbyname": socket.gethostbyname,
    "gethostbyname_ex": socket.gethostbyname_ex,
}


def _lookup_cases(addresses: list[str]) -> list[Any]:
    """``(lookup, address)`` pairs; the IPv4-only lookups skip IPv6 text."""

    return [
        pytest.param(name, address, id=f"{name}-{address}")
        for address in addresses
        for name in LOOKUPS
        if name == "getaddrinfo" or ":" not in address
    ]


# ---------------------------------------------------------------------------
# Item 1: an allowlisted host must not reach a non-public address
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("lookup", "address"), _lookup_cases(NON_PUBLIC))
def test_allowlisted_host_resolving_to_non_public_address_is_refused(
    stub_dns: None, tmp_path: Path, lookup: str, address: str
) -> None:
    ADDRS[:] = [address]

    def body() -> None:
        CAUGHT.append(_attempt(lambda: LOOKUPS[lookup](ALLOWED_HOST)))
        # Nothing was recorded, so connecting to that very address is refused.
        CAUGHT.append(_attempt(lambda: _connect(address)))

    result = _run(tmp_path, body=body)
    assert result["ok"] is True, result
    lookup_error, connect_error = CAUGHT
    assert isinstance(lookup_error, PolicyDenied), lookup_error
    assert lookup_error.blast_radius == (
        f"net:{ALLOWED_HOST}",
        "egress:non_public_address",
    )
    assert isinstance(connect_error, PolicyDenied), connect_error
    assert connect_error.blast_radius == ("net:unresolved", "egress:unresolved")
    assert CONNECTS == []


@pytest.mark.parametrize(("lookup", "address"), _lookup_cases([PUBLIC_V4, PUBLIC_V6]))
def test_allowlisted_host_resolving_to_public_address_is_allowed(
    stub_dns: None, tmp_path: Path, lookup: str, address: str
) -> None:
    ADDRS[:] = [address]

    def body() -> None:
        LOOKUPS[lookup](ALLOWED_HOST)
        _connect(address)

    result = _run(tmp_path, body=body)
    assert result["ok"] is True, result
    assert CONNECTS == [_sockaddr(address, 443)]


@pytest.mark.parametrize("private", ["127.0.0.1", "10.0.0.5", "::1"])
def test_mixed_public_and_private_answer_is_refused_and_records_nothing(
    stub_dns: None, tmp_path: Path, private: str
) -> None:
    ADDRS[:] = [PUBLIC_V4, private]

    def body() -> None:
        CAUGHT.append(_attempt(lambda: _lookup_getaddrinfo(ALLOWED_HOST)))
        CAUGHT.append(_attempt(lambda: _connect(PUBLIC_V4)))  # not even the public one

    assert _run(tmp_path, body=body)["ok"] is True
    lookup_error, connect_error = CAUGHT
    assert isinstance(lookup_error, PolicyDenied), lookup_error
    assert lookup_error.blast_radius[-1] == "egress:non_public_address"
    assert isinstance(connect_error, PolicyDenied), connect_error
    assert CONNECTS == []


def test_non_public_refusal_surfaces_as_a_policy_denied_envelope(
    stub_dns: None, tmp_path: Path
) -> None:
    ADDRS[:] = ["10.0.0.5"]
    result = _run(tmp_path, body=lambda: _lookup_getaddrinfo(ALLOWED_HOST))
    assert result["error"]["code"] == "policy_denied", result
    assert "egress:non_public_address" in result["error"]["details"]["blast_radius"]
    assert CONNECTS == []


def test_lookup_outside_an_invocation_is_not_filtered(stub_dns: None) -> None:
    ADDRS[:] = ["127.0.0.1"]
    assert _lookup_getaddrinfo(ALLOWED_HOST)[0][4][0] == "127.0.0.1"
    assert socket.gethostbyname(ALLOWED_HOST) == "127.0.0.1"
    assert socket.gethostbyname_ex(ALLOWED_HOST)[2] == ["127.0.0.1"]


def test_address_predicate_is_the_single_override_point(
    stub_dns: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ADDRS[:] = ["127.0.0.1"]
    monkeypatch.setattr(policy_module, "_address_allowed", lambda ip: ip == "127.0.0.1")

    def body() -> None:
        _lookup_getaddrinfo(ALLOWED_HOST)
        _connect("127.0.0.1")

    assert _run(tmp_path, body=body)["ok"] is True
    assert CONNECTS == [("127.0.0.1", 443)]


@pytest.mark.parametrize("address", NON_PUBLIC + ["fe80::1%eth0", "not-an-ip", ""])
def test_address_predicate_refuses_non_public(address: str) -> None:
    assert policy_module._address_allowed(address) is False


@pytest.mark.parametrize(
    "address", [PUBLIC_V4, PUBLIC_V6, "8.8.8.8", "::ffff:93.184.216.34"]
)
def test_address_predicate_allows_public(address: str) -> None:
    assert policy_module._address_allowed(address) is True


# ---------------------------------------------------------------------------
# Item 2: the scope ends when request() returns
# ---------------------------------------------------------------------------


def test_thread_started_in_a_request_cannot_use_the_scope_after_it_returns(
    stub_dns: None, tmp_path: Path
) -> None:
    ADDRS[:] = [PUBLIC_V4]
    result = _run(tmp_path, "stale_thread")
    assert result["ok"] is True, result
    assert STATE["joined"] is True
    assert [name for name, _ in CAUGHT] == ["getaddrinfo", "connect"], CAUGHT
    for name, error in CAUGHT:
        assert isinstance(error, PolicyDenied), (name, error)
        assert error.blast_radius == ("net:direct", "egress:scope_closed"), name
        assert "scope has ended" in error.reason
    # The in-request resolution had recorded PUBLIC_V4; it is no longer usable.
    assert CONNECTS == []


def test_egress_allowed_with_no_scope_is_refused(stub_dns: None, tmp_path: Path) -> None:
    ADDRS[:] = [PUBLIC_V4]
    assert _run(tmp_path, "scope_less")["ok"] is True
    assert [name for name, _ in CAUGHT] == ["getaddrinfo", "connect"], CAUGHT
    for name, error in CAUGHT:
        assert isinstance(error, PolicyDenied), (name, error)
        assert error.blast_radius == ("net:direct", "egress:scope_closed"), name
    assert CONNECTS == []


def test_each_request_opens_a_fresh_scope(stub_dns: None, tmp_path: Path) -> None:
    ADDRS[:] = [PUBLIC_V4]

    def body() -> None:
        _lookup_getaddrinfo(ALLOWED_HOST)
        _connect(PUBLIC_V4)

    result = _run(tmp_path, body=body, requests=2)
    assert result["ok"] is True, result
    assert CONNECTS == [(PUBLIC_V4, 443), (PUBLIC_V4, 443)]


# ---------------------------------------------------------------------------
# Item 3: a real httpx client against a real loopback server
# ---------------------------------------------------------------------------

PROXY_VARS = (
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)
LOOPBACK_BODY = b"hello-from-loopback"
HITS: list[str] = []


class _PingHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        HITS.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(LOOPBACK_BODY)))
        self.end_headers()
        self.wfile.write(LOOPBACK_BODY)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


@pytest.fixture
def loopback_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    """Serve on 127.0.0.1 from a thread started outside any invocation."""

    for name in PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
    HITS.clear()
    STATE.clear()
    handlers.reset()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PingHandler)
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    STATE["port"] = server.server_address[1]
    try:
        yield STATE["port"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _loopback_only(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip.partition("%")[0]).is_loopback
    except ValueError:
        return False


def _fetch_loopback(tmp_path: Path) -> Mapping[str, Any]:
    """Run ``loopback_fetch`` under a policy that allows only host ``localhost``.

    The manifest schema rejects the bare label ``localhost`` as a host, so the
    policy and context are built directly. The guard, ``EgressClient`` and
    ``httpx`` are the production objects.
    """

    policy = InvocationPolicy(
        skill="demo.loopback",
        version="0.1.0",
        effects=frozenset({"read", "network"}),
        paths=(),
        hosts=("localhost",),
        repos=(),
        config=PolicyConfig(workspace_root=tmp_path),
    )
    context = GuardedSkillContext(
        SimpleNamespace(skill="demo.loopback", workspace_root=tmp_path), policy
    )
    with active_invocation(policy, tool="demo.loopback"):
        return asyncio.run(loopback_fetch({"url": "http://localhost/ping"}, context))


def test_real_httpx_reaches_a_loopback_server_when_the_predicate_allows_it(
    loopback_server: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(policy_module, "_address_allowed", _loopback_only)
    assert _fetch_loopback(tmp_path) == {"value": LOOPBACK_BODY.decode()}
    assert HITS == ["/ping"]


def test_real_httpx_to_loopback_is_refused_by_default(
    loopback_server: int, tmp_path: Path
) -> None:
    with pytest.raises(PolicyDenied) as raised:
        _fetch_loopback(tmp_path)
    assert raised.value.blast_radius == ("net:localhost", "egress:non_public_address")
    assert HITS == []


# ---------------------------------------------------------------------------
# Datagram sends follow the same binding as connect
# ---------------------------------------------------------------------------


SENDS: list[Any] = []


@pytest.fixture
def stub_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    """Record datagram sends instead of putting them on the wire."""

    SENDS.clear()

    def fake_sendto(self: socket.socket, data: Any, *args: Any) -> int:
        SENDS.append(("sendto", args[-1] if args else None))
        return len(data)

    def fake_sendmsg(
        self: socket.socket,
        buffers: Any,
        ancdata: Any = (),
        flags: int = 0,
        address: Any = None,
    ) -> int:
        SENDS.append(("sendmsg", address))
        return 0

    monkeypatch.setattr(policy_module, "_REAL_SOCKET_SENDTO", fake_sendto)
    monkeypatch.setattr(policy_module, "_REAL_SOCKET_SENDMSG", fake_sendmsg)


def _udp(call: Callable[[socket.socket], Any]) -> BaseException | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        return _attempt(lambda: call(sock))
    finally:
        sock.close()


DATAGRAM_SENDS: dict[str, Callable[[socket.socket, Any], Any]] = {
    "sendto": lambda sock, address: sock.sendto(b"x", address),
    "sendmsg": lambda sock, address: sock.sendmsg([b"x"], [], 0, address),
}


@pytest.mark.parametrize("send", list(DATAGRAM_SENDS))
def test_datagram_to_an_allowlisted_hostname_is_refused(
    stub_dns: None, stub_sends: None, tmp_path: Path, send: str
) -> None:
    # A hostname would be resolved inside CPython, bypassing the guarded
    # lookup and the non-public check, so it is refused outright.
    ADDRS[:] = ["169.254.169.254"]

    def body() -> None:
        CAUGHT.append(
            _udp(lambda sock: DATAGRAM_SENDS[send](sock, (ALLOWED_HOST, 53)))
        )

    assert _run(tmp_path, body=body)["ok"] is True
    (error,) = CAUGHT
    assert isinstance(error, PolicyDenied), error
    assert error.blast_radius == ("net:unresolved", "egress:unresolved")
    assert SENDS == []


@pytest.mark.parametrize("send", list(DATAGRAM_SENDS))
def test_datagram_to_a_resolved_public_ip_is_allowed(
    stub_dns: None, stub_sends: None, tmp_path: Path, send: str
) -> None:
    ADDRS[:] = [PUBLIC_V4]

    def body() -> None:
        _lookup_getaddrinfo(ALLOWED_HOST)
        CAUGHT.append(_udp(lambda sock: DATAGRAM_SENDS[send](sock, (PUBLIC_V4, 443))))

    assert _run(tmp_path, body=body)["ok"] is True
    assert CAUGHT == [None]
    assert SENDS == [(send, (PUBLIC_V4, 443))]


async def scope_less_datagram(
    arguments: Mapping[str, Any], context: Any
) -> Mapping[str, Any]:
    """Address-less datagram sends with the egress flag but no live scope."""

    token = policy_module._EGRESS_ALLOWED.set(True)
    try:
        CAUGHT.append(("sendto", _udp(lambda sock: sock.sendto(b"x"))))  # type: ignore[call-overload]
        CAUGHT.append(("sendmsg", _udp(lambda sock: sock.sendmsg([b"x"]))))
    finally:
        policy_module._EGRESS_ALLOWED.reset(token)
    return {"value": "done"}


def test_address_less_datagram_without_a_live_scope_is_refused(
    stub_dns: None, stub_sends: None, tmp_path: Path
) -> None:
    assert _run(tmp_path, "scope_less_datagram")["ok"] is True
    assert [name for name, _ in CAUGHT] == ["sendto", "sendmsg"], CAUGHT
    for name, error in CAUGHT:
        assert isinstance(error, PolicyDenied), (name, error)
        assert error.blast_radius == ("net:direct", "egress:scope_closed"), name
    assert SENDS == []


def test_sendto_with_flags_checks_the_address_argument(
    stub_dns: None, stub_sends: None, tmp_path: Path
) -> None:
    ADDRS[:] = [PUBLIC_V4]

    def body() -> None:
        _lookup_getaddrinfo(ALLOWED_HOST)
        CAUGHT.append(_udp(lambda sock: sock.sendto(b"x", 0, (PUBLIC_V4, 443))))
        CAUGHT.append(_udp(lambda sock: sock.sendto(b"x", 0, (ALLOWED_HOST, 443))))

    assert _run(tmp_path, body=body)["ok"] is True
    allowed, refused = CAUGHT
    assert allowed is None
    assert isinstance(refused, PolicyDenied), refused
    assert refused.blast_radius == ("net:unresolved", "egress:unresolved")
    assert SENDS == [("sendto", (PUBLIC_V4, 443))]


def test_datagram_outside_an_invocation_is_not_checked(stub_sends: None) -> None:
    assert _udp(lambda sock: sock.sendto(b"x", ("example.invalid", 9))) is None
    assert SENDS == [("sendto", ("example.invalid", 9))]

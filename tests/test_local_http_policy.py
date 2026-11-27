"""L1 header policy: exactly one canonical Host, matching Origin, owner bearer.

These tests exercise a pure validator over an ``email.message.Message`` (the
``http.client.HTTPMessage`` base). No socket, provider, or desktop code is
involved. Every malformed, duplicated, or missing value must fail closed with
one fixed ``LocalHttpRejection`` value, and no rejection carries a secret.
"""

from __future__ import annotations

from email.message import Message

import pytest

from chat.local_http import LocalHttpRejection, check_local_headers

PORT = 43123
BEARER = "test-owner-bearer-0123456789"
HOST_IP = f"127.0.0.1:{PORT}"
HOST_LOCAL = f"localhost:{PORT}"
ORIGIN_IP = f"http://{HOST_IP}"


def _headers(*pairs: tuple[str, str]) -> Message:
    message = Message()
    for name, value in pairs:
        message[name] = value
    return message


def _valid() -> Message:
    return _headers(
        ("Host", HOST_IP),
        ("Origin", ORIGIN_IP),
        ("Authorization", f"Bearer {BEARER}"),
    )


@pytest.mark.parametrize("host", [HOST_IP, HOST_LOCAL])
def test_accepts_exactly_one_canonical_loopback_host(host: str) -> None:
    message = _headers(
        ("Host", host),
        ("Origin", f"http://{host}"),
        ("Authorization", f"Bearer {BEARER}"),
    )
    assert check_local_headers(message, port=PORT, owner_bearer=BEARER) is None


def test_header_names_are_case_insensitive() -> None:
    message = _headers(
        ("hOsT", HOST_IP),
        ("oRiGiN", ORIGIN_IP),
        ("aUtHoRiZaTiOn", f"Bearer {BEARER}"),
    )
    assert check_local_headers(message, port=PORT, owner_bearer=BEARER) is None


def test_rejects_missing_host() -> None:
    message = _headers(("Origin", ORIGIN_IP), ("Authorization", f"Bearer {BEARER}"))
    assert (
        check_local_headers(message, port=PORT, owner_bearer=BEARER)
        is LocalHttpRejection.INVALID_HOST
    )


def test_rejects_duplicate_host() -> None:
    message = _headers(
        ("Host", HOST_IP),
        ("Host", HOST_LOCAL),
        ("Origin", ORIGIN_IP),
        ("Authorization", f"Bearer {BEARER}"),
    )
    assert (
        check_local_headers(message, port=PORT, owner_bearer=BEARER)
        is LocalHttpRejection.INVALID_HOST
    )


@pytest.mark.parametrize(
    "host",
    [
        f"127.0.0.1:{PORT + 1}",
        f"LOCALHOST:{PORT}",
        f"127.0.0.1:{PORT}, http://evil",
        f"127.0.0.1:{PORT}.evil",
        f" 127.0.0.1:{PORT}",
    ],
)
def test_rejects_noncanonical_host_values(host: str) -> None:
    message = _headers(
        ("Host", host),
        ("Origin", ORIGIN_IP),
        ("Authorization", f"Bearer {BEARER}"),
    )
    assert (
        check_local_headers(message, port=PORT, owner_bearer=BEARER)
        is LocalHttpRejection.INVALID_HOST
    )


@pytest.mark.parametrize("port", [0, 65536, True])
def test_invalid_bound_port_fails_closed_as_invalid_host(port: object) -> None:
    assert (
        check_local_headers(_valid(), port=port, owner_bearer=BEARER)
        is LocalHttpRejection.INVALID_HOST
    )


def test_rejects_missing_origin() -> None:
    message = _headers(("Host", HOST_IP), ("Authorization", f"Bearer {BEARER}"))
    assert (
        check_local_headers(message, port=PORT, owner_bearer=BEARER)
        is LocalHttpRejection.INVALID_ORIGIN
    )


def test_rejects_duplicate_origin() -> None:
    message = _headers(
        ("Host", HOST_IP),
        ("Origin", ORIGIN_IP),
        ("Origin", "http://evil.example"),
        ("Authorization", f"Bearer {BEARER}"),
    )
    assert (
        check_local_headers(message, port=PORT, owner_bearer=BEARER)
        is LocalHttpRejection.INVALID_ORIGIN
    )


@pytest.mark.parametrize(
    "origin",
    [
        "null",
        f"https://127.0.0.1:{PORT}",
        f"http://127.0.0.1:{PORT + 1}",
        f"http://localhost:{PORT}",
        f"http://127.0.0.1:{PORT}/",
        f"http://127.0.0.1:{PORT}, http://evil",
        f" http://127.0.0.1:{PORT}",
    ],
)
def test_rejects_noncanonical_origin_values(origin: str) -> None:
    message = _headers(
        ("Host", HOST_IP),
        ("Origin", origin),
        ("Authorization", f"Bearer {BEARER}"),
    )
    assert (
        check_local_headers(message, port=PORT, owner_bearer=BEARER)
        is LocalHttpRejection.INVALID_ORIGIN
    )


def test_cookie_only_request_is_unauthenticated() -> None:
    message = _headers(
        ("Host", HOST_IP),
        ("Origin", ORIGIN_IP),
        ("Cookie", "KELP_AUTH_TOKEN=cookie-is-not-ambient-auth"),
    )
    assert (
        check_local_headers(message, port=PORT, owner_bearer=BEARER)
        is LocalHttpRejection.UNAUTHORIZED
    )


def test_rejects_duplicate_authorization() -> None:
    message = _headers(
        ("Host", HOST_IP),
        ("Origin", ORIGIN_IP),
        ("Authorization", f"Bearer {BEARER}"),
        ("Authorization", f"Bearer {BEARER}"),
    )
    assert (
        check_local_headers(message, port=PORT, owner_bearer=BEARER)
        is LocalHttpRejection.UNAUTHORIZED
    )


@pytest.mark.parametrize(
    "authorization",
    [
        "Bearer wrong-owner-bearer",
        f"Basic {BEARER}",
        f"bearer {BEARER}",
        f"Bearer  {BEARER}",
        f"Bearer {BEARER} extra",
    ],
)
def test_rejects_malformed_or_wrong_authorization(authorization: str) -> None:
    message = _headers(
        ("Host", HOST_IP),
        ("Origin", ORIGIN_IP),
        ("Authorization", authorization),
    )
    assert (
        check_local_headers(message, port=PORT, owner_bearer=BEARER)
        is LocalHttpRejection.UNAUTHORIZED
    )


def test_empty_expected_bearer_fails_closed() -> None:
    assert (
        check_local_headers(_valid(), port=PORT, owner_bearer="")
        is LocalHttpRejection.UNAUTHORIZED
    )


def test_rejection_values_are_fixed_enum_members() -> None:
    assert {rejection.name for rejection in LocalHttpRejection} == {
        "INVALID_HOST",
        "INVALID_ORIGIN",
        "UNAUTHORIZED",
    }

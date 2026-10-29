"""Block outbound sockets for an offline evaluation run.

This is the same defence used by the offline test suite, promoted to the eval
harness so a future task cannot quietly start reaching the network. Local
SQLite files and asyncio's in-process self-pipe (``socketpair``) are untouched;
only connect/name-resolution paths are replaced.
"""

from __future__ import annotations

import socket
from contextlib import contextmanager
from typing import Iterator


class OfflineViolation(RuntimeError):
    """Raised when offline evaluation code attempts network access."""


def _blocked(*_args: object, **_kwargs: object) -> None:
    raise OfflineViolation("offline evaluation attempted network access")


@contextmanager
def block_network() -> Iterator[None]:
    """Fail closed while the context is active. Always restores the originals."""

    originals: dict[tuple[object, str], object] = {}

    def patch(target: object, name: str, replacement: object) -> None:
        originals[(target, name)] = getattr(target, name)
        setattr(target, name, replacement)

    patch(socket.socket, "connect", _blocked)
    patch(socket.socket, "connect_ex", _blocked)
    patch(socket, "create_connection", _blocked)
    patch(socket, "getaddrinfo", _blocked)
    # UDP is another way out of the process; block it too.
    if hasattr(socket.socket, "sendto"):
        patch(socket.socket, "sendto", _blocked)
    if hasattr(socket.socket, "sendmsg"):
        patch(socket.socket, "sendmsg", _blocked)
    try:
        yield
    finally:
        for (target, name), original in originals.items():
            setattr(target, name, original)

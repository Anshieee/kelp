"""Linux-only local-supervisor entrypoint for the KELP sidecar.

Run exactly as::

    python -I -B -m chat.local_serve

There is no environment selector and no default human desktop path. The parent
must pass a connected ``AF_UNIX``/``SOCK_STREAM`` socket as fd 0. This module
validates it, duplicates it non-inheritably, and replaces fd 0 with
``/dev/null`` *before* importing :mod:`chat.server`. It then reads one config
frame, composes the server mapping, and starts the sealed Local runtime
(``serve(..., local_only=True)``) with a loopback/port-0 override.

The visible HTTP surface is the Local chat profile: the L1 loopback header
guard plus exact health, thread, turn, stream, and stop/retry targets. Hosted
routes, skills/tools, schedules, payments, credentials, and UI/bridge
activation stay out of this entrypoint.

Exit status is part of the contract: ``0`` is returned only after a complete
ready frame was written and the server shut down cleanly. Configuration,
protocol, and pre-ready channel failures return ``2``; startup and readiness
delivery failures return ``1``.
"""

from __future__ import annotations

import os
import select
import socket
import stat
import sys
import threading
from typing import Any

from .local_channel import (
    CONFIG_DEADLINE_SECONDS,
    CONFIG_MAX_BYTES,
    ProtocolError,
    compose_environ,
    parse_config,
    read_frame,
    send_error,
    send_ready,
)

__all__ = ["main"]

#: Bound on one watcher poll and on joining the watcher thread.
_WATCH_POLL_SECONDS = 0.1
_WATCH_JOIN_SECONDS = 2.0


class _ChannelError(Exception):
    """The parent did not hand over a usable connected socket."""


def _bootstrap_channel() -> socket.socket:
    """Validate fd 0, duplicate it non-inheritably, and blind stdin."""

    if not sys.platform.startswith("linux"):
        raise _ChannelError("linux only")
    try:
        info = os.fstat(0)
    except OSError:
        raise _ChannelError("stdin missing") from None
    if not stat.S_ISSOCK(info.st_mode):
        raise _ChannelError("stdin is not a socket")

    try:
        duplicate = os.dup(0)
    except OSError:
        raise _ChannelError("cannot duplicate stdin") from None
    # os.dup() produces a non-inheritable descriptor on Linux; keep the
    # process-boundary invariant explicit before constructing the socket.
    try:
        os.set_inheritable(duplicate, False)
    except OSError:
        os.close(duplicate)
        raise _ChannelError("cannot duplicate stdin") from None

    try:
        channel = socket.socket(fileno=duplicate)
    except BaseException:
        os.close(duplicate)
        raise

    try:
        if channel.family != socket.AF_UNIX or channel.type != socket.SOCK_STREAM:
            raise _ChannelError("stdin is not an AF_UNIX stream socket")
        try:
            channel.getpeername()
        except OSError:
            raise _ChannelError("stdin is not connected") from None

        devnull = os.open(os.devnull, os.O_RDONLY)
        try:
            os.dup2(devnull, 0)
            os.set_inheritable(0, False)
        finally:
            os.close(devnull)
    except BaseException:
        channel.close()
        raise
    return channel


class _Watcher:
    """Stop the server on parent EOF or any byte after the config frame.

    The watcher reads through its own duplicate of the channel, with an
    explicit short receive timeout. That keeps it independent of the
    ``CONFIG_DEADLINE_SECONDS`` deadline ``read_frame`` leaves on the main
    descriptor and of the two-second status write deadline, so a stalled
    parent cannot pin this thread inside ``recv``.

    The blocking poll runs *outside* the lock: a waiter must never have to win
    a reacquisition race against a tight select loop. Only the one-byte
    consume and the recorded reason are lock-protected, and both are
    effectively instantaneous.
    """

    def __init__(self, channel: socket.socket, stop: threading.Event):
        self._socket = channel.dup()
        try:
            self._socket.settimeout(_WATCH_POLL_SECONDS)
        except BaseException:
            self._socket.close()
            raise
        self._stop = stop
        self._lock = threading.Lock()
        self._reason: str | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="kelp-local-channel",
            daemon=True,
        )
        self._started = False

    def start(self) -> None:
        self._thread.start()
        self._started = True

    def reason(self) -> str | None:
        """Why the channel ended (``eof``/``unexpected_byte``/``closed``)."""

        with self._lock:
            return self._reason

    def _record(self, reason: str) -> None:
        """Record the first violation; the caller must hold ``_lock``."""

        if self._reason is None:
            self._reason = reason
        self._stop.set()

    def _consume(self) -> None:
        """Consume one readable byte or EOF; the caller must hold ``_lock``."""

        if self._reason is not None:
            return
        try:
            data = self._socket.recv(1)
        except TimeoutError:
            # Another prober consumed the byte between our select and recv.
            return
        except OSError:
            self._record("closed")
            return
        self._record("eof" if not data else "unexpected_byte")

    def intact(self) -> bool:
        """Synchronously confirm the channel is still clean before readiness."""

        with self._lock:
            if self._reason is not None:
                return False
            try:
                readable, _writable, _errors = select.select(
                    [self._socket], [], [], 0
                )
            except (OSError, ValueError):
                self._record("closed")
                return False
            if readable:
                self._consume()
            return self._reason is None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                readable, _writable, _errors = select.select(
                    [self._socket], [], [], _WATCH_POLL_SECONDS
                )
            except (OSError, ValueError):
                with self._lock:
                    self._record("closed")
                return
            if not readable:
                continue
            with self._lock:
                self._consume()
                if self._reason is not None:
                    return

    def join(self, timeout: float = _WATCH_JOIN_SECONDS) -> None:
        if self._started:
            self._thread.join(timeout=timeout)

    def close(self) -> None:
        try:
            self._socket.close()
        except OSError:
            pass


class _Status:
    """Own the single status-frame budget for the channel.

    Ready and error frames share one slot: once a frame write has started, no
    later path may write another. ``ready_sent`` only becomes true after the
    ready frame was written completely.
    """

    def __init__(self, channel: socket.socket) -> None:
        self._channel = channel
        self.nonce: str | None = None
        self._sent = False
        self.ready_sent = False

    def error(self, code: str) -> None:
        if self._sent:
            return
        self._sent = True
        try:
            send_error(self._channel, self.nonce, code)
        except ProtocolError:
            pass

    def ready(self, nonce: str, port: int) -> bool:
        if self._sent:
            return False
        # A partially written ready frame still consumes the slot: writing an
        # error afterwards would be a second frame on the same channel.
        self._sent = True
        try:
            send_ready(self._channel, nonce, port)
        except ProtocolError:
            return False
        self.ready_sent = True
        return True


def _run(channel: socket.socket) -> int:
    # Import only after the bootstrap has consumed and blinded fd 0.
    try:
        from chat.server import ServerConfig, ServerError, serve
    except BaseException:
        try:
            send_error(channel, None, "startup_failed")
        except ProtocolError:
            pass
        finally:
            try:
                channel.close()
            except OSError:
                pass
        return 1

    status = _Status(channel)
    stop = threading.Event()
    watcher: _Watcher | None = None
    pending_error: str | None = None

    try:
        payload = read_frame(
            channel,
            max_bytes=CONFIG_MAX_BYTES,
            deadline=CONFIG_DEADLINE_SECONDS,
        )
        config = parse_config(payload)
        status.nonce = config.nonce
        environ = compose_environ(os.environ, config)

        active = _Watcher(channel, stop)
        watcher = active
        active.start()

        try:
            server_config = ServerConfig.from_env(environ)
        except ServerError:
            status.error("invalid_config")
            return 2
        except Exception:  # noqa: BLE001 - never echo a boot traceback
            status.error("startup_failed")
            return 1

        if stop.is_set() or not active.intact():
            # The parent closed the channel or sent a byte after the config
            # frame. Do not bind, create state, or claim readiness.
            status.error("bad_frame")
            return 2

        def on_listening(host: str, port: int, _server: Any) -> None:
            nonlocal pending_error
            if host != "127.0.0.1" or type(port) is not int or not 1 <= port <= 65535:
                # Never claim an endpoint the listener did not bind. This is a
                # server fault, so it is ``startup_failed`` and not a protocol
                # error.
                pending_error = "startup_failed"
                stop.set()
                return
            if stop.is_set() or not active.intact():
                pending_error = "bad_frame"
                stop.set()
                return
            if not status.ready(config.nonce, port):
                # A readiness frame could not be delivered completely. Do not
                # attempt a second frame; the exit status stays non-zero.
                pending_error = "startup_failed"
                stop.set()

        try:
            result = serve(
                server_config,
                environ=environ,
                local_only=True,
                on_listening=on_listening,
                stop_event=stop,
            )
        except ServerError:
            status.error("invalid_config")
            return 2
        except Exception:  # noqa: BLE001 - fixed code only
            status.error("startup_failed")
            return 1

        if status.ready_sent:
            return result
        if pending_error is None:
            # The loop returned before readiness without a recorded server
            # fault. A channel violation explains it; anything else is an
            # unexpected startup failure.
            pending_error = (
                "bad_frame" if active.reason() is not None else "startup_failed"
            )
        status.error(pending_error)
        return 2 if pending_error == "bad_frame" else 1
    except ProtocolError as error:
        if error.nonce is not None:
            status.nonce = error.nonce
        status.error(error.code)
        return 2
    except Exception:  # noqa: BLE001 - fixed code instead of a traceback
        status.error("startup_failed")
        return 1
    finally:
        stop.set()
        try:
            channel.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        if watcher is not None:
            watcher.join()
            watcher.close()
        try:
            channel.close()
        except OSError:
            pass


def main() -> int:
    try:
        channel = _bootstrap_channel()
    except BaseException:
        # No channel exists, so stderr is the only safe fixed diagnostic.
        sys.stderr.write("kelp-local: invalid_channel\n")
        sys.stderr.flush()
        return 2
    try:
        return _run(channel)
    except BaseException:
        sys.stderr.write("kelp-local: startup_failed\n")
        sys.stderr.flush()
        return 1


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())

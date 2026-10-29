"""Structured JSON logging to stdout with request correlation (KEL-14, M2-D).

One log line is one JSON object on a single line:

    {"ts": "...Z", "level": "info", "event": "http_request",
     "request_id": "req_...", "thread_id": "...", "turn": "...",
     "tool": "...", "duration_ms": 3, "status": 200}

Design rules, in order of importance:

* **Only allow-listed scalar fields are written.** Free text that could carry a
  prompt, a request body, tool arguments, or a model answer (``text``,
  ``body``, ``prompt``, ``arguments``, ``content`` ...) is dropped, not
  truncated. A caller cannot leak payload text by passing a field name.
* **Every string value is scrubbed** with the same :class:`SecretScrubber`
  OpenShell uses for its audit trail. Known secret values are replaced exactly
  and common credential patterns are caught.
* **The request id is ambient**, carried in a :class:`contextvars.ContextVar`
  so request handling, a turn running on the event-loop thread, tool calls, and
  the usage store all stamp the same value without threading it through every
  call.

Output goes to ``sys.stdout`` by default, resolved at emit time so test
capture works. ``KELP_LOG_LEVEL`` (``debug``, ``info``, ``warning``, ``error``)
is honoured for the whole process; an unknown value falls back to ``info``
rather than silencing the log.
"""

from __future__ import annotations

import contextvars
import json
import math
import re
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

from openshell.audit import SecretScrubber

__all__ = [
    "LEVELS",
    "LOG_LEVEL_ENV",
    "configure_logging",
    "current_request_id",
    "is_valid_request_id",
    "log",
    "log_level",
    "new_request_id",
    "request_context",
    "reset_request_id",
    "set_request_id",
]

LOG_LEVEL_ENV = "KELP_LOG_LEVEL"

LEVELS: dict[str, int] = {
    "debug": 10,
    "info": 20,
    "warning": 30,
    "warn": 30,
    "error": 40,
    "critical": 50,
}
_DEFAULT_LEVEL = "info"

#: Scalar fields a log record may carry. Anything else is dropped: no prompt,
#: body, argument, or result text can reach stdout, even by mistake.
_ALLOWED_FIELDS = frozenset(
    {
        "request_id",
        "thread_id",
        "turn",
        "tool",
        "duration_ms",
        "status",
        "method",
        "route",
        "model",
        "direction",
        "tokens",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cost_usd",
        "outcome",
        "reason",
        "count",
        "step",
        "error_code",
        "approval_id",
        "skill",
        "usage_reported",
        "cost_known",
        # A one-way digest of the rejected credential or peer. The raw value
        # never reaches a log record; this is the auth-failure correlation key.
        "identity_hash",
    }
)

#: Full-string match on purpose: ``re.match`` with a trailing ``$`` accepts
#: ``"req_ok\n"`` because ``$`` also matches just before a final newline, which
#: is exactly the log-injection shape this check exists to reject.
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


_request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "kelp_request_id", default=None
)

_level_value = LEVELS[_DEFAULT_LEVEL]
_level_name = _DEFAULT_LEVEL
_scrubber = SecretScrubber()
_stream: Any | None = None
_write_lock = threading.Lock()
#: Logging stays silent until the process entrypoint configures it. Library
#: users that embed ``ChatAPI`` (the demo harness) own their stdout and must
#: not have request lines interleaved with their own protocol.
_enabled = False


def new_request_id() -> str:
    """A fresh, opaque request id. Never derived from user input."""

    return f"req_{uuid.uuid4().hex}"


def is_valid_request_id(value: Any) -> bool:
    """Accept a caller-supplied id only when it is inert and bounded.

    The header is echoed back and logged, so it must not be able to inject a
    newline or a second log record.
    """

    return isinstance(value, str) and bool(_REQUEST_ID_RE.fullmatch(value))


def current_request_id() -> str | None:
    return _request_id_var.get()


def set_request_id(value: str | None) -> contextvars.Token:
    return _request_id_var.set(value)


def reset_request_id(token: contextvars.Token) -> None:
    try:
        _request_id_var.reset(token)
    except ValueError:  # pragma: no cover - token from another context
        _request_id_var.set(None)


@contextmanager
def request_context(request_id: str | None) -> Iterator[str | None]:
    """Bind ``request_id`` for the duration of the block."""

    token = set_request_id(request_id)
    try:
        yield request_id
    finally:
        reset_request_id(token)


def log_level() -> str:
    return _level_name


def configure_logging(
    *,
    level: str | None = None,
    secret_values: Iterable[str] | None = None,
    stream: Any | None = None,
    enabled: bool = True,
) -> str:
    """Set the process level, redaction set, and destination.

    ``secret_values=None`` keeps the current redaction set; an explicit
    iterable replaces it. ``stream=None`` means write to ``sys.stdout`` at emit
    time, which is what the server does. ``enabled=False`` silences the sink
    again (used by tests that restore global state).
    """

    global _level_value, _level_name, _scrubber, _stream, _enabled

    _enabled = bool(enabled)
    if level is not None:
        candidate = str(level).strip().lower()
        if candidate not in LEVELS:
            candidate = _DEFAULT_LEVEL
        if candidate == "warn":
            candidate = "warning"
        _level_name = candidate
        _level_value = LEVELS[candidate]
    if secret_values is not None:
        _scrubber = SecretScrubber(secret_values)
    _stream = stream
    return _level_name


def add_secret_values(values: Iterable[str]) -> None:
    _scrubber.add_values(values)


def _record_enabled(level: str) -> tuple[bool, str]:
    name = str(level).strip().lower() or _DEFAULT_LEVEL
    if name == "warn":
        name = "warning"
    value = LEVELS.get(name)
    if value is None:
        name = _DEFAULT_LEVEL
        value = LEVELS[name]
    return value >= _level_value, name


def _timestamp() -> str:
    now = datetime.now(timezone.utc)
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _clean(value: Any) -> Any:
    """Return a JSON-safe, scrubbed scalar or ``None`` when not permitted."""

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # ``json.dumps`` is called with ``allow_nan=False`` below. A duration
        # that divided by zero or overflowed would otherwise raise inside the
        # sink and drop the whole line; drop the impossible value instead.
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _scrubber.scrub_text(value)
    return None


def log(event: str, *, level: str = "info", **fields: Any) -> dict[str, Any] | None:
    """Write one JSON line. Returns the record, or ``None`` when suppressed."""

    if not _enabled:
        return None
    enabled, level_name = _record_enabled(level)
    if not enabled:
        return None

    record: dict[str, Any] = {
        "ts": _timestamp(),
        "level": level_name,
        "event": _scrubber.scrub_text(str(event)),
    }
    request_id = fields.pop("request_id", None) or current_request_id()
    # A supplied or ambient id is only echoed when it passes the same check the
    # HTTP layer applies, so an unvalidated caller cannot smuggle a separator
    # into the one field every sink joins on.
    if is_valid_request_id(request_id):
        record["request_id"] = _scrubber.scrub_text(str(request_id))
    for key, value in fields.items():
        if key not in _ALLOWED_FIELDS:
            continue
        cleaned = _clean(value)
        if cleaned is not None:
            record[key] = cleaned

    line = json.dumps(record, separators=(",", ":"), default=str, allow_nan=False)
    with _write_lock:
        stream = _stream if _stream is not None else sys.stdout
        print(line, file=stream, flush=True)
    return record

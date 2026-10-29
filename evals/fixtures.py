"""Offline fixture plumbing: recorded provider transcripts and local documents.

Recorded transcripts are intentionally small, hand-maintained reels of the
events the kernel consumes. They are *not* captured provider responses: they
are deterministic baselines that keep the offline suite stable and free of
secrets. The report labels this explicitly as ``recorded://synthetic``.
"""

from __future__ import annotations

import json
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Sequence

from kernel.provider import (
    ChatRequest,
    ProviderFailure,
    StreamEnd,
    TextDelta,
    ToolCallReady,
    UsageReport,
)

__all__ = [
    "FixtureError",
    "RecordedProvider",
    "load_recorded_fixtures",
    "make_pdf",
    "substitute",
    "write_artifact",
]

_ALLOWED_EVENTS = frozenset({"text", "usage", "tool_call", "failure", "stream_end"})


class FixtureError(ValueError):
    """The recorded fixture file is missing, malformed, or inconsistent."""


def load_recorded_fixtures(path: str | Path) -> dict[str, Any]:
    """Load and validate the versioned recorded-provider fixture set."""

    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FixtureError(f"recorded fixture is unreadable: {path}") from exc
    except json.JSONDecodeError as exc:
        raise FixtureError(f"recorded fixture is not valid JSON: {path}") from exc

    if not isinstance(data, Mapping):
        raise FixtureError("recorded fixture must be a JSON object")
    if data.get("fixture_version") != 1:
        raise FixtureError("recorded fixture_version must be 1")
    tasks = data.get("tasks")
    if not isinstance(tasks, Mapping) or not tasks:
        raise FixtureError("recorded fixture needs a non-empty tasks object")

    for task_id, record in tasks.items():
        if not isinstance(task_id, str) or not task_id:
            raise FixtureError("recorded fixture task ids must be strings")
        turns = record.get("turns") if isinstance(record, Mapping) else None
        if not isinstance(turns, list) or not turns:
            raise FixtureError(f"{task_id}: turns must be a non-empty list")
        for index, turn in enumerate(turns):
            if not isinstance(turn, list) or not turn:
                raise FixtureError(f"{task_id}: turn {index} must be a non-empty list")
            for event in turn:
                if not isinstance(event, Mapping):
                    raise FixtureError(f"{task_id}: turn {index} has a non-object event")
                kind = event.get("type")
                if kind not in _ALLOWED_EVENTS:
                    raise FixtureError(f"{task_id}: unknown event type {kind!r}")
                _validate_event(task_id, index, event)
    return dict(data)


def _validate_event(task_id: str, turn: int, event: Mapping[str, Any]) -> None:
    kind = event["type"]
    if kind == "text" and not isinstance(event.get("text"), str):
        raise FixtureError(f"{task_id}: turn {turn} text event needs a string")
    if kind == "usage":
        for name in ("input_tokens", "output_tokens"):
            value = event.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise FixtureError(f"{task_id}: turn {turn} usage {name} must be >= 0")
    if kind == "tool_call":
        if not isinstance(event.get("name"), str) or not event["name"]:
            raise FixtureError(f"{task_id}: turn {turn} tool_call needs a name")
        if not isinstance(event.get("arguments"), Mapping):
            raise FixtureError(f"{task_id}: turn {turn} tool_call needs arguments")
        if not isinstance(event.get("call_id"), str) or not event["call_id"]:
            raise FixtureError(f"{task_id}: turn {turn} tool_call needs call_id")
    if kind == "failure":
        if not isinstance(event.get("code"), str) or not event["code"]:
            raise FixtureError(f"{task_id}: turn {turn} failure needs a code")
        if not isinstance(event.get("message"), str):
            raise FixtureError(f"{task_id}: turn {turn} failure needs a message")
    if kind == "stream_end" and not isinstance(event.get("finish_reason", "stop"), str):
        raise FixtureError(f"{task_id}: turn {turn} finish_reason must be a string")


def _parse_event(task_id: str, event: Mapping[str, Any]) -> Any:
    kind = event["type"]
    if kind == "text":
        return TextDelta(str(event["text"]))
    if kind == "usage":
        input_tokens = int(event["input_tokens"])
        output_tokens = int(event["output_tokens"])
        return UsageReport(input_tokens, output_tokens, input_tokens + output_tokens)
    if kind == "tool_call":
        return ToolCallReady(
            index=int(event.get("index", 0)),
            call_id=str(event["call_id"]),
            name=str(event["name"]),
            arguments=dict(event["arguments"]),
        )
    if kind == "failure":
        return ProviderFailure(
            str(event["code"]),
            str(event["message"]),
            bool(event.get("retryable", False)),
            event.get("status") if isinstance(event.get("status"), int) else None,
        )
    if kind == "stream_end":
        return StreamEnd(str(event.get("finish_reason", "stop")))
    raise FixtureError(f"{task_id}: unknown event type {kind!r}")


@dataclass
class RecordedProvider:
    """Replay one recorded turn per provider call; the last turn repeats."""

    task_id: str
    record: Mapping[str, Any]
    requests: list[ChatRequest]

    def __init__(self, task_id: str, record: Mapping[str, Any]):
        turns = record.get("turns")
        if not isinstance(turns, list) or not turns:
            raise FixtureError(f"{task_id}: no recorded turns")
        self.task_id = task_id
        self.record = record
        self._turns = [
            [_parse_event(task_id, event) for event in turn]  # type: ignore[misc]
            for turn in turns
        ]
        self.requests = []

    async def stream(self, request: ChatRequest) -> AsyncIterator[Any]:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self._turns) - 1)
        for event in self._turns[index]:
            yield event


def substitute(value: Any, replacements: Mapping[str, str]) -> Any:
    """Recursively replace ``{{name}}`` placeholders in strings and keys."""

    if isinstance(value, str):
        for name, replacement in replacements.items():
            value = value.replace("{{" + name + "}}", replacement)
        return value
    if isinstance(value, list):
        return [substitute(item, replacements) for item in value]
    if isinstance(value, tuple):
        return tuple(substitute(item, replacements) for item in value)
    if isinstance(value, Mapping):
        return {
            (substitute(key, replacements) if isinstance(key, str) else key): substitute(
                item, replacements
            )
            for key, item in value.items()
        }
    return value


# ---------------------------------------------------------------------------
# deterministic local documents


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _content_stream(lines: Sequence[str]) -> bytes:
    parts = ["BT", "/F1 12 Tf", "72 720 Td", "16 TL"]
    for line in lines:
        parts.append(f"({_escape(line)}) Tj")
        parts.append("T*")
    parts.append("ET")
    return "\n".join(parts).encode("latin-1")


def make_pdf(pages: Sequence[Sequence[str]]) -> bytes:
    """Build a small, structurally real PDF with one page per line group."""

    if not pages:
        raise ValueError("a PDF fixture needs at least one page")
    page_numbers = [3 + 2 * index for index in range(len(pages))]
    kids = " ".join(f"{number} 0 R" for number in page_numbers)
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("ascii"),
    ]
    for index, lines in enumerate(pages):
        payload = _content_stream(lines)
        compressed = zlib.compress(payload)
        content_number = page_numbers[index] + 1
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Contents {content_number} 0 R >>"
            ).encode("ascii")
        )
        objects.append(
            b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(compressed)
            + compressed
            + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    for number, body in enumerate(objects, start=1):
        out += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    out += b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    return bytes(out)


def write_artifact(
    workspace: str | Path,
    doc: Mapping[str, Any],
    token: str,
) -> tuple[str, str]:
    """Write a dataset document under ``var/artifacts`` and return its handle."""

    kind = str(doc.get("kind") or "")
    if kind == "csv":
        content = doc.get("content")
        if not isinstance(content, str) or not content:
            raise FixtureError("CSV fixture needs non-empty content")
        data = content.encode("utf-8")
    elif kind == "pdf":
        pages = doc.get("pages")
        if not isinstance(pages, list) or not pages:
            raise FixtureError("PDF fixture needs non-empty pages")
        data = make_pdf(pages)
    else:
        raise FixtureError(f"unsupported document kind {kind!r}")

    target = Path(workspace) / "var" / "artifacts"
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{token}.{kind}").write_bytes(data)
    return f"artifact:{token}", kind

"""Read-only access to the artifacts a demo turn attached.

Skill handlers deliberately receive only a :class:`skills.context.SkillContext`
— no database handle. The artifact registry stores files under
``${WORKSPACE}/var/artifacts`` with an opaque name derived from the handle, so a
fixture handler can resolve the same file the registry served, without widening
any real interface.

Nothing here accepts a caller-supplied path: the only input is an
``artifact:<token>`` handle, validated against the registry's own pattern.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from pathlib import Path

from chat.artifacts import HANDLE_RE

__all__ = ["ArtifactText", "money", "numeric", "read_csv", "read_pdf"]

_TJ_RE = re.compile(rb"\(((?:\\.|[^\\()])*)\)\s*Tj")
_STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)
_UNESCAPE = {b"\\(": b"(", b"\\)": b")", b"\\\\": b"\\"}


@dataclass(frozen=True)
class ArtifactText:
    kind: str
    mime: str
    pages: list[list[str]]


def _escape(raw: bytes) -> str:
    for source, target in _UNESCAPE.items():
        raw = raw.replace(source, target)
    return raw.decode("latin-1", "replace")


def _lines(stream: bytes) -> list[str]:
    lines: list[str] = []
    for match in _TJ_RE.finditer(stream):
        text = _escape(match.group(1))
        text = " ".join(text.split())
        if text:
            lines.append(text)
    return lines


def _artifact_path(context: object, handle: str, kind: str) -> Path:
    if not isinstance(handle, str) or not HANDLE_RE.match(handle):
        raise ValueError("artifact handle is not valid")
    if kind not in {"pdf", "csv"}:
        raise ValueError("unsupported artifact kind")
    token = handle.split(":", 1)[1]
    root = Path(getattr(context, "workspace_root")) / "var" / "artifacts"
    path = (root / f"{token}.{kind}").resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise FileNotFoundError("artifact content is missing")
    return path


def read_pdf(context: object, handle: str) -> ArtifactText:
    data = _artifact_path(context, handle, "pdf").read_bytes()
    if not data.startswith(b"%PDF-"):
        raise ValueError("artifact is not a PDF")
    pages = [_lines(stream) for stream in _STREAM_RE.findall(data)]
    pages = [page for page in pages if page]
    return ArtifactText(kind="pdf", mime="application/pdf", pages=pages or [[]])


def read_csv(context: object, handle: str) -> tuple[list[str], list[list[str]]]:
    text = _artifact_path(context, handle, "csv").read_text(encoding="utf-8")
    delimiter = ";" if ";" in (text.splitlines()[0] if text.splitlines() else "") else ","
    rows = [
        [cell.strip() for cell in row]
        for row in csv.reader(io.StringIO(text), delimiter=delimiter)
        if any(cell.strip() for cell in row)
    ]
    if not rows:
        raise ValueError("artifact CSV is empty")
    return rows[0], rows[1:]


def money(value: float) -> str:
    """``1284500`` -> ``1,284,500``; cents kept only when they exist."""

    if abs(value - round(value)) < 0.005:
        return f"{round(value):,}"
    return f"{value:,.2f}"


def numeric(value: str) -> float | None:
    try:
        return float(value.replace(",", "").replace("$", ""))
    except ValueError:
        return None

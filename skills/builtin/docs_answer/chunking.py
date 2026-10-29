"""Deterministic chunking with page/row locators.

A chunk is the unit that gets scored and cited. Everything here is a pure
function of the artifact bytes and the chunk parameters, so rebuilding an index
produces byte-identical JSON. Chunk text never leaves the workspace and is
never recombined with caller input; locators are derived only from source
positions. CSV row text is clipped (:func:`clip_text`) before it is chunked, so
one oversized cell cannot dominate the token budget, and the answer builder
clips with the same helper so a citation and its chunk always agree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .extract import CsvTable

__all__ = [
    "Chunk",
    "CLIP_CHARS",
    "CSV_CHUNK_ROWS",
    "MAX_CHUNK_CHARS",
    "chunk_csv",
    "chunk_pdf",
    "chunk_to_dict",
    "chunk_from_dict",
    "clip_text",
]

#: Chunk size target. Small enough that a top-5 selection fits a 1.2k-token
#: context cap, large enough to keep a whole paragraph together.
MAX_CHUNK_CHARS = 900
#: Overlap carried into the next window when a single line must be split.
CHUNK_OVERLAP_CHARS = 140
#: CSV rows per chunk; rows stay individually addressable inside a chunk.
CSV_CHUNK_ROWS = 12
#: Longest row excerpt that may reach a chunk or a citation. One 100 KiB cell
#: would otherwise blow the token budget and dominate the response, so every
#: row is clipped to this many characters (a ``…`` marks the cut).
CLIP_CHARS = 280

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def clip_text(text: str, limit: int = CLIP_CHARS) -> str:
    """Collapse whitespace and cut to ``limit`` characters on a word boundary.

    The result is always derived from ``text`` and never contains anything the
    source did not: it is an excerpt, not a summary. The trailing ``…`` says a
    cut happened, so a reader never mistakes an excerpt for the whole span.
    """

    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit + 1].rsplit(" ", 1)[0]
    return (cut or text[:limit]).rstrip(",;:") + "…"


@dataclass(frozen=True)
class Chunk:
    """One indexed span with a stable citation locator."""

    locator: str
    text: str
    ordinal: int
    page: int | None = None
    rows: tuple[tuple[int, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "locator": self.locator,
            "ordinal": self.ordinal,
            "text": self.text,
        }
        if self.page is not None:
            payload["page"] = self.page
        if self.rows:
            payload["rows"] = [[number, text] for number, text in self.rows]
        return payload


def chunk_to_dict(chunk: Chunk) -> dict[str, Any]:
    return chunk.to_dict()


def chunk_from_dict(payload: Mapping[str, Any]) -> Chunk:
    rows = tuple((int(number), str(text)) for number, text in payload.get("rows") or ())
    page = payload.get("page")
    return Chunk(
        locator=str(payload["locator"]),
        text=str(payload["text"]),
        ordinal=int(payload["ordinal"]),
        page=int(page) if page is not None else None,
        rows=rows,
    )


def _windows(line: str) -> list[str]:
    """Split an over-long line on word boundaries, with a small overlap."""

    words = line.split()
    if not words:
        return []
    windows: list[str] = []
    current: list[str] = []
    size = 0
    for word in words:
        if current and size + len(word) + 1 > MAX_CHUNK_CHARS:
            windows.append(" ".join(current))
            # Keep the tail words as overlap so a sentence split mid-window is
            # still recoverable from one of the neighbouring chunks.
            tail: list[str] = []
            tail_size = 0
            for previous in reversed(current):
                if tail_size + len(previous) + 1 > CHUNK_OVERLAP_CHARS:
                    break
                tail.insert(0, previous)
                tail_size += len(previous) + 1
            current = tail
            size = tail_size
        current.append(word)
        size += len(word) + 1
    if current:
        windows.append(" ".join(current))
    return windows


def chunk_pdf(pages: Sequence[Sequence[str]]) -> list[Chunk]:
    """Chunk every page, preserving page numbers for citations."""

    chunks: list[Chunk] = []
    ordinal = 0
    for page_number, lines in enumerate(pages, start=1):
        buffer: list[str] = []
        size = 0

        def flush() -> None:
            nonlocal buffer, size, ordinal
            text = "\n".join(buffer).strip()
            if text:
                chunks.append(
                    Chunk(locator=f"page {page_number}", text=text, ordinal=ordinal, page=page_number)
                )
                ordinal += 1
            buffer = []
            size = 0

        for line in lines:
            text = " ".join(line.split())
            if not text:
                continue
            if len(text) > MAX_CHUNK_CHARS:
                flush()
                for window in _windows(text):
                    chunks.append(
                        Chunk(
                            locator=f"page {page_number}",
                            text=window,
                            ordinal=ordinal,
                            page=page_number,
                        )
                    )
                    ordinal += 1
                continue
            if buffer and size + len(text) + 1 > MAX_CHUNK_CHARS:
                flush()
            buffer.append(text)
            size += len(text) + 1
        flush()
    return chunks


def chunk_csv(table: CsvTable) -> list[Chunk]:
    """Group rows into chunks; every row keeps its own locator inside."""

    chunks: list[Chunk] = []
    ordinal = 0
    group: list[tuple[int, str]] = []
    size = 0

    def flush() -> None:
        nonlocal group, size, ordinal
        if not group:
            return
        first = group[0][0]
        last = group[-1][0]
        locator = f"row {first}" if first == last else f"rows {first}-{last}"
        chunks.append(
            Chunk(
                locator=locator,
                text="\n".join(text for _, text in group),
                ordinal=ordinal,
                rows=tuple(group),
            )
        )
        ordinal += 1
        group = []
        size = 0

    for index, row in enumerate(table.rows):
        number = index + 2  # row 1 is the header
        # Clip per row, not per chunk: this row's excerpt is also what a
        # citation will quote, so the two can never disagree.
        text = clip_text(",".join(cell for cell in row))
        if group and (len(group) >= CSV_CHUNK_ROWS or size + len(text) + 1 > MAX_CHUNK_CHARS):
            flush()
        group.append((number, text))
        size += len(text) + 1
    flush()
    return chunks


def sentences(text: str) -> list[str]:
    """Split chunk text into sentences/lines for extractive answers."""

    parts = [part.strip() for part in _SENTENCE_SPLIT.split(text.replace("\n", " "))]
    return [part for part in parts if part]

"""Answer assembly: turn ranked chunks into an answer plus citations.

The answer is extractive and deterministic: sentences (PDF) or rows (CSV) are
copied from the artifact, never paraphrased, and every citation's ``text`` is a
verbatim slice of the cited page or row. Nothing here reads the network or the
caller's secrets.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from .chunking import Chunk, sentences
from .extract import CsvTable, numeric_value
from .retrieval import ScoredChunk, tokenize

__all__ = ["csv_answer", "pdf_answer"]

_SNIPPET_CHARS = 280
_MAX_SENTENCES = 3
_MAX_PDF_CITATIONS = 8
_MAX_CSV_MATCHES = 5
#: Hard ceiling from the manifest's ``citations.maxItems``.
_MAX_CITATIONS = 50

_AGGREGATES = {
    "count": ("how many", "count", "number of"),
    "average": ("average", "avg", "mean"),
    "minimum": ("minimum", "min", "lowest", "smallest"),
    "maximum": ("maximum", "max", "highest", "largest", "most"),
    "total": ("total", "sum", "summed", "combined", "altogether"),
}


def _clip(text: str, limit: int = _SNIPPET_CHARS) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit + 1].rsplit(" ", 1)[0]
    return (cut or text[:limit]).rstrip(",;:") + "…"


def _format_amount(value: float) -> str:
    if abs(value - round(value)) < 0.005:
        return f"{round(value):,}"
    return f"{value:,.2f}"


def _column_tokens(header: str) -> set[str]:
    return set(tokenize(header.replace("_", " ")))


def _numeric_columns(table: CsvTable) -> list[int]:
    columns: list[int] = []
    for index in range(len(table.header)):
        values = [row[index] for row in table.rows if index < len(row) and row[index]]
        if values and all(numeric_value(value) is not None for value in values):
            columns.append(index)
    return columns


def _aggregate_for(tokens: set[str], question: str) -> str | None:
    lowered = question.lower()
    for operation, words in _AGGREGATES.items():
        for word in words:
            if " " in word:
                if word in lowered:
                    return operation
            elif word in tokens:
                return operation
    return None


def _pdf_sentences(
    selected: Sequence[ScoredChunk], terms: set[str]
) -> list[tuple[Chunk, str, int]]:
    ranked: list[tuple[int, int, Chunk, str]] = []
    for position, item in enumerate(selected):
        for order, sentence in enumerate(sentences(item.chunk.text)):
            words = set(tokenize(sentence))
            overlap = len(words & terms)
            if overlap:
                ranked.append((overlap, position * 100 + order, item.chunk, sentence))
    ranked.sort(key=lambda entry: (-entry[0], entry[1]))
    return [(chunk, sentence, overlap) for overlap, _, chunk, sentence in ranked]


def pdf_answer(
    selected: Sequence[ScoredChunk], question: str
) -> tuple[str, list[dict[str, str]]]:
    """Extractive PDF answer with page citations."""

    if not selected:
        raise ValueError("PDF has no readable text")

    terms = set(tokenize(question))
    candidates = _pdf_sentences(selected, terms)

    picked: list[tuple[Chunk, str]] = []
    seen: set[str] = set()
    for chunk, sentence, _ in candidates:
        key = " ".join(tokenize(sentence))
        if not key or key in seen:
            continue
        seen.add(key)
        picked.append((chunk, sentence))
        if len(picked) >= _MAX_SENTENCES:
            break

    if not picked:
        fallback = sentences(selected[0].chunk.text)
        if not fallback:
            fallback = [" ".join(selected[0].chunk.text.split())]
        picked = [(selected[0].chunk, fallback[0])]

    answer = " ".join(_clip(sentence, _SNIPPET_CHARS) for _, sentence in picked)

    citations: list[dict[str, str]] = []
    seen_locations: set[tuple[str, str]] = set()
    for chunk, sentence in picked:
        snippet = _clip(sentence)
        key = (chunk.locator, snippet)
        if key in seen_locations:
            continue
        seen_locations.add(key)
        citations.append({"locator": chunk.locator, "text": snippet})
        if len(citations) >= _MAX_PDF_CITATIONS:
            break

    # Cite the page the answer came from even when the snippet repeats.
    if not citations:
        citations.append({"locator": picked[0][0].locator, "text": _clip(picked[0][1])})
    return answer, citations


def _row_text(row: Sequence[str]) -> str:
    return ",".join(row)


def _aggregate_answer(
    table: CsvTable, column: int, operation: str, max_rows: int
) -> tuple[str, list[dict[str, str]]]:
    header = table.header[column]
    values: list[float] = []
    used: list[int] = []
    for index, row in enumerate(table.rows):
        text = row[column] if column < len(row) else ""
        value = numeric_value(text)
        if value is None:
            continue
        values.append(value)
        used.append(index + 2)

    if not values:
        return f"No numeric values found in column {header}.", []

    if operation == "count":
        answer = f"Column {header} has {len(values)} numeric values across {len(table.rows)} rows."
    else:
        computed = {
            "total": sum(values),
            "average": sum(values) / len(values),
            "minimum": min(values),
            "maximum": max(values),
        }[operation]
        label = {
            "total": "Total",
            "average": "Average",
            "minimum": "Minimum",
            "maximum": "Maximum",
        }[operation]
        answer = f"{label} {header} across {len(values)} rows is {_format_amount(computed)}."
        if operation in ("minimum", "maximum"):
            # Name the row that holds the extreme; an aggregate alone would
            # leave "which one?" unanswered.
            position = values.index(computed)
            number = used[position]
            answer += f" Row {number}: {_row_text(table.rows[number - 2])}."

    limit = max(1, min(max_rows, _MAX_CITATIONS))
    citations = [
        {"locator": f"row {number}", "text": _row_text(table.rows[number - 2])}
        for number in used[:limit]
    ]
    return answer, citations


def csv_answer(
    table: CsvTable,
    question: str,
    *,
    max_rows: int = 20,
) -> tuple[str, list[dict[str, str]]]:
    """Aggregate or row-match answer with row citations."""

    if not table.header:
        raise ValueError("CSV is empty")

    terms = set(tokenize(question))
    operation = _aggregate_for(terms, question)

    columns = _numeric_columns(table)
    if columns:
        matching = [index for index in columns if _column_tokens(table.header[index]) & terms]
        column = matching[0] if matching else columns[0]
        if operation is not None or matching:
            return _aggregate_answer(table, column, operation or "total", max_rows)

    # Lexical row matching: rank rows by term overlap, then by row number.
    scored_rows: list[tuple[int, int]] = []
    for index, row in enumerate(table.rows):
        words = set(tokenize(_row_text(row)))
        overlap = len(words & terms)
        if overlap:
            scored_rows.append((overlap, index + 2))
    scored_rows.sort(key=lambda item: (-item[0], item[1]))

    if not scored_rows:
        answer = (
            f"The file has {len(table.rows)} data rows across {len(table.header)} columns."
        )
        return answer, []

    limit = min(_MAX_CSV_MATCHES, max(1, max_rows), _MAX_CITATIONS)
    chosen = sorted(number for _, number in scored_rows[:limit])
    citations = [
        {"locator": f"row {number}", "text": _row_text(table.rows[number - 2])}
        for number in chosen
    ]
    if len(chosen) == 1:
        answer = f"Row {chosen[0]}: {citations[0]['text']}."
    else:
        answer = "Matching rows: " + "; ".join(
            f"row {number} ({_row_text(table.rows[number - 2])})" for number in chosen
        ) + "."
    return answer, citations


def artifact_descriptor(index: Any) -> dict[str, Any]:
    """Output-schema artifact block straight from the index metadata."""

    artifact = index["artifact"]
    descriptor: dict[str, Any] = {
        "handle": artifact["handle"],
        "kind": artifact["kind"],
        "mime": artifact["mime"],
    }
    if artifact["kind"] == "pdf":
        descriptor["pages"] = int(artifact["pages"])
    else:
        descriptor["rows"] = int(artifact["rows"])
    return descriptor

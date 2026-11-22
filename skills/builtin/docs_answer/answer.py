"""Answer assembly: turn ranked chunks into an answer plus citations.

The answer is extractive and deterministic: sentences (PDF) or rows (CSV) are
copied from the artifact, never paraphrased. A citation's ``text`` is the cited
page or row with whitespace collapsed and clipped to :data:`CLIP_CHARS`
characters — an excerpt, not a whole span, and marked with ``…`` when it was
cut. It is derived only from the artifact, so no caller input can appear in it;
a secret that was itself pasted into the document is handled by the handler's
best-effort scrub on the way out, not here. Nothing here reads the network or
the caller's secrets.
"""

from __future__ import annotations

from typing import Any, Sequence

from .chunking import Chunk, clip_text as _clip, sentences
from .extract import CsvTable, numeric_value
from .retrieval import ScoredChunk, tokenize

__all__ = ["csv_answer", "pdf_answer"]

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

    answer = " ".join(_clip(sentence) for _, sentence in picked)

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
    """A row as it is quoted in answers and citations: clipped, never raw.

    A spreadsheet cell can hold megabytes. Quoting one row verbatim would put
    the whole cell in the response, so the excerpt is clipped exactly like a
    CSV chunk.
    """

    return _clip(",".join(row))


def _filter_groups(
    table: CsvTable, column: int, question: str
) -> list[tuple[list[int], list[str]]]:
    """Row-filter groups ``(columns, terms)`` named by the question, in order.

    A question such as "revenue for East" carries a filter as well as an
    aggregate, but a CSV has no schema saying which words are values. A word is
    treated as one only when it is a token of a cell in a non-numeric column:
    that keeps filler ("across", "regions") from changing the answer, and the
    aggregate, header and target-column words are never candidates. Terms that
    share a candidate column are alternatives (OR); distinct groups must all
    hold (AND).
    """

    skip = set().union(*(_column_tokens(header) for header in table.header))
    for words in _AGGREGATES.values():
        skip.update(word for word in words if " " not in word)
    terms = [t for t in dict.fromkeys(tokenize(question)) if t not in skip]
    numeric = set(_numeric_columns(table)) | {column}
    candidates = [index for index in range(len(table.header)) if index not in numeric]

    columns_of: dict[str, set[int]] = {}
    # Tokenise each candidate column once, not once per question term.
    vocabulary = {
        index: {token for row in table.rows if index < len(row) for token in tokenize(row[index])}
        for index in candidates
    }
    for term in terms:
        found = {index for index in candidates if term in vocabulary[index]}
        if found:
            columns_of[term] = found

    # Merge terms whose column sets overlap, transitively, in question order.
    groups: list[tuple[set[int], list[str]]] = []
    for term, found in columns_of.items():
        merged_columns, merged_terms = set(found), [term]
        for group in [g for g in groups if g[0] & merged_columns]:
            groups.remove(group)
            merged_columns |= group[0]
            merged_terms += group[1]
        groups.append((merged_columns, merged_terms))
    order = {term: position for position, term in enumerate(terms)}
    return [(sorted(cols), sorted(words, key=order.__getitem__)) for cols, words in groups]


def _matching_rows(
    table: CsvTable, groups: Sequence[tuple[list[int], list[str]]]
) -> list[int]:
    """Indices of rows that satisfy every filter group."""

    passing: list[int] = []
    for index, row in enumerate(table.rows):
        for columns, words in groups:
            cells = {
                token
                for column in columns
                if column < len(row)
                for token in tokenize(row[column])
            }
            if not cells.intersection(words):
                break
        else:
            passing.append(index)
    return passing


def _describe_filters(
    table: CsvTable, groups: Sequence[tuple[list[int], list[str]]]
) -> str:
    """"region matches east or west and product matches gpu" for the answer."""

    return " and ".join(
        "/".join(_clip(table.header[column]) for column in columns)
        + " matches "
        + " or ".join(words)
        for columns, words in groups
    )


def _aggregate_answer(
    table: CsvTable,
    column: int,
    operation: str,
    max_rows: int,
    question: str = "",
) -> tuple[str, list[dict[str, str]]]:
    # The header is file content: clip it like a row so a huge header cannot
    # flood the answer.
    header = _clip(table.header[column])
    groups = _filter_groups(table, column, question)
    if groups:
        rows = _matching_rows(table, groups)
        where = _describe_filters(table, groups)
    else:
        rows = list(range(len(table.rows)))
        where = ""
    if groups and not rows:
        return f"No rows where {where}.", []
    suffix = f" where {where}" if where else ""

    values: list[float] = []
    used: list[int] = []
    for index in rows:
        row = table.rows[index]
        text = row[column] if column < len(row) else ""
        value = numeric_value(text)
        if value is None:
            continue
        values.append(value)
        used.append(index + 2)

    if not values:
        return f"No numeric values found in column {header}{suffix}.", []

    if operation == "count":
        answer = (
            f"Column {header} has {len(values)} numeric values across {len(rows)} rows{suffix}."
        )
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
        answer = f"{label} {header} across {len(values)} rows{suffix} is {_format_amount(computed)}."
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
            return _aggregate_answer(table, column, operation or "total", max_rows, question)

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

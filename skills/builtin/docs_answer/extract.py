"""Local text extraction for `docs.answer` artifacts.

Stdlib only, no network, no subprocesses. Two readers:

* :func:`extract_pdf` walks the PDF object table, resolves the page tree and each
  page's ``/Contents`` streams (including objects packed into an ``/ObjStm``),
  inflates FlateDecode streams, follows ``Form`` XObjects to a fixed depth, and
  replays the text operators so every line keeps the page it came from. Inline
  images (``BI … ID … EI``) are skipped whole: their bytes are opaque, so a
  ``Tj`` that happens to appear inside them is never read as text. Pages are
  returned in document order, so ``page N`` is a stable citation locator.
* :func:`read_csv` yields a header plus logical records. Row ``N`` is the
  1-based *record* number in file order: the header is row 1, so the first data
  record is row 2. Records are counted after blank lines are dropped, so a blank
  line or a quoted multi-line field does not shift the numbering. That is the
  citation convention in ``docs/CHAT_SPEC.md`` §8.1.

Both readers are deliberately forgiving about syntax and strict about output:
undecodable content is skipped, never guessed. Byte budgets bound the total
work: :data:`MAX_ARTIFACT_BYTES` for the file, ``_MAX_DECOMPRESSED_BYTES`` for
one stream, and ``_TOTAL_DECOMPRESSED_BYTES`` for the whole document — so a
crafted PDF with many small bombs cannot expand without limit.
"""

from __future__ import annotations

import csv
import io
import re
import zlib
from dataclasses import dataclass

__all__ = [
    "CsvTable",
    "MAX_ARTIFACT_BYTES",
    "extract_pdf",
    "numeric_value",
    "read_csv",
]

#: Hard ceiling on what a single artifact may cost to read. The chat uploader
#: already enforces 20 MiB; this is defence in depth for direct skill use.
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
#: Ceiling on one inflated content stream, so a crafted ``/FlateDecode`` bomb
#: cannot expand a small upload into unbounded memory.
_MAX_DECOMPRESSED_BYTES = 32 * 1024 * 1024
#: Ceiling on everything one document may inflate in total. A per-stream cap
#: alone is not enough: a PDF with a thousand small bombs would each pass it.
_TOTAL_DECOMPRESSED_BYTES = 96 * 1024 * 1024
#: How deep ``Form`` XObjects may nest before the walk stops. Real documents
#: stay at one or two levels; the cap keeps a cyclic document finite.
_MAX_FORM_DEPTH = 4
#: How far ``/Resources`` may be inherited up a ``/Parent`` chain.
_MAX_RESOURCE_DEPTH = 8

_OBJ_RE = re.compile(rb"(?<![0-9])(\d+)\s+(\d+)\s+obj\b(.*?)endobj", re.DOTALL)
_STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)
_PAGE_TYPE_RE = re.compile(rb"/Type\s*/Page(?![A-Za-z])")
_PAGES_TYPE_RE = re.compile(rb"/Type\s*/Pages(?![A-Za-z])")
_OBJSTM_TYPE_RE = re.compile(rb"/Type\s*/ObjStm(?![A-Za-z])")
_KIDS_RE = re.compile(rb"/Kids\s*\[([^\]]*)\]")
_CONTENTS_RE = re.compile(rb"/Contents\s*(\[[^\]]*\]|[0-9]+\s+[0-9]+\s+R)")
_REF_RE = re.compile(rb"([0-9]+)\s+[0-9]+\s+R")
_FLATE_RE = re.compile(rb"/Filter\s*(\[[^\]]*\]|/[A-Za-z0-9]+)")
_FIRST_RE = re.compile(rb"/First\s+([0-9]+)")
_N_RE = re.compile(rb"/N\s+([0-9]+)")
_PARENT_RE = re.compile(rb"/Parent\s+([0-9]+\s+[0-9]+\s+R)")
_XOBJECT_RE = re.compile(rb"/XObject\s*<<(.*?)>>", re.DOTALL)
_XOBJECT_REF_RE = re.compile(rb"/XObject\s+([0-9]+\s+[0-9]+\s+R)")
_FORM_SUBTYPE_RE = re.compile(rb"/Subtype\s*/Form(?![A-Za-z])")
_NAME_REF_RE = re.compile(rb"(/[^\s/\[\]<>(){}%]+)\s+([0-9]+\s+[0-9]+\s+R)")
# ``ID`` ends the inline-image dictionary and is followed by exactly one
# whitespace byte before the opaque payload.
_INLINE_ID_RE = re.compile(rb"(?<![A-Za-z0-9])ID\s")
# ``EI`` stands alone between delimiters; the payload itself may contain the
# bytes "EI", so the first whitespace-delimited match ends the image.
_INLINE_EI_RE = re.compile(rb"\sEI(?=[\s]|$)")

# Literal strings nest at most a couple of levels in practice; two levels keeps
# the scanner linear instead of backtracking on malformed input.
_TOKEN_RE = re.compile(
    rb"\((?:[^()\\]|\\.|\((?:[^()\\]|\\.)*\))*\)"  # literal string
    rb"|<[0-9A-Fa-f\s]*>"  # hex string
    rb"|\[|\]"
    rb"|/[^\s/\[\]<>(){}%]+"  # name
    rb"|[-+]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)"  # number
    rb"|[A-Za-z*'\"]+"  # operator
    rb"|%[^\r\n]*"  # comment
)

_NUMBER_RE = re.compile(rb"[-+]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)")
_ESCAPES = {
    b"n": b"\n",
    b"r": b"\r",
    b"t": b"\t",
    b"b": b"\b",
    b"f": b"\f",
    b"(": b"(",
    b")": b")",
    b"\\": b"\\",
}


@dataclass(frozen=True)
class CsvTable:
    """Parsed CSV: header cells plus records in file order."""

    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    delimiter: str


class _Array:
    """Marker type so ``TJ`` operands stay distinguishable from plain strings."""

    __slots__ = ("items",)

    def __init__(self, items: list[bytes]):
        self.items = items


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def _header(body: bytes) -> bytes:
    """Dictionary portion of an object body (everything before ``stream``)."""

    index = body.find(b"stream")
    return body if index < 0 else body[:index]


def _decode_stream(body: bytes, *, budget: list[int] | None = None) -> bytes | None:
    """Return the inflated payload for one object body, or ``None``.

    ``budget`` is a one-element list holding the bytes the *document* may still
    inflate; every successful decode pays into it. ``None`` means "this call
    stands alone" and only the per-stream cap applies.
    """

    match = _STREAM_RE.search(body)
    if match is None:
        return None
    raw = match.group(1)
    filters = _FLATE_RE.search(_header(body))
    if filters is None:
        if budget is not None:
            if len(raw) > budget[0]:
                return None
            budget[0] -= len(raw)
        return raw
    declared = filters.group(1)
    if b"FlateDecode" not in declared and b"/Fl" not in declared:
        # Unsupported filter (image codecs, LZW, ...): not text we can read.
        return None
    allowance = _MAX_DECOMPRESSED_BYTES
    if budget is not None:
        allowance = min(allowance, budget[0])
    if allowance <= 0:
        return None
    inflater = zlib.decompressobj()
    try:
        # ``max_length`` caps expansion; a truncated or bomb-sized stream is
        # either partially decoded or rejected, never fully materialised.
        payload = inflater.decompress(raw, allowance)
    except zlib.error:
        return None
    # Charge even a rejected (partially decoded) stream: the work was done, and
    # charging it is what makes repeated bombs exhaust the document budget.
    if budget is not None:
        budget[0] -= len(payload)
    if inflater.unconsumed_tail:
        return None
    return payload


def _skip_inline_image(stream: bytes, start: int) -> int | None:
    """Offset just past an inline image whose ``BI`` ended at ``start``.

    Everything between ``ID`` and the closing whitespace-delimited ``EI`` is
    opaque binary. Tokens inside it must never reach the text replay: image
    bytes routinely contain ``(``, ``Tj`` and worse, and reading them would put
    fabricated sentences into a citation.

    Returns ``None`` when no ``ID`` follows (so this was not an inline image),
    and ``len(stream)`` when the image is unterminated — nothing after an
    unclosed payload can be trusted.
    """

    identifier = _INLINE_ID_RE.search(stream, start)
    if identifier is None:
        return None
    end = _INLINE_EI_RE.search(stream, identifier.end())
    if end is None:
        return len(stream)
    return end.end()


def _objects(data: bytes, *, budget: list[int] | None = None) -> dict[int, bytes]:
    """Map object number -> body, expanding ``/ObjStm`` containers in place."""

    objects: dict[int, bytes] = {}
    for match in _OBJ_RE.finditer(data):
        objects[int(match.group(1))] = match.group(3)

    for body in list(objects.values()):
        if _OBJSTM_TYPE_RE.search(_header(body)) is None:
            continue
        payload = _decode_stream(body, budget=budget)
        first = _FIRST_RE.search(_header(body))
        count = _N_RE.search(_header(body))
        if payload is None or first is None or count is None:
            continue
        offset = int(first.group(1))
        pairs = payload[:offset].split()
        inner = payload[offset:]
        limit = min(len(pairs) - 1, int(count.group(1)) * 2)
        for index in range(0, limit, 2):
            try:
                number = int(pairs[index])
                start = int(pairs[index + 1])
            except ValueError:
                continue
            following = int(pairs[index + 3]) if index + 3 < len(pairs) else len(inner)
            objects.setdefault(number, inner[start:following])
    return objects


def _decode_literal(raw: bytes) -> str:
    """Decode a PDF literal string body (without the surrounding parens)."""

    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", "replace")

    out = bytearray()
    index = 0
    while index < len(raw):
        char = raw[index : index + 1]
        if char != b"\\":
            out += char
            index += 1
            continue
        index += 1
        if index >= len(raw):
            break
        nxt = raw[index : index + 1]
        index += 1
        if nxt in _ESCAPES:
            out += _ESCAPES[nxt]
        elif nxt.isdigit():
            digits = nxt
            while len(digits) < 3 and index < len(raw) and raw[index : index + 1].isdigit():
                digits += raw[index : index + 1]
                index += 1
            out += bytes([int(digits, 8) & 0xFF])
        elif nxt in (b"\r", b"\n"):
            if nxt == b"\r" and raw[index : index + 1] == b"\n":
                index += 1
        else:
            out += nxt

    try:
        return bytes(out).decode("utf-8")
    except UnicodeDecodeError:
        return bytes(out).decode("latin-1", "replace")


def _decode_hex(raw: bytes) -> str:
    digits = re.sub(rb"[^0-9A-Fa-f]", b"", raw)
    if len(digits) % 2:
        digits += b"0"
    try:
        value = bytes.fromhex(digits.decode("ascii"))
    except ValueError:  # pragma: no cover - guarded by the regex
        return ""
    if value.startswith(b"\xfe\xff"):
        return value[2:].decode("utf-16-be", "replace")
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return value.decode("latin-1", "replace")


def _string_value(token: bytes) -> str:
    if token.startswith(b"("):
        return _decode_literal(token[1:-1])
    if token.startswith(b"<"):
        return _decode_hex(token[1:-1])
    return ""


def _page_lines(
    stream: bytes,
    *,
    objects: dict[int, bytes] | None = None,
    xobjects: dict[str, int] | None = None,
    depth: int = 0,
    seen: frozenset[int] = frozenset(),
    budget: list[int] | None = None,
) -> list[str]:
    """Replay a content stream and return its lines in reading order.

    ``objects``/``xobjects`` let ``Do`` descend into a ``Form`` XObject, whose
    own text operators belong at that exact position in the page. The walk stops
    at :data:`_MAX_FORM_DEPTH` and never revisits a form it is already inside, so
    a self-referencing document cannot loop.
    """

    lines: list[str] = []
    parts: list[str] = []
    operands: list[bytes | _Array] = []
    array: list[bytes] | None = None
    space = False
    last_y: float | None = None
    resume = 0

    def flush() -> None:
        nonlocal parts, space
        text = " ".join("".join(parts).split())
        if text:
            lines.append(text)
        parts = []
        space = False

    def emit(fragment: str) -> None:
        nonlocal space
        if not fragment:
            return
        if space and parts and not parts[-1].endswith(" "):
            parts.append(" ")
        parts.append(fragment)
        space = False

    def number(value: bytes | _Array) -> float | None:
        if isinstance(value, _Array):
            return None
        try:
            return float(value)
        except ValueError:
            return None

    for match in _TOKEN_RE.finditer(stream):
        if match.start() < resume:
            continue
        token = match.group(0)
        if token.startswith(b"%"):
            continue
        if token == b"[":
            array = []
            continue
        if array is not None:
            if token == b"]":
                operands.append(_Array(array))
                array = None
            else:
                array.append(token)
            continue
        if token == b"BI":
            # Inline image: the payload is opaque, so skip to the ``EI`` that
            # closes it instead of tokenising binary bytes as text operators.
            skipped = _skip_inline_image(stream, match.end())
            if skipped is not None:
                resume = skipped
                operands = []
            continue
        if token.startswith((b"(", b"<", b"/")):
            operands.append(token)
            continue
        if _NUMBER_RE.fullmatch(token):
            operands.append(token)
            continue

        if token == b"Tj":
            for operand in reversed(operands):
                if isinstance(operand, bytes) and operand.startswith((b"(", b"<")):
                    emit(_string_value(operand))
                    break
        elif token in (b"'", b'"'):
            flush()
            for operand in reversed(operands):
                if isinstance(operand, bytes) and operand.startswith((b"(", b"<")):
                    emit(_string_value(operand))
                    break
        elif token == b"TJ":
            for operand in reversed(operands):
                if not isinstance(operand, _Array):
                    continue
                for item in operand.items:
                    if item.startswith((b"(", b"<")):
                        emit(_string_value(item))
                    else:
                        kern = number(item)
                        if kern is not None and kern < -120:
                            space = True
                break
        elif token in (b"Td", b"TD"):
            values = [value for value in (number(op) for op in operands) if value is not None]
            if len(values) >= 2 and abs(values[-1]) > 0.5:
                flush()
            elif values:
                space = True
        elif token == b"Tm":
            values = [value for value in (number(op) for op in operands) if value is not None]
            if values:
                y = values[-1]
                if last_y is not None and abs(y - last_y) > 0.5:
                    flush()
                elif parts:
                    space = True
                last_y = y
        elif token == b"T*":
            flush()
        elif token == b"Do":
            name = next(
                (
                    operand
                    for operand in reversed(operands)
                    if isinstance(operand, bytes) and operand.startswith(b"/")
                ),
                None,
            )
            if name is not None and objects is not None and xobjects is not None:
                reference = xobjects.get(name[1:].decode("latin-1"))
                if (
                    reference is not None
                    and reference not in seen
                    and depth < _MAX_FORM_DEPTH
                ):
                    form = objects.get(reference)
                    payload = (
                        _decode_stream(form, budget=budget) if form is not None else None
                    )
                    if payload is not None:
                        # The form's text belongs here, in stream order.
                        flush()
                        lines.extend(
                            _page_lines(
                                payload,
                                objects=objects,
                                xobjects=_xobject_map(objects, form),
                                depth=depth + 1,
                                seen=seen | {reference},
                                budget=budget,
                            )
                        )
                        last_y = None
        elif token == b"ET":
            flush()
            last_y = None
        operands = []

    flush()
    return lines


def _page_order(objects: dict[int, bytes]) -> list[int]:
    """Page object numbers in document order, from the ``/Pages`` tree."""

    kids: dict[int, list[int]] = {}
    referenced: set[int] = set()
    for number, body in objects.items():
        if _PAGES_TYPE_RE.search(_header(body)) is None:
            continue
        match = _KIDS_RE.search(_header(body))
        if match is None:
            continue
        children = [int(ref) for ref in _REF_RE.findall(match.group(1))]
        kids[number] = children
        referenced.update(children)

    roots = sorted(set(kids) - referenced)
    ordered: list[int] = []
    seen: set[int] = set()

    def walk(number: int) -> None:
        if number in seen:
            return
        seen.add(number)
        if number in kids:
            for child in kids[number]:
                walk(child)
        elif _PAGE_TYPE_RE.search(_header(objects.get(number, b""))):
            ordered.append(number)

    for root in roots:
        walk(root)

    if ordered:
        return ordered
    # No usable page tree: object order is the only order available.
    return [
        number
        for number in sorted(objects)
        if _PAGE_TYPE_RE.search(_header(objects[number])) is not None
    ]


def _xobject_map(
    objects: dict[int, bytes], body: bytes, *, depth: int = 0
) -> dict[str, int]:
    """Name -> object number for the ``Form`` XObjects reachable from ``body``.

    ``/Resources`` may be inline, an indirect dictionary, or inherited from a
    ``/Parent``; all three are followed. Image XObjects are dropped: they hold
    no text operators, and their bytes are not text.
    """

    header = _header(body)
    inner: bytes | None = None
    match = _XOBJECT_RE.search(header)
    if match is not None:
        inner = match.group(1)
    else:
        ref = _XOBJECT_REF_RE.search(header)
        if ref is not None:
            numbers = _REF_RE.findall(ref.group(1))
            target = objects.get(int(numbers[0])) if numbers else None
            if target is not None:
                nested = _XOBJECT_RE.search(_header(target))
                if nested is not None:
                    inner = nested.group(1)

    if inner is None:
        if depth >= _MAX_RESOURCE_DEPTH:
            return {}
        parent = _PARENT_RE.search(header)
        if parent is None:
            return {}
        numbers = _REF_RE.findall(parent.group(1))
        target = objects.get(int(numbers[0])) if numbers else None
        if target is None:
            return {}
        return _xobject_map(objects, target, depth=depth + 1)

    mapping: dict[str, int] = {}
    for name, reference in _NAME_REF_RE.findall(inner):
        numbers = _REF_RE.findall(reference)
        if not numbers:
            continue
        target = objects.get(int(numbers[0]))
        if target is None:
            continue
        if _FORM_SUBTYPE_RE.search(_header(target)) is None:
            continue
        mapping[name[1:].decode("latin-1")] = int(numbers[0])
    return mapping


def _contents(objects: dict[int, bytes], page: int) -> list[bytes]:
    """Content-stream object bodies for one page, in stream order."""

    match = _CONTENTS_RE.search(_header(objects.get(page, b"")))
    if match is None:
        return []
    refs = [int(ref) for ref in _REF_RE.findall(match.group(1))]
    streams: list[bytes] = []
    for ref in refs:
        target = objects.get(ref)
        if target is None:
            continue
        if _STREAM_RE.search(target) is not None:
            streams.append(target)
            continue
        # ``/Contents`` may point at an array object rather than a stream.
        for extra in _REF_RE.findall(_header(target)):
            nested = objects.get(int(extra))
            if nested is not None and _STREAM_RE.search(nested) is not None:
                streams.append(nested)
    return streams


def extract_pdf(data: bytes) -> list[list[str]]:
    """Return ``pages[i]`` = lines on page ``i + 1``.

    Empty pages are preserved so a citation's page number never drifts.
    """

    if not data.startswith(b"%PDF-"):
        raise ValueError("artifact is not a PDF")
    if len(data) > MAX_ARTIFACT_BYTES:
        raise ValueError("artifact is larger than the reader allows")

    # One budget for the whole document: object streams, page content and the
    # Form XObjects they pull in all pay into the same allowance.
    budget = [_TOTAL_DECOMPRESSED_BYTES]
    objects = _objects(data, budget=budget)
    order = _page_order(objects)

    pages: list[list[str]] = []
    for page in order:
        lines: list[str] = []
        xobjects = _xobject_map(objects, objects.get(page, b""))
        for stream in _contents(objects, page):
            payload = _decode_stream(stream, budget=budget)
            if payload is not None:
                lines.extend(
                    _page_lines(
                        payload,
                        objects=objects,
                        xobjects=xobjects,
                        budget=budget,
                    )
                )
        pages.append(lines)

    if not pages:
        # No page tree at all: one readable stream still deserves an answer,
        # and inventing page numbers would make citations wrong.
        fallback: list[str] = []
        for _, body in sorted(objects.items()):
            if _STREAM_RE.search(body) is None:
                continue
            payload = _decode_stream(body, budget=budget)
            if payload is not None:
                fallback.extend(_page_lines(payload, objects=objects, budget=budget))
        if not fallback:
            raise ValueError("PDF has no readable text")
        pages = [fallback]
    return pages


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

_CELL_NUMERIC_RE = re.compile(r"^[+-]?(?:[$€£]\s*)?\(?-?[0-9][0-9,]*(?:\.[0-9]+)?%?\)?$")


def _sniff_delimiter(sample: str) -> str:
    counts = {candidate: sample.count(candidate) for candidate in (",", ";", "\t")}
    best = max(counts, key=counts.get)
    return best if counts[best] else ","


def read_csv(data: bytes) -> CsvTable:
    """Parse CSV bytes into a header and records; row 1 is the header.

    Every failure surfaces as ``ValueError`` with a fixed message: the caller
    turns that into one user-facing sentence, so the raw exception (which can
    quote file content) never escapes. ``csv.Error`` is included — a malformed
    quote, a NUL byte, or a cell past the parser's field limit must not become
    an unhandled 500.
    """

    if len(data) > MAX_ARTIFACT_BYTES:
        raise ValueError("artifact is larger than the reader allows")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV is not valid UTF-8") from exc

    first = next((line for line in text.splitlines() if line.strip()), "")
    delimiter = _sniff_delimiter(first)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        rows = [[cell.strip() for cell in row] for row in reader]
    except csv.Error as exc:
        raise ValueError("CSV could not be parsed") from exc
    rows = [row for row in rows if any(cell for cell in row)]
    if not rows:
        raise ValueError("CSV is empty")
    header = tuple(rows[0])
    body = tuple(tuple(row) for row in rows[1:])
    return CsvTable(header=header, rows=body, delimiter=delimiter)


def numeric_value(cell: str) -> float | None:
    """Return the numeric value of a cell, or ``None`` when it is not one."""

    text = cell.strip()
    if not text or not _CELL_NUMERIC_RE.match(text):
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = text.strip("()").replace(",", "").replace("%", "")
    cleaned = cleaned.replace("$", "").replace("€", "").replace("£", "").strip()
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return -value if negative else value

"""KEL-13 M2-C: real `docs.answer` retrieval over local artifacts.

Everything here is offline by construction: an autouse fixture replaces the
socket layer with a tripwire, and every artifact is generated in ``tmp_path``.
The suite covers per-page PDF extraction, the CSV row model, citation
locators, argument/security failures, the output-schema contract, and the
200-page performance envelope from the acceptance criteria.
"""

from __future__ import annotations

import asyncio
import csv
import json
import socket
import time
import zlib
from pathlib import Path

import pytest

from skills.builtin.docs_answer import embeddings, extract as extract_module
from skills.builtin.docs_answer.chunking import CLIP_CHARS, chunk_csv, chunk_pdf, clip_text
from skills.builtin.docs_answer.handler import run
from skills.builtin.docs_answer.extract import extract_pdf, numeric_value, read_csv
from skills.builtin.docs_answer.retrieval import (
    ScoredChunk,
    score_chunks,
    select_chunks,
    token_count,
)
from skills.context import SkillContext
from skills.errors import SkillHandlerError
from skills.loader import SkillRegistry

BUILTIN_ROOT = Path(__file__).resolve().parents[1] / "skills" / "builtin"
HANDLE = "artifact:abcdefgh1234"


# ---------------------------------------------------------------------------
# offline guarantees + generated artifacts
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any outward connection attempt fails the test, not just the request.

    Only ``connect``/``getaddrinfo`` are trapped: asyncio's event loop builds
    local self-pipes with ``socketpair`` and must keep working.
    """

    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("docs.answer attempted network access")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    embeddings.reset_embedder()


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _content_stream(lines: list[str]) -> bytes:
    parts = ["BT", "/F1 12 Tf", "72 720 Td", "16 TL"]
    for line in lines:
        parts.append(f"({_escape(line)}) Tj")
        parts.append("T*")
    parts.append("ET")
    return "\n".join(parts).encode("latin-1")


def make_pdf(pages: list[list[str]], *, compress: bool = True) -> bytes:
    """Write a small but structurally real PDF with a page tree."""

    page_numbers = [3 + 2 * index for index in range(len(pages))]
    kids = " ".join(f"{number} 0 R" for number in page_numbers)
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("ascii"),
    ]
    for index, lines in enumerate(pages):
        payload = _content_stream(lines)
        filter_entry = b""
        if compress:
            payload = zlib.compress(payload)
            filter_entry = b" /Filter /FlateDecode"
        content_number = page_numbers[index] + 1
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Contents {content_number} 0 R >>"
            ).encode("ascii")
        )
        objects.append(
            b"<< /Length %d%s >>\nstream\n" % (len(payload), filter_entry)
            + payload
            + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    for number, body in enumerate(objects, start=1):
        out += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    out += b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    return bytes(out)


def workspace(tmp_path: Path, files: dict[str, bytes]) -> tuple[SkillContext, Path]:
    root = tmp_path / "ws"
    artifacts = root / "var" / "artifacts"
    artifacts.mkdir(parents=True)
    for name, data in files.items():
        (artifacts / name).write_bytes(data)
    return SkillContext("docs.answer", root), root


def ask(context: SkillContext, kind: str, question: str, **extra: object) -> dict:
    arguments = {"artifact": HANDLE, "kind": kind, "question": question, **extra}
    return dict(asyncio.run(run(arguments, context)))


def make_pdf_from_objects(bodies: dict[int, bytes]) -> bytes:
    """Assemble a PDF from explicit object bodies (content streams included)."""

    out = bytearray(b"%PDF-1.4\n")
    for number in sorted(bodies):
        out += f"{number} 0 obj\n".encode("ascii") + bodies[number] + b"\nendobj\n"
    out += b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    return bytes(out)


def stream_object(payload: bytes, extra: bytes = b"") -> bytes:
    head = b"<< /Length %d%s >>\nstream\n" % (len(payload), extra)
    return head + payload + b"\nendstream"


# ---------------------------------------------------------------------------
# PDF extraction
# ---------------------------------------------------------------------------


def test_pdf_extraction_keeps_pages_in_document_order() -> None:
    pages = extract_pdf(
        make_pdf(
            [
                ["Pricing overview", "H200 costs $2.49 per GPU-hour."],
                ["Support", "Production support response target is 30 minutes."],
                ["Regions", "eu-north-1 lists H200 capacity as available."],
            ]
        )
    )
    assert len(pages) == 3
    assert pages[0][0] == "Pricing overview"
    assert "30 minutes" in pages[1][1]
    assert pages[2][1].startswith("eu-north-1")


def test_pdf_extracts_tj_arrays_and_hex_strings() -> None:
    body = (
        b"BT /F1 12 Tf 72 720 Td "
        b"[(Quarterly) -300 (revenue) -300 (grew)] TJ T* "
        b"<556E697420636F73747320617265206C6F7765722E> Tj ET"
    )
    data = (
        b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>\nendobj\n"
        b"4 0 obj\n<< /Length " + str(len(body)).encode("ascii") + b" >>\nstream\n"
        + body
        + b"\nendstream\nendobj\n"
        b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    )
    pages = extract_pdf(data)
    assert pages == [["Quarterly revenue grew", "Unit costs are lower."]]


def test_flate_stream_expansion_is_bounded() -> None:
    from skills.builtin.docs_answer.extract import _decode_stream

    payload = zlib.compress(b"\0" * (33 * 1024 * 1024), 9)
    body = (
        b"<< /Length " + str(len(payload)).encode() + b" /Filter /FlateDecode >>\nstream\n"
        + payload
        + b"\nendstream"
    )
    assert _decode_stream(body) is None


def test_total_decompression_budget_stops_the_third_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Per-stream caps alone would let every page through; the document-wide
    # budget is what makes a many-small-bombs PDF finite.
    monkeypatch.setattr(extract_module, "_TOTAL_DECOMPRESSED_BYTES", 60)
    pages = extract_pdf(make_pdf([["Alpha"], ["Beta"], ["Gamma"]], compress=False))

    assert pages[0] == ["Alpha"]
    # The remaining allowance cannot cover another stream, so later pages stay
    # empty instead of being decoded anyway.
    assert pages[1] == []
    assert pages[2] == []


def test_a_rejected_bomb_still_spends_the_document_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skills.builtin.docs_answer.extract import _decode_stream

    monkeypatch.setattr(extract_module, "_MAX_DECOMPRESSED_BYTES", 512)
    payload = zlib.compress(b"\0" * (2 * 1024 * 1024), 9)
    body = (
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(payload)
        + payload
        + b"\nendstream"
    )
    budget = [1024]

    # The stream is refused (it would exceed the per-stream cap) but the bytes
    # it did inflate are charged, so the bomb cannot be retried for free.
    assert _decode_stream(body, budget=budget) is None
    assert budget[0] == 1024 - 512


def test_inline_image_payload_is_never_read_as_text() -> None:
    # The image bytes spell out a complete text operator and a sentence. If the
    # scanner tokenises them, they become a fabricated citation.
    injected = b"\xff\x00(Injected secret pricing) Tj (more injected) Tj "
    content = (
        b"BT /F1 12 Tf 72 720 Td (Real page text) Tj ET\n"
        b"q 100 0 0 100 0 0 cm BI /W 4 /H 4 /BPC 8 /CS /G ID " + injected + b" EI Q\n"
    )
    data = make_pdf_from_objects(
        {
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>",
            4: stream_object(content),
        }
    )

    pages = extract_pdf(data)

    assert pages == [["Real page text"]]
    assert "Injected" not in json.dumps(pages)


def test_inline_image_asked_about_returns_no_fabricated_answer(tmp_path: Path) -> None:
    injected = b"\xff\x00(The injected secret answer is 42.) Tj "
    content = (
        b"BT /F1 12 Tf 72 720 Td (Real page text) Tj ET\n"
        b"q BI /W 4 /H 4 /BPC 8 /CS /G ID " + injected + b" EI Q\n"
    )
    data = make_pdf_from_objects(
        {
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>",
            4: stream_object(content),
        }
    )
    context, _ = workspace(tmp_path, {"abcdefgh1234.pdf": data})

    output = ask(context, "pdf", "injected secret answer")

    assert "Injected" not in json.dumps(output)
    assert "42" not in output["answer"]
    assert output["answer"] == "Real page text"
    assert [citation["locator"] for citation in output["citations"]] == ["page 1"]


def test_form_xobject_text_is_followed_in_place() -> None:
    page_content = (
        b"BT /F1 12 Tf 72 720 Td (Before form) Tj ET "
        b"q /Fm0 Do Q BT (After form) Tj ET"
    )
    form_content = b"BT /F1 12 Tf 72 700 Td (Inside form) Tj ET"
    compressed = zlib.compress(form_content)
    data = make_pdf_from_objects(
        {
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: (
                b"<< /Type /Page /Parent 2 0 R "
                b"/Resources << /XObject << /Fm0 5 0 R >> >> /Contents 4 0 R >>"
            ),
            4: stream_object(page_content),
            5: stream_object(
                compressed,
                b" /Type /XObject /Subtype /Form /BBox [0 0 612 792] "
                b"/Resources << >> /Filter /FlateDecode",
            ),
        }
    )

    assert extract_pdf(data) == [["Before form", "Inside form", "After form"]]


def test_form_xobject_nesting_stops_at_the_depth_cap() -> None:
    forms: dict[int, bytes] = {}
    for level in range(1, 6):
        content = b"BT (Level %d) Tj ET" % level
        if level < 5:
            content += b" /Fm%d Do" % (level + 1)
            resources = b" /Resources << /XObject << /Fm%d %d 0 R >> >>" % (
                level + 1,
                level + 11,
            )
        else:
            resources = b" /Resources << >>"
        forms[level + 10] = stream_object(
            content,
            b" /Type /XObject /Subtype /Form /BBox [0 0 612 792]" + resources,
        )
    bodies: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (
            b"<< /Type /Page /Parent 2 0 R "
            b"/Resources << /XObject << /Fm1 11 0 R >> >> /Contents 4 0 R >>"
        ),
        4: stream_object(b"BT (Page) Tj ET /Fm1 Do"),
        **forms,
    }

    lines = extract_pdf(make_pdf_from_objects(bodies))[0]

    assert lines[0] == "Page"
    assert lines[1:] == ["Level 1", "Level 2", "Level 3", "Level 4"]


_FORM_HEAD = b" /Type /XObject /Subtype /Form /BBox [0 0 612 792]"


def _form_fanout_pdf(*, fan_out: int, levels: int) -> bytes:
    """Page -> F0 x N -> F1 x N -> ... -> leaf form that shows one text line."""

    bodies: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (
            b"<< /Type /Page /Parent 2 0 R "
            b"/Resources << /XObject << /Fm 10 0 R >> >> /Contents 4 0 R >>"
        ),
        4: stream_object(b"/Fm Do " * fan_out),
    }
    for level in range(levels):
        number = 10 + level
        if level + 1 < levels:
            content = b"/Fm Do " * fan_out
            resources = b" /Resources << /XObject << /Fm %d 0 R >> >>" % (number + 1)
        else:
            content = b"BT (leaf) Tj ET"
            resources = b" /Resources << >>"
        bodies[number] = stream_object(content, _FORM_HEAD + resources)
    return make_pdf_from_objects(bodies)


def test_form_used_twice_is_extracted_twice() -> None:
    data = make_pdf_from_objects(
        {
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: (
                b"<< /Type /Page /Parent 2 0 R "
                b"/Resources << /XObject << /Fm0 5 0 R >> >> /Contents 4 0 R >>"
            ),
            4: stream_object(b"BT (A) Tj ET /Fm0 Do BT (B) Tj ET /Fm0 Do"),
            5: stream_object(
                zlib.compress(b"BT (Inside) Tj ET"),
                _FORM_HEAD + b" /Resources << >> /Filter /FlateDecode",
            ),
        }
    )

    assert extract_pdf(data) == [["A", "Inside", "B", "Inside"]]


def test_form_decoded_once_per_document(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _form_fanout_pdf(fan_out=3, levels=2)
    real = extract_module._decode_stream
    form_decodes = 0

    def counting(body: bytes, **kwargs: object) -> bytes | None:
        nonlocal form_decodes
        if b"/Subtype /Form" in body:
            form_decodes += 1
        return real(body, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(extract_module, "_decode_stream", counting)

    lines = extract_pdf(data)[0]

    assert lines == ["leaf"] * 9  # behaviour preserved: every invocation is read
    assert form_decodes == 2  # one decode per form object, not per invocation


def test_form_fanout_is_bounded_in_time_and_output() -> None:
    data = _form_fanout_pdf(fan_out=20, levels=4)  # 20**4 leaf invocations
    assert len(data) < 4096

    started = time.monotonic()
    pages = extract_pdf(data)
    elapsed = time.monotonic() - started

    assert elapsed < 10.0  # generous for slow CI; the counts below are the real check
    assert len(pages) == 1
    assert pages[0]  # partial output survives the cap
    assert len(pages[0]) <= extract_module._MAX_FORM_INVOCATIONS
    assert set(pages[0]) == {"leaf"}


def test_one_form_repeated_many_times_is_capped() -> None:
    bodies = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 2 >>",
        3: (
            b"<< /Type /Page /Parent 2 0 R "
            b"/Resources << /XObject << /Fm0 5 0 R >> >> /Contents 4 0 R >>"
        ),
        4: stream_object(zlib.compress(b"/Fm0 Do " * 100_000), b" /Filter /FlateDecode"),
        5: stream_object(b"BT (x) Tj ET", _FORM_HEAD + b" /Resources << >>"),
    }
    started = time.monotonic()
    pages = extract_pdf(make_pdf_from_objects(bodies))
    elapsed = time.monotonic() - started

    assert elapsed < 10.0  # generous for slow CI; the counts below are the real check
    assert 0 < len(pages[0]) <= extract_module._MAX_FORM_INVOCATIONS < 100_000


def test_token_cap_truncates_a_flat_content_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(extract_module, "_MAX_CONTENT_TOKENS", 100)
    content = b"".join(b"BT (line %d) Tj ET\n" % index for index in range(1000))
    data = make_pdf_from_objects(
        {
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>",
            4: stream_object(content),
        }
    )

    lines = extract_pdf(data)[0]

    assert lines[0] == "line 0"
    assert 0 < len(lines) <= 100 // 4  # BT ( ) Tj ET = 4 tokens per line


def test_work_cap_leaves_later_pages_in_place_but_empty() -> None:
    bodies = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 >>",
        3: (
            b"<< /Type /Page /Parent 2 0 R "
            b"/Resources << /XObject << /Fm0 5 0 R >> >> /Contents 4 0 R >>"
        ),
        4: stream_object(b"/Fm0 Do " * (extract_module._MAX_FORM_INVOCATIONS + 10)),
        5: stream_object(b"BT (x) Tj ET", _FORM_HEAD + b" /Resources << >>"),
        6: b"<< /Type /Page /Parent 2 0 R /Contents 7 0 R >>",
        7: stream_object(b"BT (Second page) Tj ET"),
    }

    pages = extract_pdf(make_pdf_from_objects(bodies))

    assert len(pages) == 2  # page numbering never drifts
    assert pages[1] == []  # the cap is document-wide, so nothing more is read


def test_oversized_artifact_is_refused_before_reading(tmp_path: Path) -> None:
    context, root = workspace(tmp_path, {})
    oversized = root / "var" / "artifacts" / "abcdefgh1234.csv"
    with oversized.open("wb") as handle:
        handle.truncate(65 * 1024 * 1024)

    with pytest.raises(SkillHandlerError) as caught:
        asyncio.run(run({"artifact": HANDLE, "kind": "csv", "question": "total"}, context))
    assert caught.value.code == "handler_error"
    assert "larger than" in caught.value.message


def test_pdf_rejects_non_pdf_bytes() -> None:
    with pytest.raises(ValueError):
        extract_pdf(b"not a pdf at all")


def test_pdf_answer_cites_the_page_that_holds_it(tmp_path: Path) -> None:
    data = make_pdf(
        [
            ["Pricing overview", "H200 costs $2.49 per GPU-hour."],
            ["Support", "Production support response target is 30 minutes."],
            ["Regions", "eu-north-1 lists H200 capacity as available."],
        ]
    )
    context, _ = workspace(tmp_path, {"abcdefgh1234.pdf": data})

    output = ask(context, "pdf", "What is the support response target?")

    assert "30 minutes" in output["answer"]
    assert output["artifact"] == {
        "handle": HANDLE,
        "kind": "pdf",
        "mime": "application/pdf",
        "pages": 3,
    }
    assert output["citations"]
    assert {citation["locator"] for citation in output["citations"]} == {"page 2"}
    # Citations quote the source page, they do not restate the answer.
    page_two = " ".join(extract_pdf(data)[1])
    assert output["citations"][0]["text"] in page_two


def test_pdf_without_a_match_falls_back_to_the_first_page(tmp_path: Path) -> None:
    context, _ = workspace(
        tmp_path,
        {"abcdefgh1234.pdf": make_pdf([["Overview", "The first fact."], ["More", "Second page."]])},
    )

    output = ask(context, "pdf", "zzzz qqqq")

    assert output["citations"][0]["locator"] == "page 1"
    assert output["citations"][0]["text"] == "Overview The first fact."


# ---------------------------------------------------------------------------
# CSV row model
# ---------------------------------------------------------------------------


def test_csv_row_model_numbers_rows_from_the_header() -> None:
    table = read_csv(b"region,quarter,revenue_usd,orders\nemea,Q3,412300,1840\napac,Q3,135300,612\n")
    assert table.header == ("region", "quarter", "revenue_usd", "orders")
    assert table.rows[0] == ("emea", "Q3", "412300", "1840")
    assert len(table.rows) == 2
    chunks = chunk_csv(table)
    assert chunks[0].locator == "rows 2-3"
    assert chunks[0].rows == ((2, "emea,Q3,412300,1840"), (3, "apac,Q3,135300,612"))


def test_csv_semicolon_delimiter_and_quoted_commas() -> None:
    table = read_csv(b'region;note;amount\nemea;"Q3, strong";100\n')
    assert table.delimiter == ";"
    assert table.rows == (("emea", "Q3, strong", "100"),)


def test_csv_numeric_cells() -> None:
    assert numeric_value("1,284,500") == 1284500.0
    assert numeric_value("$2.49") == 2.49
    assert numeric_value("(300)") == -300.0
    assert numeric_value("12%") == 12.0
    assert numeric_value("n/a") is None
    assert numeric_value("") is None


def test_csv_parse_errors_become_a_plain_value_error() -> None:
    previous = csv.field_size_limit()
    csv.field_size_limit(4)
    try:
        with pytest.raises(ValueError) as caught:
            read_csv(b"region,revenue\nemea,412300\n")
    finally:
        csv.field_size_limit(previous)

    # Fixed sentence: the raw csv.Error text is kept only as the cause, so it
    # can never surface to a caller.
    assert str(caught.value) == "CSV could not be parsed"
    assert isinstance(caught.value.__cause__, csv.Error)


def test_csv_parse_error_on_the_answer_path_is_user_fixable(
    tmp_path: Path,
) -> None:
    context, _ = workspace(tmp_path, {"abcdefgh1234.csv": b"region,revenue\nemea,412300\n"})
    ask(context, "csv", "total revenue")  # warm the index while parsing works

    previous = csv.field_size_limit()
    csv.field_size_limit(4)
    try:
        # The cached index still loads; the re-parse behind the answer does not.
        with pytest.raises(SkillHandlerError) as caught:
            ask(context, "csv", "total revenue")
    finally:
        csv.field_size_limit(previous)

    assert caught.value.code == "handler_error"
    assert caught.value.user_fixable is True
    assert "could not be answered" in caught.value.message
    assert "field larger" not in caught.value.message


def test_oversized_csv_cells_are_clipped_in_chunks_and_citations(
    tmp_path: Path,
) -> None:
    note = " ".join(["revenue"] * 13000)
    assert 100 * 1024 < len(note) < 128 * 1024  # ~100 KiB, still one legal cell
    data = ("id,note\n1," + note + "\n").encode()
    context, root = workspace(tmp_path, {"abcdefgh1234.csv": data})

    output = ask(context, "csv", "revenue")

    assert output["citations"], output
    citation = output["citations"][0]
    assert citation["locator"] == "row 2"
    assert len(citation["text"]) <= CLIP_CHARS + 1
    assert citation["text"].endswith("…")
    # The 100 KiB cell must not reach the response or blow the token budget.
    assert len(output["answer"]) <= CLIP_CHARS + 16
    assert token_count(output["answer"]) < 200

    payload = json.loads(next((root / "var" / "index").rglob("*.json")).read_text())
    assert payload["chunks"]
    for chunk in payload["chunks"]:
        assert len(chunk["text"]) <= CLIP_CHARS + 1
    assert token_count(" ".join(chunk["text"] for chunk in payload["chunks"])) < 1200


def test_csv_cell_over_the_parser_limit_is_a_plain_fixable_error(
    tmp_path: Path,
) -> None:
    # Beyond csv's field limit there is no row to clip; the request must fail
    # with one sentence, not leak the parser's exception text.
    data = ("id,note\n1," + "x" * 200_000 + "\n").encode()
    context, _ = workspace(tmp_path, {"abcdefgh1234.csv": data})

    with pytest.raises(SkillHandlerError) as caught:
        ask(context, "csv", "note")

    assert caught.value.code == "handler_error"
    assert caught.value.user_fixable is True
    assert "could not be read" in caught.value.message
    assert "field larger" not in caught.value.message


def test_csv_total_names_rows_and_quotes_them(tmp_path: Path) -> None:
    data = b"region,quarter,revenue_usd,orders\nemea,Q3,412300,1840\nnorth_america,Q3,736900,2975\napac,Q3,135300,612\n"
    context, _ = workspace(tmp_path, {"abcdefgh1234.csv": data})

    output = ask(context, "csv", "What was the total revenue?")

    assert output["answer"] == "Total revenue_usd across 3 rows is 1,284,500."
    assert output["artifact"]["rows"] == 3
    assert output["artifact"]["mime"] == "text/csv; charset=utf-8"
    assert output["citations"] == [
        {"locator": "row 2", "text": "emea,Q3,412300,1840"},
        {"locator": "row 3", "text": "north_america,Q3,736900,2975"},
        {"locator": "row 4", "text": "apac,Q3,135300,612"},
    ]


def test_csv_maximum_names_the_winning_row(tmp_path: Path) -> None:
    data = b"region,revenue_usd,orders\nemea,412300,1840\nnorth_america,736900,2975\napac,135300,612\n"
    context, _ = workspace(tmp_path, {"abcdefgh1234.csv": data})

    output = ask(context, "csv", "Which region had the highest orders?")

    assert "Maximum orders across 3 rows is 2,975." in output["answer"]
    assert "Row 3: north_america,736900,2975." in output["answer"]


def test_csv_max_rows_caps_citations_but_not_the_total(tmp_path: Path) -> None:
    lines = ["amount"] + [str(index * 100) for index in range(1, 31)]
    context, _ = workspace(tmp_path, {"abcdefgh1234.csv": ("\n".join(lines) + "\n").encode()})

    output = ask(context, "csv", "total amount", max_rows=5)

    assert "across 30 rows is 46,500" in output["answer"]
    assert len(output["citations"]) == 5
    assert [citation["locator"] for citation in output["citations"]] == [
        "row 2",
        "row 3",
        "row 4",
        "row 5",
        "row 6",
    ]


def test_citation_count_never_exceeds_the_manifest_ceiling(tmp_path: Path) -> None:
    lines = ["amount"] + [str(index) for index in range(1, 61)]
    context, _ = workspace(tmp_path, {"abcdefgh1234.csv": ("\n".join(lines) + "\n").encode()})

    output = ask(context, "csv", "total amount", max_rows=200)

    assert "across 60 rows" in output["answer"]
    assert len(output["citations"]) == 50  # manifest citations.maxItems


def test_csv_question_without_a_match_does_not_invent_rows(tmp_path: Path) -> None:
    data = b"region,revenue_usd\nemea,412300\napac,135300\n"
    context, _ = workspace(tmp_path, {"abcdefgh1234.csv": data})

    output = ask(context, "csv", "zzzz qqqq")

    assert output["answer"] == "The file has 2 data rows across 2 columns."
    assert output["citations"] == []


# ---------------------------------------------------------------------------
# CSV aggregate row filters
# ---------------------------------------------------------------------------

REGION_DATA = b"region,revenue\nEast,100\nWest,900\nNorth,50\n"
EVAL_DATA = (
    "region,product,units,revenue\n"
    "North,GPU cluster,12,48000\n"
    "South,Storage tier,30,15000\n"
    "East,Inference API,20,64000\n"
    "West,Support plan,7,9000\n"
).encode()


def test_csv_total_is_restricted_to_the_filtered_region(tmp_path: Path) -> None:
    context, _ = workspace(tmp_path, {"abcdefgh1234.csv": REGION_DATA})

    output = ask(context, "csv", "What is the revenue for the East region?")

    assert output["answer"] == "Total revenue across 1 rows where region matches east is 100."
    assert output["citations"] == [{"locator": "row 2", "text": "East,100"}]


def test_csv_eval_dataset_filter_cites_only_the_matching_row(tmp_path: Path) -> None:
    context, _ = workspace(tmp_path, {"abcdefgh1234.csv": EVAL_DATA})

    output = ask(context, "csv", "What is the total revenue for East?")

    assert output["answer"] == (
        "Total revenue across 1 rows where region matches east is 64,000."
    )
    assert output["citations"] == [{"locator": "row 4", "text": "East,Inference API,20,64000"}]


def test_csv_filter_term_matching_no_cell_is_ignored(tmp_path: Path) -> None:
    context, _ = workspace(tmp_path, {"abcdefgh1234.csv": EVAL_DATA})

    output = ask(context, "csv", "What is the total revenue across all regions?")

    assert output["answer"] == "Total revenue across 4 rows is 136,000."
    assert [citation["locator"] for citation in output["citations"]] == [
        "row 2",
        "row 3",
        "row 4",
        "row 5",
    ]


def test_csv_terms_in_one_column_are_alternatives(tmp_path: Path) -> None:
    context, _ = workspace(tmp_path, {"abcdefgh1234.csv": REGION_DATA})

    output = ask(context, "csv", "total revenue for East or West")

    assert output["answer"] == (
        "Total revenue across 2 rows where region matches east or west is 1,000."
    )
    assert [citation["locator"] for citation in output["citations"]] == ["row 2", "row 3"]


def test_csv_terms_in_different_columns_must_all_hold(tmp_path: Path) -> None:
    data = b"region,product,revenue\nEast,GPU,100\nEast,Storage,20\nWest,GPU,5\n"
    context, _ = workspace(tmp_path, {"abcdefgh1234.csv": data})

    output = ask(context, "csv", "total revenue for East GPU")

    assert output["answer"] == (
        "Total revenue across 1 rows where region matches east and product matches gpu is 100."
    )
    assert output["citations"] == [{"locator": "row 2", "text": "East,GPU,100"}]


def test_csv_term_present_in_two_columns_names_both_headers(tmp_path: Path) -> None:
    data = b"region,hub,revenue\nEast,West,100\nWest,North,20\nNorth,East,5\n"
    context, _ = workspace(tmp_path, {"abcdefgh1234.csv": data})

    output = ask(context, "csv", "total revenue for East")

    assert output["answer"] == (
        "Total revenue across 2 rows where region/hub matches east is 105."
    )
    assert [citation["locator"] for citation in output["citations"]] == ["row 2", "row 4"]


def test_csv_filter_with_no_passing_rows_says_so(tmp_path: Path) -> None:
    data = b"region,product,revenue\nEast,Storage,20\nWest,GPU,5\n"
    context, _ = workspace(tmp_path, {"abcdefgh1234.csv": data})

    output = ask(context, "csv", "total revenue for East GPU")

    assert output["answer"] == "No rows where region matches east and product matches gpu."
    assert output["citations"] == []


def test_csv_filter_with_rows_but_no_numeric_values_keeps_the_message(
    tmp_path: Path,
) -> None:
    data = b"region,revenue\nEast,\nWest,900\nNorth,50\n"
    context, _ = workspace(tmp_path, {"abcdefgh1234.csv": data})

    output = ask(context, "csv", "total revenue for East")

    assert output["answer"] == (
        "No numeric values found in column revenue where region matches east."
    )
    assert output["citations"] == []


def test_csv_count_reports_filtered_rows(tmp_path: Path) -> None:
    context, _ = workspace(tmp_path, {"abcdefgh1234.csv": REGION_DATA})

    output = ask(context, "csv", "How many revenue values for East?")

    assert output["answer"] == (
        "Column revenue has 1 numeric values across 1 rows where region matches east."
    )


def test_csv_maximum_names_the_winning_row_among_filtered_rows(tmp_path: Path) -> None:
    data = b"region,sales\nEast,10\nWest,900\nEast,70\n"
    context, _ = workspace(tmp_path, {"abcdefgh1234.csv": data})

    output = ask(context, "csv", "What was the highest sales in East?")

    assert output["answer"] == (
        "Maximum sales across 2 rows where region matches east is 70. Row 4: East,70."
    )
    assert [citation["locator"] for citation in output["citations"]] == ["row 2", "row 4"]


def test_csv_header_name_selects_the_column_and_is_not_a_filter(tmp_path: Path) -> None:
    context, _ = workspace(tmp_path, {"abcdefgh1234.csv": EVAL_DATA})

    output = ask(context, "csv", "total units for the East region")

    assert output["answer"] == "Total units across 1 rows where region matches east is 20."
    assert "units matches" not in output["answer"]
    assert "region matches region" not in output["answer"]


def test_clip_text_never_exceeds_the_limit_without_spaces() -> None:
    clipped = clip_text("x" * 5000)

    assert len(clipped) <= CLIP_CHARS
    assert clipped.endswith("…")


def test_clip_text_never_exceeds_the_limit_with_spaces() -> None:
    clipped = clip_text("word " * 500)

    assert len(clipped) <= CLIP_CHARS
    assert clipped.endswith("…")
    # A space exactly at the boundary must not push the result over either.
    for length in range(CLIP_CHARS - 3, CLIP_CHARS + 4):
        assert len(clip_text(("a" * (length - 1) + " ") * 3)) <= CLIP_CHARS


def test_clip_text_leaves_short_text_untouched() -> None:
    assert clip_text("  a   b ") == "a b"
    assert clip_text("y" * CLIP_CHARS) == "y" * CLIP_CHARS


@pytest.mark.parametrize("question", ["total", "count of rows"])
def test_csv_answer_clips_an_oversized_header(tmp_path: Path, question: str) -> None:
    header = "h" * 5000
    data = f"{header},label\n5,a\n7,b\n".encode()
    context, _ = workspace(tmp_path, {"abcdefgh1234.csv": data})

    output = ask(context, "csv", question)

    assert header not in output["answer"]
    assert len(output["answer"]) <= CLIP_CHARS + 64


# ---------------------------------------------------------------------------
# chunking and retrieval units
# ---------------------------------------------------------------------------


def test_pdf_chunks_carry_page_locators_and_respect_size() -> None:
    long_line = " ".join(f"word{index}" for index in range(400))
    pages = [["Short page."], [long_line], []]
    chunks = chunk_pdf(pages)

    assert chunks[0].locator == "page 1"
    assert all(chunk.locator == "page 2" for chunk in chunks[1:])
    assert all(len(chunk.text) <= 900 for chunk in chunks[1:])
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))
    # The empty page is preserved in numbering: page 3 has no chunk, so no
    # locator can silently shift to it.
    assert {chunk.page for chunk in chunks} == {1, 2}


def test_ties_break_on_document_order_not_input_order() -> None:
    index = {
        "header": [],
        "embedding_model": None,
        "chunks": [
            {"locator": "page 2", "ordinal": 1, "text": "alpha beta"},
            {"locator": "page 1", "ordinal": 0, "text": "alpha beta"},
        ],
    }
    ranked = score_chunks(index, "alpha beta")
    assert [item.chunk.locator for item in ranked] == ["page 1", "page 2"]
    assert ranked[0].score == ranked[1].score


def test_context_token_cap_drops_lower_ranked_chunks() -> None:
    chunk = [{"locator": "page 1", "ordinal": 0, "text": "alpha " * 40}] * 6
    index = {"header": [], "embedding_model": None, "chunks": chunk}
    ranked = score_chunks(index, "alpha")
    selected = select_chunks(ranked, top_k=5, token_cap=100)
    assert len(selected) == 2
    assert sum(token_count(item.chunk.text) for item in selected) <= 100
    # The best chunk survives even when it alone blows the budget.
    huge = [ScoredChunk(chunk=chunk_pdf([["one " * 2000]])[0], score=1.0)]
    assert len(select_chunks(huge, top_k=3, token_cap=50)) == 1


# ---------------------------------------------------------------------------
# embedding hook: optional, additive, and never required
# ---------------------------------------------------------------------------


def test_embedding_hook_can_reorder_results(tmp_path: Path) -> None:
    # "support" appears on every page, so lexical scores are close together and
    # page 5 wins on term frequency. A vector pointing at page 1 flips it.
    pages = [["Elsewhere", f"Zone {number} mentions support once."] for number in range(10)]
    pages.append(["Support", "Support support support."])
    context, _ = workspace(tmp_path, {"abcdefgh1234.pdf": make_pdf(pages)})
    lexical = ask(context, "pdf", "support")["citations"][0]["locator"]
    assert lexical == "page 11"

    # A toy local "model": the first chunk aligns with the query.
    embeddings.set_embedder(
        "fake-toy", lambda texts: [[1.0, 0.0] if index == 0 else [0.0, 1.0] for index, _ in enumerate(texts)]
    )
    output = ask(context, "pdf", "support")
    assert output["citations"][0]["locator"] == "page 1"

    index_files = list((tmp_path / "ws" / "var" / "index").rglob("*.json"))
    assert index_files
    payload = json.loads(index_files[0].read_text())
    assert payload["embedding_model"] == "fake-toy"
    assert payload["chunks"][0]["vector"] == [1.0, 0.0]


def test_broken_embedder_falls_back_to_lexical(tmp_path: Path) -> None:
    context, root = workspace(
        tmp_path,
        {"abcdefgh1234.pdf": make_pdf([["Support", "Support response target is 30 minutes."]])},
    )

    def broken(texts: list[str]) -> list[list[float]]:
        raise RuntimeError("model is not loaded")

    embeddings.set_embedder("broken", broken)
    output = ask(context, "pdf", "support response")

    assert "30 minutes" in output["answer"]
    payload = json.loads(next((root / "var" / "index").rglob("*.json")).read_text())
    assert payload["embedding_model"] is None
    assert payload["embedding_requested"] == "broken"
    assert "vector" not in payload["chunks"][0]


# ---------------------------------------------------------------------------
# determinism, secrets, and the manifest contract
# ---------------------------------------------------------------------------


def test_answers_are_byte_for_byte_deterministic(tmp_path: Path) -> None:
    data = make_pdf([["Support", "Support response target is 30 minutes."]])
    first_context, _ = workspace(tmp_path / "a", {"abcdefgh1234.pdf": data})
    second_context, _ = workspace(tmp_path / "b", {"abcdefgh1234.pdf": data})

    first = ask(first_context, "pdf", "What is the support response target?")
    second = ask(second_context, "pdf", "What is the support response target?")

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first == ask(first_context, "pdf", "What is the support response target?")


def test_secrets_never_reach_the_index_or_the_answer(tmp_path: Path) -> None:
    secret = "sk-docs-answer-super-secret"
    root = tmp_path / "ws"
    (root / "var" / "artifacts").mkdir(parents=True)
    (root / "var" / "artifacts" / "abcdefgh1234.csv").write_bytes(
        b"region,revenue_usd\nemea,412300\n"
    )
    context = SkillContext("docs.answer", root, {"DEMO_TOKEN": secret})

    output = ask(context, "csv", "total revenue")

    serialised = json.dumps(output)
    assert secret not in serialised
    index_text = "\n".join(path.read_text() for path in (root / "var" / "index").rglob("*.json"))
    assert secret not in index_text
    # The question is not persisted either.
    assert "total revenue" not in index_text


def test_registry_envelope_validates_against_the_output_schema(tmp_path: Path) -> None:
    registry = SkillRegistry.load(BUILTIN_ROOT)
    skill = registry.get("docs.answer")
    context, _ = workspace(
        tmp_path,
        {
            "abcdefgh1234.pdf": make_pdf([["Support", "Support response target is 30 minutes."]]),
            "abcdefgh5678.csv": b"region,revenue_usd\nemea,412300\n",
        },
    )

    for handle, kind, question in (
        (HANDLE, "pdf", "support response"),
        ("artifact:abcdefgh5678", "csv", "total revenue"),
    ):
        result = asyncio.run(
            registry.invoke(
                "docs.answer",
                {"artifact": handle, "kind": kind, "question": question},
                workspace_root=context.workspace_root,
            )
        )
        assert result["ok"] is True, result
        skill.output_validator.validate(result["output"])


# ---------------------------------------------------------------------------
# argument and path safety
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "field"),
    [
        ({"artifact": "nope", "kind": "pdf", "question": "q"}, "/artifact"),
        ({"artifact": "artifact:../../etc/passwd", "kind": "pdf", "question": "q"}, "/artifact"),
        ({"artifact": HANDLE, "kind": "xlsx", "question": "q"}, "/kind"),
        ({"artifact": HANDLE, "kind": {}, "question": "q"}, "/kind"),
        ({"artifact": HANDLE, "kind": ["pdf"], "question": "q"}, "/kind"),
        ({"artifact": HANDLE, "kind": "pdf", "question": "   "}, "/question"),
        ({"artifact": HANDLE, "kind": "pdf", "question": "q" * 1001}, "/question"),
        ({"artifact": HANDLE, "kind": "pdf", "question": "q", "max_rows": 0}, "/max_rows"),
        ({"artifact": HANDLE, "kind": "pdf", "question": "q", "max_rows": 201}, "/max_rows"),
        ({"artifact": HANDLE, "kind": "pdf", "question": "q", "max_rows": True}, "/max_rows"),
    ],
)
def test_invalid_arguments_are_user_fixable(
    arguments: dict[str, object], field: str, tmp_path: Path
) -> None:
    context, _ = workspace(tmp_path, {})
    with pytest.raises(SkillHandlerError) as caught:
        asyncio.run(run(arguments, context))
    assert caught.value.code == "invalid_arguments"
    assert caught.value.user_fixable is True
    assert (caught.value.details or {}).get("field") == field


def test_missing_artifact_is_a_fixable_handler_error(tmp_path: Path) -> None:
    context, _ = workspace(tmp_path, {})
    with pytest.raises(SkillHandlerError) as caught:
        asyncio.run(run({"artifact": HANDLE, "kind": "pdf", "question": "q"}, context))
    assert caught.value.code == "handler_error"
    assert caught.value.user_fixable is True
    assert "abcdefgh1234" in json.dumps(caught.value.details)


def test_symlink_escape_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(make_pdf([["Secrets", "This must not be readable."]]))
    context, root = workspace(tmp_path, {})
    (root / "var" / "artifacts" / "abcdefgh1234.pdf").symlink_to(outside)

    with pytest.raises(SkillHandlerError) as caught:
        asyncio.run(run({"artifact": HANDLE, "kind": "pdf", "question": "secrets"}, context))
    assert caught.value.code == "handler_error"
    assert "This must not be readable" not in caught.value.message


def test_wrong_extension_content_is_reported_not_answered(tmp_path: Path) -> None:
    context, _ = workspace(tmp_path, {"abcdefgh1234.pdf": b"plain text, not a pdf"})
    with pytest.raises(SkillHandlerError) as caught:
        asyncio.run(run({"artifact": HANDLE, "kind": "pdf", "question": "q"}, context))
    assert caught.value.code == "handler_error"
    assert caught.value.user_fixable is True


# ---------------------------------------------------------------------------
# performance envelope
# ---------------------------------------------------------------------------


def test_200_page_pdf_answers_within_two_seconds(tmp_path: Path) -> None:
    pages = [
        [
            f"Section {number}",
            f"Capacity in zone zone{number} is available for H200 tenants.",
            f"Support response target for tier {number % 5} is {number % 60 + 1} minutes.",
        ]
        for number in range(1, 201)
    ]
    context, _ = workspace(tmp_path, {"abcdefgh1234.pdf": make_pdf(pages)})

    cold = time.perf_counter()
    first = ask(context, "pdf", "support response target for tier 3")
    cold_seconds = time.perf_counter() - cold

    warm = time.perf_counter()
    second = ask(context, "pdf", "support response target for tier 3")
    warm_seconds = time.perf_counter() - warm

    assert cold_seconds < 2.0, cold_seconds
    assert warm_seconds < 2.0, warm_seconds
    assert first == second
    assert first["artifact"]["pages"] == 200
    assert 1 <= len(first["citations"]) <= 8
    # Context is capped: the answer cannot smuggle in 600 pages of text.
    context_tokens = sum(
        token_count(citation["text"]) for citation in first["citations"]
    )
    assert context_tokens <= 1200

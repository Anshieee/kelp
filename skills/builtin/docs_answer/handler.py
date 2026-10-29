"""`docs.answer` handler — local, citation-backed PDF/CSV question answering.

Retrieval is hybrid: BM25 over a content-addressed index under
``${WORKSPACE}/var/index``, plus an optional local-embedding hook (see
:mod:`skills.builtin.docs_answer.embeddings`). Lexical is the default and the
only mode that runs without configuration; neither mode uses the network.

Per invocation:

1. Validate arguments against the manifest contract (the loader already does
   this for registry calls; direct calls get the same treatment).
2. Resolve the opaque ``artifact:<token>`` handle inside ``var/artifacts``.
3. Load or rebuild the SHA-256-keyed index, pruning indexes whose artifacts
   were deleted.
4. Score chunks, take the top few that fit the context-token cap, and return an
   extractive answer with ``page N`` / ``row N`` citations.

Parsing and scoring are CPU-bound, so they run in a worker thread: a large PDF
must not stall the event loop for every other session. The handler never
receives paths, never writes outside the workspace, never stores the question
or a secret in the index. Answers and citations are excerpts copied from the
artifact (whitespace collapsed, clipped to a fixed budget, ``…`` when cut) — they
are never paraphrased or synthesised. Every outgoing string additionally passes
through :meth:`SkillContext.scrub`, which is best-effort substring redaction of
the values this invocation was given; it is a defence in depth, not a promise
that arbitrary file content is safe to show.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Mapping

from skills.context import SkillContext
from skills.errors import SkillHandlerError

from . import embeddings
from .answer import artifact_descriptor, csv_answer, pdf_answer
from .extract import CsvTable, read_csv
from .index import ensure_index
from .paths import HANDLE_RE, KINDS, artifact_path
from .retrieval import score_chunks, select_chunks

__all__ = ["run"]

_QUESTION_MAX = 1000
_MAX_ROWS_DEFAULT = 20
_MAX_ROWS_CEILING = 200
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _invalid(field: str, message: str) -> SkillHandlerError:
    return SkillHandlerError(
        "invalid_arguments",
        message,
        retryable=False,
        user_fixable=True,
        details={"field": field},
    )


def _validate(arguments: Mapping[str, Any]) -> tuple[str, str, str, int]:
    if not isinstance(arguments, Mapping):
        raise _invalid("", "Arguments must be an object.")

    handle = arguments.get("artifact")
    if not isinstance(handle, str) or not HANDLE_RE.match(handle):
        raise _invalid("/artifact", "artifact must be an artifact:<id> handle.")

    kind = arguments.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        raise _invalid("/kind", 'kind must be "pdf" or "csv".')

    question = arguments.get("question")
    if not isinstance(question, str) or not question.strip():
        raise _invalid("/question", "question must be a non-empty string.")
    if len(question) > _QUESTION_MAX:
        raise _invalid("/question", f"question must be at most {_QUESTION_MAX} characters.")
    question = " ".join(_CONTROL_RE.sub(" ", question).split())
    if not question:
        raise _invalid("/question", "question must be a non-empty string.")

    max_rows = arguments.get("max_rows", _MAX_ROWS_DEFAULT)
    if isinstance(max_rows, bool) or not isinstance(max_rows, int):
        raise _invalid("/max_rows", "max_rows must be an integer.")
    if not 1 <= max_rows <= _MAX_ROWS_CEILING:
        raise _invalid("/max_rows", f"max_rows must be between 1 and {_MAX_ROWS_CEILING}.")

    return handle, str(kind), question, max_rows


def _query_vector(index: Mapping[str, Any], question: str) -> list[float] | None:
    """Embed the question only when the index was built with the same model."""

    hook = embeddings.get_embedder()
    if hook is None or not index.get("embedding_model"):
        return None
    name, function = hook
    if index.get("embedding_model") != name:
        return None
    try:
        rows = list(function([question]))
    except Exception:  # noqa: BLE001 - optional hook, lexical fallback is fine
        return None
    if len(rows) != 1:
        return None
    try:
        return [float(value) for value in rows[0]]
    except (TypeError, ValueError):
        return None


def _csv_table(path: Any) -> CsvTable:
    # The index is a cache of derived text; the source rows are authoritative
    # for aggregates, so parse once more from disk (still local, still small).
    # A parse fault (including ``csv.Error``, already normalised to ValueError by
    # ``read_csv``) must reach the caller as one plain sentence, never as a raw
    # exception that could quote file content.
    try:
        return read_csv(path.read_bytes())
    except OSError as exc:
        raise ValueError("CSV content is no longer readable") from exc


def _answer(
    context: SkillContext, handle: str, kind: str, question: str, max_rows: int
) -> dict[str, Any]:
    """Synchronous body; runs in a worker thread via :func:`run`."""

    try:
        path = artifact_path(context, handle, kind)
    except ValueError as exc:
        raise _invalid("/artifact", "artifact is not a valid handle for this kind.") from exc
    except FileNotFoundError as exc:
        raise SkillHandlerError(
            "handler_error",
            "The attached file is no longer on disk.",
            retryable=False,
            user_fixable=True,
            details={"artifact": handle},
        ) from exc

    try:
        index = ensure_index(
            context.workspace_root,
            handle=handle,
            kind=kind,
            path=path,
            embedder=embeddings.get_embedder(),
        ).payload
    except ValueError as exc:
        raise SkillHandlerError(
            "handler_error",
            context.scrub(f"The attached file could not be read: {exc}"),
            retryable=False,
            user_fixable=True,
            details={"artifact": handle},
        ) from exc
    except OSError as exc:
        raise SkillHandlerError(
            "handler_error",
            "The attached file could not be read or indexed.",
            retryable=True,
            user_fixable=False,
            details={"artifact": handle},
        ) from exc

    ranked = score_chunks(index, question, query_vector=_query_vector(index, question))
    selected = select_chunks(ranked)

    try:
        if kind == "pdf":
            answer, citations = pdf_answer(selected, question)
        else:
            answer, citations = csv_answer(
                _csv_table(path), question, max_rows=max_rows
            )
    except ValueError as exc:
        raise SkillHandlerError(
            "handler_error",
            context.scrub(f"The attached file could not be answered: {exc}"),
            retryable=False,
            user_fixable=True,
            details={"artifact": handle},
        ) from exc

    # Defence in depth: even if a document happened to contain a configured
    # secret value, it never leaves the skill in an answer or citation.
    return {
        "answer": context.scrub(answer),
        "artifact": artifact_descriptor(index),
        "citations": [
            {"locator": str(item["locator"]), "text": context.scrub(str(item["text"]))}
            for item in citations
        ],
    }


async def run(arguments: Mapping[str, Any], context: SkillContext) -> Mapping[str, Any]:
    """Answer a question about a local PDF or CSV artifact.

    Contract: read-only, local files only, no network. Returns
    ``{answer, artifact, citations}`` per ``manifest.json``.
    """

    handle, kind, question, max_rows = _validate(arguments)
    return await asyncio.to_thread(_answer, context, handle, kind, question, max_rows)

"""Fixture ``docs.answer`` handler — answers from the attached PDF or CSV.

This is the M1 stand-in for the real handler. It resolves the artifact through
the same workspace path the registry wrote to, reads it, and derives an answer
and citations locally. No model, no network.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from demo.handlers import _artifacts
from skills.context import SkillContext
from skills.errors import SkillHandlerError

__all__ = ["run"]

_STOPWORDS = frozenset(
    {
        "about", "answer", "does", "from", "have", "into", "much", "please",
        "question", "show", "summarise", "summarize", "that", "this", "total",
        "what", "when", "where", "which", "with", "rows", "many",
    }
)


def _keywords(question: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", question.lower())
    return [word for word in words if len(word) > 3 and word not in _STOPWORDS]


def _answer_pdf(context: SkillContext, handle: str, question: str) -> Mapping[str, Any]:
    text = _artifacts.read_pdf(context, handle)
    keywords = _keywords(question)
    best_page = 0
    best_line = ""
    best_score = 0
    for index, lines in enumerate(text.pages, start=1):
        for line in lines:
            lowered = line.lower()
            score = sum(1 for keyword in keywords if keyword in lowered)
            if score > best_score:
                best_page, best_line, best_score = index, line, score
    if not best_line:
        best_page = 1
        first = text.pages[0] if text.pages else []
        # Line 0 is the document title; the first factual line reads better as
        # a default summary when the question names no keyword.
        best_line = first[1] if len(first) > 1 else (first[0] if first else "The document is empty.")
    return {
        "answer": best_line,
        "artifact": {
            "handle": handle,
            "kind": "pdf",
            "mime": text.mime,
            "pages": len(text.pages),
        },
        "citations": [{"locator": f"page {best_page}", "text": best_line}],
    }


def _answer_csv(
    context: SkillContext, handle: str, question: str, max_rows: int
) -> Mapping[str, Any]:
    header, body = _artifacts.read_csv(context, handle)
    lowered = question.lower()
    chosen = None
    for index, name in enumerate(header):
        values = [row[index] for row in body if index < len(row)]
        if values and all(_artifacts.numeric(value) is not None for value in values):
            if chosen is None:
                chosen = (index, name)
            if name.lower() in lowered or any(
                part in lowered for part in name.lower().split("_")
            ):
                chosen = (index, name)
                break

    if chosen is not None:
        index, name = chosen
        total = sum(_artifacts.numeric(row[index]) or 0.0 for row in body if index < len(row))
        answer = f"Total {name} across {len(body)} rows is {_artifacts.money(total)}."
    else:
        answer = f"The file has {len(body)} data rows across {len(header)} columns."

    citations = [
        {"locator": f"row {number}", "text": ",".join(row)}
        for number, row in enumerate(body[:max_rows], start=2)
    ]
    return {
        "answer": answer,
        "artifact": {
            "handle": handle,
            "kind": "csv",
            "mime": "text/csv; charset=utf-8",
            "rows": len(body),
        },
        "citations": citations,
    }


async def run(arguments: Mapping[str, Any], context: SkillContext) -> Mapping[str, Any]:
    handle = str(arguments["artifact"])
    kind = str(arguments["kind"])
    question = str(arguments["question"])
    max_rows = int(arguments.get("max_rows") or 20)
    try:
        if kind == "pdf":
            return _answer_pdf(context, handle, question)
        return _answer_csv(context, handle, question, max_rows)
    except FileNotFoundError as exc:
        raise SkillHandlerError(
            "handler_error",
            "The attached file is no longer on disk.",
            retryable=False,
            user_fixable=True,
            details={"artifact": handle},
        ) from exc
    except ValueError as exc:
        raise SkillHandlerError(
            "handler_error",
            f"The attached file could not be read: {exc}",
            retryable=False,
            user_fixable=True,
        ) from exc

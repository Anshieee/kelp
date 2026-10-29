"""PDF/CSV question answering with page and row citations.

Public surface: :func:`run` (the manifest entry point) plus the optional
:mod:`~skills.builtin.docs_answer.embeddings` hook. Everything else is private
retrieval machinery; see ``docs/adr/0001-docs-answer-single-skill.md``.
"""

from __future__ import annotations

from .handler import run

__all__ = ["run"]

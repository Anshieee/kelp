# ADR 0001 — `docs.answer` stays one skill; PDF/CSV dispatch is internal

- **Status:** Accepted
- **Date:** 2026-10-28
- **Issue:** KEL-5 (P0-4), decision requested by the issue text
- **Related:** `docs/SKILL_SPEC.md`, `skills/builtin/docs_answer/manifest.json`

## Context

P0-4 requires a skill that answers questions about uploaded PDFs and CSVs and returns the
answer plus an artifact handle. Two shapes were possible:

1. One model-facing tool, `docs.answer`, with a `kind` discriminator (`pdf` | `csv`) and an
   internal dispatch to format-specific readers.
2. Two tools, `pdf.extract` and `csv.query`, plus an orchestration layer that decides which to
   call.

Constraints that matter:

- The v1.0 contract binds exactly one model-facing tool per skill.
- The kernel loop (P0-1) should not need format-specific branching.
- Approval policy (P0-3) is per tool call; both formats are read-only, so policy is identical.
- Retrieval internals (text extraction, page/row citation) will evolve independently of the
  public tool surface.

## Decision

Ship **one skill**: `docs.answer`, with `kind: "pdf" | "csv"` in the input schema and an
internal dispatcher. It returns the same output envelope for both formats: `answer`, an
artifact descriptor (`handle`, `kind`, `mime`, optional `pages`/`rows`), and `citations`
(page locator for PDF, row locator for CSV).

The handler may use private helpers (`pdf.extract`, `csv.query`) internally. They are not
registered skills, are not exposed to the model, and can change without a manifest change.

## Consequences

- **Good:** one contract, one approval policy, one set of citations; the model cannot pick the
  wrong format tool; adding a format (e.g. XLSX) is a schema enum bump, not a new tool.
- **Good:** per-format internals can be replaced or reused by a future ingestion pipeline.
- **Cost:** the input schema carries a discriminator and the handler owns a dispatch table.
- **Cost:** if a future surface needs raw table access (e.g. "show me row 42"), that becomes a
  separate skill with its own contract rather than overloading `docs.answer`.
- **Revisit when:** a second surface needs format-specific output, or a non-LLM client needs
  structured extraction without an answer. At that point split into `pdf.extract` and
  `csv.query` and keep `docs.answer` as the composing skill.

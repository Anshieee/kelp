"""Hybrid retrieval over indexed chunks: lexical by default, optional vectors.

Scoring is BM25 over an in-memory index. An embedding hook can add a cosine
term, but it is optional and *never* the only signal: with no hook configured
(the default, and the only mode in tests) retrieval is purely lexical, local,
and network-free. Ties break on the chunk's document ordinal, so identical
inputs always yield identical citations.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from .chunking import Chunk, chunk_from_dict

__all__ = [
    "DEFAULT_TOP_K",
    "EMBEDDING_WEIGHT",
    "TOKEN_CAP",
    "ScoredChunk",
    "cosine",
    "select_chunks",
    "score_chunks",
    "tokenize",
    "token_count",
]

#: Default number of chunks offered to the answer builder.
DEFAULT_TOP_K = 5
#: Default context ceiling, in whitespace tokens, for the selected chunks.
TOKEN_CAP = 1200
#: Relative weight of the optional embedding similarity term.
EMBEDDING_WEIGHT = 0.35

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    """
    a about after all also an and any are as at be been before being between both
    but by can could did do does doing done during each few for from further had
    has have having he her here hers him his how i if in into is it its itself
    just me more most my no nor not now of off on once only or other our out over
    own same she should so some such than that the their them then there these
    they this those through to too under until up very was we were what when
    where which while who whom why will with would you your document file please
    tell show summarise summarize summary explain describe give list
    """.split()
)


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, minus stopwords and one-letter noise."""

    return [
        token
        for token in _TOKEN_RE.findall(text.lower())
        if len(token) > 1 and token not in _STOPWORDS
    ]


def token_count(text: str) -> int:
    """Cheap, deterministic token estimate used for the context cap."""

    return len(text.split())


@dataclass(frozen=True)
class ScoredChunk:
    chunk: Chunk
    score: float


Embedder = Callable[[Sequence[str]], Sequence[Sequence[float]]]


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity; ``0.0`` for mismatched or degenerate vectors."""

    if len(left) != len(right) or not left:
        return 0.0
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right):
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    return dot / math.sqrt(left_norm * right_norm)


def _vectors(chunks: Sequence[Mapping[str, Any]]) -> list[Sequence[float] | None]:
    vectors: list[Sequence[float] | None] = []
    for chunk in chunks:
        vector = chunk.get("vector")
        if isinstance(vector, (list, tuple)) and vector:
            vectors.append(vector)
        else:
            vectors.append(None)
    return vectors


def score_chunks(
    index: Mapping[str, Any],
    question: str,
    *,
    query_vector: Sequence[float] | None = None,
) -> list[ScoredChunk]:
    """Rank every chunk against the question.

    BM25 provides the lexical signal; an optional query vector adds a weighted
    cosine term when the index was built with the same embedding model.
    """

    raw_chunks: Sequence[Mapping[str, Any]] = index.get("chunks") or ()
    chunks = [chunk_from_dict(payload) for payload in raw_chunks]
    if not chunks:
        return []

    query_tokens = tokenize(question)
    query_terms = Counter(query_tokens)
    header = " ".join(index.get("header") or ())

    documents = [tokenize(f"{header} {chunk.text}".strip()) for chunk in chunks]
    frequencies = [Counter(document) for document in documents]
    lengths = [len(document) for document in documents]
    average = (sum(lengths) / len(lengths)) if lengths else 0.0

    document_frequency: Counter[str] = Counter()
    for document in documents:
        document_frequency.update(set(document))

    total = len(documents)
    k1, b = 1.2, 0.75
    idf: dict[str, float] = {
        term: math.log(1.0 + (total - count + 0.5) / (count + 0.5))
        for term, count in document_frequency.items()
    }

    # Adjacent query-term pairs reward phrase-like matches without needing a
    # separate phrase index.
    bigrams = list(zip(query_tokens, query_tokens[1:]))
    vectors = _vectors(raw_chunks)
    use_vectors = (
        query_vector is not None
        and bool(index.get("embedding_model"))
        and any(vector is not None for vector in vectors)
    )

    scored: list[ScoredChunk] = []
    for position, chunk in enumerate(chunks):
        score = 0.0
        frequency = frequencies[position]
        length = lengths[position] or 1
        for term, count in query_terms.items():
            occurrences = frequency.get(term)
            if not occurrences:
                continue
            weight = idf.get(term, 0.0)
            denominator = occurrences + k1 * (1.0 - b + b * (length / average if average else 1.0))
            score += weight * count * (occurrences * (k1 + 1.0) / denominator)

        if bigrams:
            document = documents[position]
            pairs = Counter(zip(document, document[1:]))
            matched = sum(1 for pair in bigrams if pairs.get(pair))
            score += 0.4 * matched

        if use_vectors and vectors[position] is not None:
            score += EMBEDDING_WEIGHT * cosine(query_vector, vectors[position] or ())
        scored.append(ScoredChunk(chunk=chunk, score=round(score, 6)))

    scored.sort(key=lambda item: (-item.score, item.chunk.ordinal))
    return scored


def select_chunks(
    scored: Iterable[ScoredChunk],
    *,
    top_k: int = DEFAULT_TOP_K,
    token_cap: int = TOKEN_CAP,
) -> list[ScoredChunk]:
    """Take the best ``top_k`` chunks that fit inside ``token_cap``.

    The cap is a ceiling on *context*, not on relevance: once the budget is
    spent, lower-ranked chunks are dropped rather than truncated mid-thought.
    The single best chunk is always kept (truncated by the answer builder if it
    alone exceeds the budget) so a match never disappears behind a hard cap.
    """

    ordered = sorted(scored, key=lambda item: (-item.score, item.chunk.ordinal))[
        : max(1, top_k)
    ]
    selected: list[ScoredChunk] = []
    used = 0
    for item in ordered:
        cost = token_count(item.chunk.text)
        if selected and used + cost > token_cap:
            continue
        selected.append(item)
        used += cost
    return selected

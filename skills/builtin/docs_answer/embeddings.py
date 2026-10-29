"""Optional embedding hook for `docs.answer` retrieval.

The default embedder is ``None``: retrieval is lexical (BM25) and touches no
network. A host that runs a local embedding model can register one at process
startup —:

    from skills.builtin.docs_answer import embeddings
    embeddings.set_embedder("local-mini", my_embed_fn)

The hook is deliberately process-wide rather than a new function argument: the
manifest fixes ``run(arguments, context)``, and retrieval must stay usable when
no hook is installed. A registered embedder is *additive*; if it raises, times
out, or returns vectors of inconsistent shape, retrieval silently falls back to
lexical scoring instead of failing the request.
"""

from __future__ import annotations

import threading
from typing import Callable, Sequence

__all__ = ["Embedder", "get_embedder", "has_embedder", "set_embedder", "reset_embedder"]

Embedder = Callable[[Sequence[str]], Sequence[Sequence[float]]]

_lock = threading.Lock()
_embedder: tuple[str, Embedder] | None = None


def set_embedder(name: str, embedder: Embedder | None) -> None:
    """Install (or clear, with ``None``) the process-wide embedding hook."""

    global _embedder
    if embedder is None:
        reset_embedder()
        return
    if not callable(embedder):
        raise TypeError("embedder must be callable")
    clean = (str(name).strip(), embedder)
    if not clean[0]:
        raise ValueError("embedder name must be non-empty")
    with _lock:
        _embedder = clean


def get_embedder() -> tuple[str, Embedder] | None:
    with _lock:
        return _embedder


def has_embedder() -> bool:
    return get_embedder() is not None


def reset_embedder() -> None:
    global _embedder
    with _lock:
        _embedder = None

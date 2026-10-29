"""Content-addressed retrieval index under ``${WORKSPACE}/var/index``.

Layout::

    var/index/<artifact-token>/<kind>/<sha256>.json

The filename is the SHA-256 of the artifact bytes, so an index can never be
mistaken for the content it was built from: changing the file changes the path,
and the old entry becomes garbage that :func:`prune_indexes` reclaims once the
artifact is gone. The per-token directory keeps stale cleanup a directory scan
instead of a full index read; the per-kind level keeps a PDF and a CSV that
happen to share a token from evicting each other.

Guarantees:

* **Idempotent** — the payload is a pure function of the artifact bytes and the
  chunk parameters. Rebuilding writes byte-identical JSON, so a second call is
  a cache hit and a forced rebuild is still reproducible.
* **Private** — directories are ``0700``, files ``0600``, written atomically via
  a same-directory temp file plus ``os.replace``.
* **Content-only** — the index holds extracted document text and positions. It
  never stores the question, the caller's arguments, or any secret value.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .chunking import (
    CSV_CHUNK_ROWS,
    MAX_CHUNK_CHARS,
    CHUNK_OVERLAP_CHARS,
    chunk_csv,
    chunk_from_dict,
    chunk_pdf,
)
from .extract import MAX_ARTIFACT_BYTES, CsvTable, extract_pdf, read_csv
from .paths import KINDS, mime_for, token
from .retrieval import Embedder

__all__ = [
    "CHUNK_PARAMS",
    "INDEX_VERSION",
    "IndexResult",
    "artifact_sha256",
    "build_payload",
    "ensure_index",
    "index_root",
    "prune_indexes",
    "remove_index",
]

INDEX_VERSION = 1
CHUNK_PARAMS: dict[str, int] = {
    "max_chars": MAX_CHUNK_CHARS,
    "overlap": CHUNK_OVERLAP_CHARS,
    "csv_rows": CSV_CHUNK_ROWS,
}
_TOKEN_DIR_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_READ_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class IndexResult:
    payload: dict[str, Any]
    rebuilt: bool
    path: Path


def index_root(workspace_root: str | os.PathLike[str]) -> Path:
    return Path(workspace_root) / "var" / "index"


def artifact_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(_READ_CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _vectorise(
    texts: Sequence[str], embedder: tuple[str, Embedder] | None
) -> tuple[str | None, list[list[float]] | None]:
    """Best-effort vectors. Any defect drops the vectors, never the request."""

    if embedder is None or not texts:
        return None, None
    name, function = embedder
    try:
        raw = function(list(texts))
        rows = [list(vector) for vector in raw]
    except Exception:  # noqa: BLE001 - an optional hook must not break retrieval
        return None, None
    if len(rows) != len(texts):
        return None, None
    dimension = len(rows[0]) if rows and rows[0] else 0
    if dimension == 0:
        return None, None
    vectors: list[list[float]] = []
    for row in rows:
        if len(row) != dimension:
            return None, None
        converted: list[float] = []
        for value in row:
            try:
                number = float(value)
            except (TypeError, ValueError):
                return None, None
            if not math.isfinite(number):
                return None, None
            converted.append(round(number, 6) + 0.0)  # normalise -0.0
        vectors.append(converted)
    return name, vectors


def build_payload(
    *,
    handle: str,
    kind: str,
    sha256: str,
    size: int,
    pages: Sequence[Sequence[str]] | None = None,
    table: CsvTable | None = None,
    embedder: tuple[str, Embedder] | None = None,
) -> dict[str, Any]:
    """Build the index document for one artifact. Pure; no I/O."""

    if kind not in KINDS:
        raise ValueError("unsupported artifact kind")
    if kind == "pdf" and pages is None:
        raise ValueError("pdf payload needs pages")
    if kind == "csv" and table is None:
        raise ValueError("csv payload needs a table")

    if kind == "pdf":
        assert pages is not None
        chunks = chunk_pdf(pages)
        artifact: dict[str, Any] = {
            "handle": handle,
            "kind": "pdf",
            "mime": mime_for("pdf"),
            "sha256": sha256,
            "size": size,
            "pages": len(pages),
        }
        header: list[str] = []
    else:
        assert table is not None
        chunks = chunk_csv(table)
        artifact = {
            "handle": handle,
            "kind": "csv",
            "mime": mime_for("csv"),
            "sha256": sha256,
            "size": size,
            "rows": len(table.rows),
        }
        header = list(table.header)

    model, vectors = _vectorise([chunk.text for chunk in chunks], embedder)

    serialised: list[dict[str, Any]] = []
    for position, chunk in enumerate(chunks):
        payload = chunk.to_dict()
        if vectors is not None:
            payload["vector"] = vectors[position]
        serialised.append(payload)

    return {
        "version": INDEX_VERSION,
        "artifact": artifact,
        "chunk_params": dict(CHUNK_PARAMS),
        "header": header,
        "embedding_model": model,
        "embedding_requested": embedder[0] if embedder is not None else None,
        "chunks": serialised,
    }


def _dump(payload: Mapping[str, Any]) -> bytes:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.encode("utf-8") + b"\n"


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:  # pragma: no cover - best effort on odd filesystems
        pass
    descriptor, temporary = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _load(
    path: Path, *, sha256: str, kind: str, embedding_model: str | None
) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    artifact = payload.get("artifact")
    if (
        payload.get("version") != INDEX_VERSION
        or payload.get("chunk_params") != CHUNK_PARAMS
        or not isinstance(artifact, dict)
        or artifact.get("sha256") != sha256
        or artifact.get("kind") != kind
        or payload.get("embedding_requested") != embedding_model
    ):
        return None
    chunks = payload.get("chunks")
    if not isinstance(chunks, list):
        return None
    try:
        for entry in chunks:
            chunk_from_dict(entry)
    except (KeyError, TypeError, ValueError):
        return None
    return payload


def _read_artifact(path: Path, kind: str) -> tuple[Sequence[Sequence[str]] | None, CsvTable | None]:
    if path.stat().st_size > MAX_ARTIFACT_BYTES:
        raise ValueError("artifact is larger than the reader allows")
    data = path.read_bytes()
    if kind == "pdf":
        return extract_pdf(data), None
    return None, read_csv(data)


def _drop_other_versions(directory: Path, keep: Path) -> None:
    """Remove index files for earlier content of the same artifact handle.

    The filename is the content hash, so an edited (or re-uploaded) artifact
    leaves its predecessor behind. Only files in our own token directory are
    touched, and only ``*.json`` entries.
    """

    try:
        entries = list(directory.iterdir())
    except OSError:  # pragma: no cover - permissions
        return
    for entry in entries:
        if entry == keep or entry.is_symlink() or not entry.is_file():
            continue
        if entry.suffix != ".json":
            continue
        try:
            entry.unlink()
        except OSError:  # pragma: no cover - concurrent reader/writer
            continue


def ensure_index(
    workspace_root: str | os.PathLike[str],
    *,
    handle: str,
    kind: str,
    path: Path,
    embedder: tuple[str, Embedder] | None = None,
) -> IndexResult:
    """Return a warm index for ``path``, building it when absent or stale."""

    if kind not in KINDS:
        raise ValueError("unsupported artifact kind")

    prune_indexes(workspace_root)

    if path.stat().st_size > MAX_ARTIFACT_BYTES:
        raise ValueError("artifact is larger than the reader allows")

    digest = artifact_sha256(path)
    destination = index_root(workspace_root) / token(handle) / kind / f"{digest}.json"

    # The requested embedding model is part of the cache key: enabling or
    # swapping the hook rebuilds rather than silently serving a lexical-only
    # index. ``embedding_requested`` (not ``embedding_model``) is compared so a
    # hook that failed once is not retried on every question.
    model_name = embedder[0] if embedder is not None else None
    cached = _load(
        destination, sha256=digest, kind=kind, embedding_model=model_name
    )
    if cached is not None:
        return IndexResult(payload=cached, rebuilt=False, path=destination)

    pages, table = _read_artifact(path, kind)
    payload = build_payload(
        handle=handle,
        kind=kind,
        sha256=digest,
        size=path.stat().st_size,
        pages=pages,
        table=table,
        embedder=embedder,
    )
    _write_atomic(destination, _dump(payload))
    _drop_other_versions(destination.parent, destination)
    return IndexResult(payload=payload, rebuilt=True, path=destination)


def remove_index(workspace_root: str | os.PathLike[str], handle: str) -> bool:
    """Drop every index entry for one artifact handle. Returns whether any existed."""

    if not re.fullmatch(r"artifact:[A-Za-z0-9_-]{8,128}", handle):
        raise ValueError("artifact handle is not valid")
    directory = index_root(workspace_root) / token(handle)
    if directory.is_symlink():
        directory.unlink()
        return True
    if not directory.is_dir():
        return False
    shutil.rmtree(directory)
    return True


def prune_indexes(workspace_root: str | os.PathLike[str]) -> list[str]:
    """Remove index directories whose artifact no longer exists.

    Runs before every build/load, so deleting an artifact also drops its derived
    text on the next `docs.answer` call. Only directories that carry a valid
    handle token are touched; anything else under ``var/index`` is left alone.
    Best effort: a read-only workspace simply keeps its cache.
    """

    root = index_root(workspace_root)
    if not root.is_dir():
        return []
    artifacts = Path(workspace_root) / "var" / "artifacts"

    removed: list[str] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:  # pragma: no cover - permissions
        return []

    for entry in entries:
        if entry.is_symlink() or not entry.is_dir():
            continue
        if not _TOKEN_DIR_RE.fullmatch(entry.name):
            continue
        if any((artifacts / f"{entry.name}.{kind}").is_file() for kind in KINDS):
            continue
        try:
            shutil.rmtree(entry)
        except OSError:  # pragma: no cover - concurrent prune
            continue
        removed.append(entry.name)
    return removed

"""Artifact handle resolution for `docs.answer`.

Handles are opaque: ``artifact:<token>`` where the token matches the registry
pattern. The handler never accepts a path, and the resolved real path must stay
inside ``${WORKSPACE}/var/artifacts`` even after symlinks are followed. Errors
carry no filesystem detail, so a caller cannot probe the workspace layout.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

__all__ = ["HANDLE_RE", "KINDS", "artifact_path", "token"]

HANDLE_RE = re.compile(r"^artifact:[A-Za-z0-9_-]{8,128}$")
KINDS = frozenset({"pdf", "csv"})

_MIME = {"pdf": "application/pdf", "csv": "text/csv; charset=utf-8"}


def token(handle: str) -> str:
    return handle.split(":", 1)[1]


def mime_for(kind: str) -> str:
    return _MIME[kind]


def artifact_path(context: Any, handle: str, kind: str) -> Path:
    """Resolve ``handle`` to the on-disk artifact, confined to the artifacts dir.

    Raises ``ValueError`` for a malformed handle or kind and ``FileNotFoundError``
    when no artifact is stored for it.
    """

    if not isinstance(handle, str) or not HANDLE_RE.match(handle):
        raise ValueError("artifact handle is not valid")
    if not isinstance(kind, str) or kind not in KINDS:
        raise ValueError("unsupported artifact kind")

    root = (Path(context.workspace_root) / "var" / "artifacts").resolve()
    candidate = root / f"{token(handle)}.{kind}"
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise FileNotFoundError("artifact content is missing") from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise FileNotFoundError("artifact content is missing")
    return resolved

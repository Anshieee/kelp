"""Artifact registry and content serving helpers (KEL-8 §8).

Uploads are validated by magic/UTF-8, stored under opaque ids inside
``var/artifacts``, and resolved by registry lookup only. User-supplied names
never become paths, so traversal cannot be expressed. Access is scoped to the
thread that claimed the upload.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .cards import artifact_card
from .store import ChatStore, iso_now

__all__ = [
    "ArtifactError",
    "ArtifactRegistry",
    "ContentResult",
    "HANDLE_RE",
    "MAX_UPLOAD_BYTES",
    "sanitize_filename",
]

HANDLE_RE = re.compile(r"^artifact:[A-Za-z0-9_-]{8,128}$")
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
PREVIEW_ROWS_MAX = 20
PREVIEW_ROWS_DEFAULT = 5
CELL_CHARS = 60
_CSV_HEADER_DELIMITERS = (",", ";")
_STORAGE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_CONTENT_TYPES = {
    "pdf": "application/pdf",
    "csv": "text/csv; charset=utf-8",
}
_PDF_PAGE_RE = re.compile(rb"/Type\s*/Page(?![s\w])")


class ArtifactError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 404):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class ContentResult:
    status: int
    headers: dict[str, str]
    body: bytes


def sanitize_filename(name: str, *, fallback: str = "download") -> str:
    """Unicode-normalised, path-free, header-safe display name."""

    raw = Path(str(name or "")).name
    # Headers are latin-1; fold to ASCII so a Unicode filename cannot break a
    # response or smuggle control characters into Content-Disposition.
    folded = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode()
    cleaned = "".join(
        char
        for char in folded
        if char.isprintable() and char not in {'"', "\\", "/", "\r", "\n", "\x00"}
    ).strip()
    cleaned = cleaned[:180] or fallback
    return cleaned


def _split_extension(name: str) -> str:
    suffix = Path(name).suffix.lower().lstrip(".")
    return suffix if suffix in {"pdf", "csv"} else ""


def _count_pdf_pages(data: bytes) -> int | None:
    total = len(_PDF_PAGE_RE.findall(data))
    return total or None


def _parse_csv(data: bytes) -> tuple[list[str], list[list[str]], int]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactError(
            "unsupported_file",
            "Only UTF-8 CSV files are supported.",
            status=415,
        ) from exc
    if "\x00" in text:
        raise ArtifactError("unsupported_file", "The file contains binary data.", status=415)
    sample = text.splitlines()[0] if text.splitlines() else ""
    delimiter = next(
        (candidate for candidate in _CSV_HEADER_DELIMITERS if candidate in sample), None
    )
    if delimiter is None:
        raise ArtifactError(
            "unsupported_file",
            "A CSV file needs a comma or semicolon header.",
            status=415,
        )
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = [[str(cell) for cell in row] for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise ArtifactError("unsupported_file", "The CSV file is empty.", status=415)
    header = [cell[:CELL_CHARS] for cell in rows[0]]
    body = [[cell[:CELL_CHARS] for cell in row] for row in rows[1:]]
    return header, body, len(body)


class ArtifactRegistry:
    def __init__(
        self,
        store: ChatStore,
        root: str | Path,
        *,
        now: Any | None = None,
        workspace_root: str | Path | None = None,
    ):
        self._store = store
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self._root, 0o700)
        except OSError:  # pragma: no cover - platform dependent
            pass
        self._now = now or iso_now
        self._workspace_root = (
            Path(workspace_root).expanduser().resolve()
            if workspace_root is not None
            else None
        )

    @property
    def root(self) -> Path:
        return self._root

    def _workspace(self) -> Path | None:
        """Workspace that owns ``var/index``, or ``None`` when unknown.

        The server passes it explicitly. A default layout
        (``<workspace>/var/artifacts``) is recognised so embedders such as the
        demo and the test harness get eager index removal without extra wiring.
        """

        if self._workspace_root is not None:
            return self._workspace_root
        if self._root.name == "artifacts" and self._root.parent.name == "var":
            return self._root.parent.parent
        return None

    # -- upload ------------------------------------------------------------

    def save_upload(self, filename: str, data: bytes) -> dict[str, Any]:
        if not data:
            raise ArtifactError("empty_file", "The uploaded file is empty.", status=400)
        if len(data) > MAX_UPLOAD_BYTES:
            raise ArtifactError(
                "upload_too_large",
                f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
                status=413,
            )
        display = sanitize_filename(filename, fallback="upload")
        kind = self._detect_kind(display, data)
        token = secrets.token_urlsafe(12)
        handle = f"artifact:{token}"
        storage_name = f"{token}.{kind}"
        path = self._root / storage_name
        if path.exists():  # pragma: no cover - 96 bits of collision space
            raise ArtifactError("conflict", "Could not allocate storage.", status=500)
        digest = hashlib.sha256(data).hexdigest()
        try:
            with path.open("xb") as handle_file:
                handle_file.write(data)
                handle_file.flush()
                os.fsync(handle_file.fileno())
            os.chmod(path, 0o600)
        except OSError as exc:
            raise ArtifactError("storage_error", "Could not store the file.", status=500) from exc

        pages = _count_pdf_pages(data) if kind == "pdf" else None
        rows = None
        columns = None
        if kind == "csv":
            header, _body, rows = _parse_csv(data)
            columns = header

        meta = self._store.create_artifact(
            {
                "handle": handle,
                "thread_id": None,
                "name": display,
                "kind": kind,
                "mime": _CONTENT_TYPES[kind],
                "size_bytes": len(data),
                "sha256": digest,
                "storage_path": storage_name,
                "created_at": self._now(),
                "pages": pages,
                "rows": rows,
            }
        )
        if columns:
            meta["columns"] = columns
        return meta

    def _detect_kind(self, filename: str, data: bytes) -> str:
        suffix = _split_extension(filename)
        if data.startswith(b"%PDF-"):
            return "pdf"
        if b"\x00" not in data:
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                data = b""
            if data:
                _parse_csv(data)  # validates the header delimiter
                return "csv"
        if suffix == "pdf" or suffix == "csv":
            raise ArtifactError(
                "unsupported_file",
                f"The file does not look like a valid {suffix.upper()}.",
                status=415,
            )
        raise ArtifactError(
            "unsupported_file",
            "Only PDF and CSV files are supported.",
            status=415,
        )

    # -- lookup ------------------------------------------------------------

    def get(
        self, handle: str, *, thread_id: str | None = None
    ) -> dict[str, Any]:
        """Resolve a handle, enforcing thread scope for bound artifacts.

        An unbound upload (``thread_id IS NULL``, before it is attached to a
        turn) is readable so the composer can preview it. Once a turn has
        claimed it, the same thread must be supplied: a bound handle without a
        matching ``thread_id`` is indistinguishable from an unknown one, so a
        guessed handle cannot be read from another thread.
        """

        meta = self._lookup(handle)
        owner = meta.get("thread_id")
        if owner is not None:
            if owner != thread_id:
                raise ArtifactError("artifact_not_found", "That file isn't available any more.")
            if self._store.get_thread(owner) is None:
                raise ArtifactError("artifact_not_found", "That file isn't available any more.")
        self._resolve_path(meta)
        return meta

    def _lookup(self, handle: str) -> dict[str, Any]:
        """Unscoped store lookup for internal flows (claim, upload card)."""

        if not isinstance(handle, str) or not HANDLE_RE.match(handle):
            raise ArtifactError("artifact_not_found", "That file isn't available any more.")
        meta = self._store.get_artifact(handle)
        if meta is None:
            raise ArtifactError("artifact_not_found", "That file isn't available any more.")
        return meta

    def card(
        self, handle: str, *, thread_id: str | None = None, rows: int = PREVIEW_ROWS_DEFAULT
    ) -> dict[str, Any]:
        meta = self.get(handle, thread_id=thread_id)
        if meta["kind"] == "csv":
            try:
                preview = self.preview(handle, rows=rows, thread_id=thread_id)
                meta = dict(meta)
                meta["table"] = preview
            except ArtifactError:
                pass
        return artifact_card(meta)

    def claim(self, handle: str, thread_id: str) -> dict[str, Any]:
        """Bind an uploaded artifact to the turn's thread exactly once."""

        existing = self._lookup(handle)
        if existing.get("thread_id") is None:
            if not self._store.bind_artifact(handle, thread_id):
                existing = self._lookup(handle)
                if existing.get("thread_id") != thread_id:
                    raise ArtifactError(
                        "artifact_not_found", "That file isn't available any more."
                    )
        elif existing.get("thread_id") != thread_id:
            raise ArtifactError(
                "artifact_not_found", "That file isn't available any more."
            )
        return self.get(handle, thread_id=thread_id)

    # -- deletion ----------------------------------------------------------

    def purge(self, artifacts: Sequence[Mapping[str, Any]]) -> dict[str, int]:
        """Unlink stored bytes and drop derived index entries for rows already
        removed from the registry.

        Best effort per item: one unreadable path must not stop the rest. A
        file that is already gone counts as ``missing`` (the normal
        second-delete case); every leftover is safe to retry, and the orphan
        sweep does exactly that.
        """

        files_removed = 0
        files_missing = 0
        indexes_removed = 0
        errors = 0
        workspace = self._workspace()
        for meta in artifacts:
            path = self._storage_path_for_unlink(meta.get("storage_path"))
            if path is None:
                errors += 1
            else:
                try:
                    path.unlink()
                    files_removed += 1
                except FileNotFoundError:
                    files_missing += 1
                except OSError:
                    errors += 1
            handle = meta.get("handle")
            if workspace is not None and isinstance(handle, str):
                try:
                    if _remove_index(workspace, handle):
                        indexes_removed += 1
                except (OSError, ValueError):
                    errors += 1
        return {
            "files_removed": files_removed,
            "files_missing": files_missing,
            "indexes_removed": indexes_removed,
            "errors": errors,
        }

    def sweep_orphans(
        self,
        *,
        older_than_seconds: float,
        skip_handles: Sequence[str] = (),
        now: datetime | None = None,
    ) -> dict[str, int]:
        """Remove parentless registry rows, files, and index directories.

        The age threshold is the crash window: a file is written before its
        registry row, so anything younger than the grace period may belong to
        an upload or a delete that is still in flight. Handles referenced by
        any message block are skipped even when the row looks unbound, which is
        the "never sweep a turn-referenced artifact" rule.
        """

        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        cutoff_dt = current - timedelta(seconds=max(0.0, float(older_than_seconds)))
        cutoff_iso = iso_now(cutoff_dt)
        cutoff_ts = cutoff_dt.timestamp()

        skipped = set(str(handle) for handle in skip_handles)
        skipped.update(self._store.referenced_artifact_handles())
        rows = self._store.orphan_artifact_rows(
            cutoff_iso, skip_handles=tuple(skipped)
        )
        handles = [row["handle"] for row in rows]
        deleted_rows = self._store.delete_artifacts(handles)
        purged = self.purge(rows)

        # Files that never got a registry row (crash between write and insert,
        # or a delete interrupted after the row went). Only age-eligible ones
        # are candidates; a symlink is unlinked, never followed.
        referenced = self._store.artifact_storage_paths()
        orphan_files = 0
        try:
            entries = list(self._root.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            if entry.name in referenced:
                continue
            try:
                if entry.is_dir() and not entry.is_symlink():
                    continue
                if entry.stat(follow_symlinks=False).st_mtime >= cutoff_ts:
                    continue
                entry.unlink()
            except OSError:
                purged["errors"] += 1
                continue
            orphan_files += 1
            token = entry.name.split(".", 1)[0]
            workspace = self._workspace()
            if workspace is not None and _STORAGE_TOKEN_RE.fullmatch(token):
                try:
                    if _remove_index(workspace, f"artifact:{token}"):
                        purged["indexes_removed"] += 1
                except (OSError, ValueError):
                    purged["errors"] += 1

        # Index directories whose artifact row is already gone. This catches a
        # crash between the registry delete and eager remove_index.
        orphan_indexes = 0
        workspace = self._workspace()
        if workspace is not None:
            live = self._store.artifact_handles()
            live_tokens = {
                handle.split(":", 1)[1]
                for handle in live
                if handle.startswith("artifact:")
            }
            index_root = workspace / "var" / "index"
            try:
                entries = list(index_root.iterdir())
            except OSError:
                entries = []
            for entry in entries:
                if entry.name in live_tokens:
                    continue
                if not _STORAGE_TOKEN_RE.fullmatch(entry.name):
                    continue
                if (
                    entry.is_dir()
                    and entry.stat(follow_symlinks=False).st_mtime >= cutoff_ts
                ):
                    continue
                try:
                    if _remove_index(workspace, f"artifact:{entry.name}"):
                        orphan_indexes += 1
                except (OSError, ValueError):
                    purged["errors"] += 1

        return {
            "artifact_rows": deleted_rows,
            "files_removed": purged["files_removed"] + orphan_files,
            "files_missing": purged["files_missing"],
            "indexes_removed": purged["indexes_removed"] + orphan_indexes,
            "skipped": len(skipped),
            "errors": purged["errors"],
        }

    # -- content -----------------------------------------------------------

    def content(
        self,
        handle: str,
        *,
        thread_id: str | None = None,
        download: bool = False,
        range_header: str | None = None,
    ) -> ContentResult:
        meta = self.get(handle, thread_id=thread_id)
        path = self._resolve_path(meta)
        data = path.read_bytes()
        total = len(data)
        filename = sanitize_filename(meta["name"])
        disposition = "attachment" if download else "inline"
        headers = {
            "Content-Type": _CONTENT_TYPES.get(meta["kind"], "application/octet-stream"),
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'{disposition}; filename="{filename}"',
            "Accept-Ranges": "bytes",
            "ETag": f'"{meta["sha256"]}"',
            # §8.2: the only route allowed inside the app's sandboxed preview.
            "X-Frame-Options": "SAMEORIGIN",
            "Content-Security-Policy": "frame-ancestors 'self'",
            "Cache-Control": "private, max-age=300",
        }
        status = 200
        body = data
        if range_header:
            parsed = _parse_range(range_header, total)
            if parsed is None:
                return ContentResult(
                    status=416,
                    headers={**headers, "Content-Range": f"bytes */{total}"},
                    body=b"",
                )
            start, end = parsed
            body = data[start : end + 1]
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{total}"
            headers["Content-Length"] = str(len(body))
        else:
            headers["Content-Length"] = str(total)
        return ContentResult(status=status, headers=headers, body=body)

    # -- preview -----------------------------------------------------------

    def preview(
        self, handle: str, *, rows: int = PREVIEW_ROWS_DEFAULT, thread_id: str | None = None
    ) -> dict[str, Any]:
        meta = self.get(handle, thread_id=thread_id)
        if meta["kind"] != "csv":
            raise ArtifactError(
                "preview_unavailable", "Previews are only available for CSV files.", status=415
            )
        limit = max(1, min(int(rows or PREVIEW_ROWS_DEFAULT), PREVIEW_ROWS_MAX))
        path = self._resolve_path(meta)
        header, body, total = _parse_csv(path.read_bytes())
        shown = body[:limit]
        return {
            "columns": header,
            "rows": shown,
            "preview_rows": len(shown),
            "total_rows": total,
            "truncated": total > len(shown),
        }

    # -- internals ---------------------------------------------------------

    def _storage_path_for_unlink(self, raw_value: Any) -> Path | None:
        """A direct child of the artifact root, or ``None`` when unusable.

        Deletion accepts a missing file (idempotent second delete) but never a
        path with separators, a directory, or anything outside the root. A
        symlink is returned so the link itself is removed, not its target.
        """

        raw = str(raw_value or "")
        if not raw or "/" in raw or "\\" in raw or raw in {".", ".."}:
            return None
        path = self._root / raw
        if path.parent != self._root:
            return None
        try:
            if path.is_dir() and not path.is_symlink():
                return None
        except OSError:
            return None
        return path

    def _resolve_path(self, meta: Mapping[str, Any]) -> Path:
        raw = str(meta.get("storage_path") or "")
        if not raw or "/" in raw or "\\" in raw or raw in {".", ".."}:
            raise ArtifactError("artifact_not_found", "That file isn't available any more.")
        path = self._root / raw
        if path.is_symlink():
            raise ArtifactError("artifact_not_found", "That file isn't available any more.")
        try:
            resolved = path.resolve(strict=True)
            root = self._root.resolve(strict=True)
        except (OSError, RuntimeError):
            raise ArtifactError(
                "artifact_not_found", "That file isn't available any more."
            ) from None
        if not resolved.is_relative_to(root):
            raise ArtifactError("artifact_not_found", "That file isn't available any more.")
        if not resolved.is_file():
            raise ArtifactError("artifact_not_found", "That file isn't available any more.")
        return resolved


def _remove_index(workspace_root: Path, handle: str) -> bool:
    """Eagerly drop a handle's ``docs.answer`` index, if the skill is present."""

    from skills.builtin.docs_answer.index import remove_index

    return remove_index(workspace_root, handle)


def _parse_range(header: str, total: int) -> tuple[int, int] | None:
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if match is None:
        return None
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        return None
    if not start_text:
        length = int(end_text)
        if length <= 0:
            return None
        start = max(0, total - length)
        end = total - 1
    else:
        start = int(start_text)
        end = int(end_text) if end_text else total - 1
    if start >= total or start > end:
        return None
    return start, min(end, total - 1)

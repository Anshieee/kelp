"""Retention policy, JSONL rotation, and the boot/daily maintenance pass.

One source of truth for every data class's clock lives here as
:data:`RETENTION_ROWS` (mirrored in ``docs/DEPLOY.md`` §8.1). The rows that are
enforced in this milestone are the ones backed by code below:

* chat content is removed when its thread is deleted
  (:meth:`chat.store.ChatStore.purge_thread`), not by an age timer; expired,
  revoked, and parentless share rows are pruned by the same boot/daily pass
  (:meth:`chat.store.ChatStore.share_sweep`), never on a share read;
* uploaded files and their derived ``docs.answer`` indexes live for the thread
  and are swept when parentless after :data:`DEFAULT_ARTIFACT_ORPHAN_SECONDS`;
* ``audit.jsonl`` and ``usage.jsonl`` rotate by size or by interval and their
  segments expire by TTL and by a segment-count cap.

Rotation never truncates or rewrites the active file. It renames it to a
timestamped segment with :func:`os.replace`, so a writer that already holds an
open descriptor finishes its line in that segment instead of losing it; the
next append creates a fresh active file. Segment readers in
:mod:`openshell.audit` and :mod:`kernel.usage` include the retained segments, so
rotation does not hide history or reset a spend window.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .logging import log

__all__ = [
    "DEFAULT_ARTIFACT_ORPHAN_SECONDS",
    "DEFAULT_AUDIT_KEEP",
    "DEFAULT_AUDIT_TTL_SECONDS",
    "DEFAULT_LOG_MAX_BYTES",
    "DEFAULT_LOG_ROTATE_SECONDS",
    "DEFAULT_MAINTENANCE_INTERVAL_SECONDS",
    "DEFAULT_USAGE_KEEP",
    "DEFAULT_USAGE_TTL_SECONDS",
    "RETENTION_ROWS",
    "RetentionMaintenance",
    "RetentionPolicy",
    "rotate_jsonl",
]

DEFAULT_AUDIT_TTL_SECONDS = 30 * 24 * 60 * 60
DEFAULT_USAGE_TTL_SECONDS = 90 * 24 * 60 * 60
DEFAULT_LOG_ROTATE_SECONDS = 24 * 60 * 60
DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_AUDIT_KEEP = 40
DEFAULT_USAGE_KEEP = 100
DEFAULT_ARTIFACT_ORPHAN_SECONDS = 60 * 60
DEFAULT_MAINTENANCE_INTERVAL_SECONDS = 24 * 60 * 60

AUDIT_DAYS_ENV = "KELP_RETENTION_AUDIT_DAYS"
USAGE_DAYS_ENV = "KELP_RETENTION_USAGE_DAYS"
AUDIT_KEEP_ENV = "KELP_RETENTION_AUDIT_KEEP"
USAGE_KEEP_ENV = "KELP_RETENTION_USAGE_KEEP"
LOG_MAX_BYTES_ENV = "KELP_RETENTION_LOG_MAX_BYTES"
LOG_ROTATE_ENV = "KELP_RETENTION_LOG_ROTATE_SECONDS"
ORPHAN_SECONDS_ENV = "KELP_RETENTION_ORPHAN_SECONDS"
INTERVAL_ENV = "KELP_RETENTION_INTERVAL_SECONDS"

#: Documentation contract. ``docs/DEPLOY.md`` §8.1 repeats this table; a test
#: checks that every row's class name appears there.
RETENTION_ROWS: tuple[dict[str, str], ...] = (
    {
        "class": "Chat content",
        "where": "chat.db threads, messages, blocks, events, turns, approvals, shares, share_views",
        "ttl": "Until the owner deletes the thread; expired/revoked share rows are pruned boot + daily",
        "enforced_by": "ChatStore.purge_thread (immediate hard delete); share_sweep boot + daily",
        "override": "none (explicit delete only)",
    },
    {
        "class": "Uploaded files",
        "where": "KELP_ARTIFACT_DIR (default workspace/var/artifacts)",
        "ttl": "Thread lifetime; unclaimed or parentless uploads after 1 h grace",
        "enforced_by": "ArtifactRegistry.purge / sweep_orphans, boot + daily",
        "override": ORPHAN_SECONDS_ENV,
    },
    {
        "class": "Derived index",
        "where": "workspace/var/index (docs.answer chunk cache)",
        "ttl": "Removed eagerly when the artifact or thread is deleted",
        "enforced_by": "ArtifactRegistry.purge -> remove_index; sweep_orphans",
        "override": "none (follows the artifact)",
    },
    {
        "class": "Audit records",
        "where": "KELP_AUDIT_DIR/audit.jsonl and its segments",
        "ttl": "30 days, capped at 40 segments",
        "enforced_by": "RetentionMaintenance.rotate (boot + daily)",
        "override": f"{AUDIT_DAYS_ENV}, {AUDIT_KEEP_ENV}",
    },
    {
        "class": "Usage records",
        "where": "KELP_USAGE_DIR/usage.jsonl and its segments",
        "ttl": "90 days, capped at 100 segments",
        "enforced_by": "RetentionMaintenance.rotate (boot + daily)",
        "override": f"{USAGE_DAYS_ENV}, {USAGE_KEEP_ENV}",
    },
    {
        "class": "Request/correlation logs",
        "where": "container stdout, not the data volume",
        "ttl": "Size-bounded: 10 MiB x 3 files per service, no age limit",
        "enforced_by": "docker-compose.yml logging block (json-file max-size/max-file)",
        "override": "docker-compose.yml logging.options (operator config)",
    },
    {
        "class": "Temporary artifacts",
        "where": "/tmp (16 MiB noexec tmpfs)",
        "ttl": "Container lifetime",
        "enforced_by": "container tmpfs, never persisted",
        "override": "docker-compose.yml",
    },
)

@dataclass(frozen=True)
class RetentionPolicy:
    """Enforced TTLs and rotation thresholds, with clamped environment overrides."""

    audit_ttl_seconds: int = DEFAULT_AUDIT_TTL_SECONDS
    usage_ttl_seconds: int = DEFAULT_USAGE_TTL_SECONDS
    audit_keep: int = DEFAULT_AUDIT_KEEP
    usage_keep: int = DEFAULT_USAGE_KEEP
    log_rotate_seconds: int = DEFAULT_LOG_ROTATE_SECONDS
    log_max_bytes: int = DEFAULT_LOG_MAX_BYTES
    artifact_orphan_seconds: int = DEFAULT_ARTIFACT_ORPHAN_SECONDS
    maintenance_interval_seconds: int = DEFAULT_MAINTENANCE_INTERVAL_SECONDS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RetentionPolicy":
        source = os.environ if env is None else env

        def number(name: str, default: int, *, minimum: int = 0) -> int:
            raw = source.get(name)
            text = raw.strip() if isinstance(raw, str) else ""
            if text == "":
                return default
            try:
                value = int(text, 10)
            except ValueError:
                raise ValueError(f"{name} must be an integer.") from None
            if value < minimum:
                raise ValueError(f"{name} must be >= {minimum}.")
            return value

        return cls(
            audit_ttl_seconds=number(
                AUDIT_DAYS_ENV, DEFAULT_AUDIT_TTL_SECONDS // 86400, minimum=1
            )
            * 86400,
            usage_ttl_seconds=number(
                USAGE_DAYS_ENV, DEFAULT_USAGE_TTL_SECONDS // 86400, minimum=1
            )
            * 86400,
            audit_keep=number(AUDIT_KEEP_ENV, DEFAULT_AUDIT_KEEP, minimum=1),
            usage_keep=number(USAGE_KEEP_ENV, DEFAULT_USAGE_KEEP, minimum=1),
            log_rotate_seconds=number(
                LOG_ROTATE_ENV, DEFAULT_LOG_ROTATE_SECONDS, minimum=1
            ),
            log_max_bytes=number(
                LOG_MAX_BYTES_ENV, DEFAULT_LOG_MAX_BYTES, minimum=1024
            ),
            artifact_orphan_seconds=number(
                ORPHAN_SECONDS_ENV, DEFAULT_ARTIFACT_ORPHAN_SECONDS, minimum=0
            ),
            maintenance_interval_seconds=number(
                INTERVAL_ENV, DEFAULT_MAINTENANCE_INTERVAL_SECONDS, minimum=60
            ),
        )


def _segments(path: Path) -> list[Path]:
    """Rotated siblings of ``path``, oldest first (timestamped names)."""

    pattern = re.compile(re.escape(path.name) + r"\.\d{8}T\d{12,}Z(?:\.\d{4,})?$")
    try:
        entries = list(path.parent.iterdir())
    except OSError:
        return []
    found = [entry for entry in entries if pattern.fullmatch(entry.name)]
    return sorted(found, key=lambda entry: entry.name)


def _next_segment(path: Path, now: datetime) -> Path:
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    candidate = path.with_name(f"{path.name}.{stamp}")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.{stamp}.{counter:04d}")
        counter += 1
    return candidate


def _oldest_record_time(path: Path) -> float | None:
    """Timestamp of the first JSONL record, or the file's mtime as fallback.

    Reading one line (not the file) lets the time trigger follow the oldest
    retained record: a continuously written audit log still rotates on schedule
    because its first line ages, while a restart minutes after a rotation sees
    a young first line and does not rotate again.
    """

    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    break
                if isinstance(row, Mapping):
                    for field in ("ts", "started_at", "finished_at"):
                        value = row.get(field)
                        if isinstance(value, str):
                            try:
                                moment = datetime.fromisoformat(
                                    value.replace("Z", "+00:00")
                                )
                            except ValueError:
                                continue
                            if moment.tzinfo is None:
                                moment = moment.replace(tzinfo=timezone.utc)
                            return moment.timestamp()
                break
    except OSError:
        return None
    try:
        return path.stat().st_mtime
    except OSError:  # pragma: no cover - file vanished
        return None


def rotate_jsonl(
    path: str | Path,
    *,
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    rotate_seconds: int = DEFAULT_LOG_ROTATE_SECONDS,
    ttl_seconds: int | None = None,
    keep: int = 1,
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Rotate one append-only JSONL file and expire old segments.

    A rotation is an atomic rename of the active file to a timestamped sibling;
    no line is copied, truncated, or scanned. That is what makes a concurrent
    append safe: an already-open descriptor keeps writing to the renamed inode,
    and the next writer creates a new active file. Expiry only unlinks whole
    segments, so a partially written line can never be split.
    """

    active = Path(path)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    now_seconds = current.timestamp()
    keep = max(0, int(keep))
    segments = _segments(active)
    rotated = False

    if active.exists():
        try:
            stat = active.stat()
        except OSError:
            stat = None
        if stat is not None and stat.st_size > 0:
            oldest = _oldest_record_time(active)
            due_time = (
                rotate_seconds > 0
                and oldest is not None
                and now_seconds - oldest >= rotate_seconds
            )
            due_size = max_bytes > 0 and stat.st_size >= max_bytes
            if force or due_size or due_time:
                destination = _next_segment(active, current)
                try:
                    os.replace(active, destination)
                except OSError:
                    pass
                else:
                    segments.append(destination)
                    rotated = True

    pruned = 0
    if ttl_seconds is not None and ttl_seconds > 0:
        threshold = now_seconds - ttl_seconds
        kept: list[Path] = []
        for segment in segments:
            # Judge a segment by its oldest record, not by an mtime a
            # concurrent append can refresh (or that a rename can stale).
            oldest = _oldest_record_time(segment)
            if oldest is None:
                try:
                    oldest = segment.stat().st_mtime
                except OSError:
                    oldest = None
            if oldest is not None and oldest < threshold:
                try:
                    segment.unlink()
                    pruned += 1
                except OSError:
                    kept.append(segment)
            else:
                kept.append(segment)
        segments = kept

    while len(segments) > keep:
        oldest = segments.pop(0)
        try:
            oldest.unlink()
            pruned += 1
        except OSError:
            segments.insert(0, oldest)
            break

    return {
        "rotated": rotated,
        "segments": len(segments),
        "kept": len(segments),
        "pruned": pruned,
    }


class RetentionMaintenance:
    """Boot-time and daily orphan sweep, share sweep, and log rotation.

    ``run_once`` is synchronous and returns a content-free report (counts
    only). ``start`` runs the same pass on a daemon thread at the configured
    interval; ``stop`` joins it before the store closes.
    """

    def __init__(
        self,
        *,
        store: Any,
        artifacts: Any,
        audit_path: str | Path,
        usage_path: str | Path | None = None,
        policy: RetentionPolicy | None = None,
        now: Any | None = None,
    ):
        self.store = store
        self.artifacts = artifacts
        self.audit_path = Path(audit_path)
        self.usage_path = Path(usage_path) if usage_path is not None else None
        self.policy = policy or RetentionPolicy()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def run_once(self) -> dict[str, Any]:
        current = self._now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        report: dict[str, Any] = {}
        orphans = self.artifacts.sweep_orphans(
            older_than_seconds=self.policy.artifact_orphan_seconds,
            now=current,
        )
        report["orphans"] = orphans
        # Share rows have no retention clock of their own; the sweep is the
        # only pruner outside `purge_thread`. It runs here, on the boot pass
        # and then daily, never on the share read path (a share GET mutates
        # nothing, so a reader cannot lose its row mid-request).
        report["shares"] = self._sweep_shares()
        report["audit"] = rotate_jsonl(
            self.audit_path,
            max_bytes=self.policy.log_max_bytes,
            rotate_seconds=self.policy.log_rotate_seconds,
            ttl_seconds=self.policy.audit_ttl_seconds,
            keep=self.policy.audit_keep,
            now=current,
        )
        if self.usage_path is not None:
            report["usage"] = rotate_jsonl(
                self.usage_path,
                max_bytes=self.policy.log_max_bytes,
                rotate_seconds=self.policy.log_rotate_seconds,
                ttl_seconds=self.policy.usage_ttl_seconds,
                keep=self.policy.usage_keep,
                now=current,
            )
        if orphans.get("artifact_rows") or report["shares"].get("shares"):
            report["vacuumed"] = self.store.vacuum()
        else:
            report["vacuumed"] = False
        return report

    def _sweep_shares(self) -> dict[str, int]:
        """Prune expired, revoked and orphaned share rows, tolerating absence.

        ``store`` is duck-typed (tests pass a spy), so an object without the
        share surface reports zero instead of failing the whole pass. A real
        sweep error is logged and does not abort the remainder of maintenance:
        rotation and the orphan sweep have already run, and the next pass
        retries.
        """

        sweep = getattr(self.store, "share_sweep", None)
        if sweep is None:
            return {"shares": 0, "views": 0}
        try:
            result = sweep()
        except Exception as exc:  # noqa: BLE001 - maintenance never fails traffic
            log(
                "share_sweep_failed",
                error_code=type(exc).__name__,
                status="error",
            )
            return {"shares": 0, "views": 0}
        if not isinstance(result, Mapping):
            return {"shares": 0, "views": 0}
        return {
            "shares": int(result.get("shares", 0) or 0),
            "views": int(result.get("views", 0) or 0),
        }

    def start(self) -> None:
        if self.policy.maintenance_interval_seconds <= 0:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="kelp-retention",
                daemon=True,
            )
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.policy.maintenance_interval_seconds):
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - never kill the process
                log(
                    "retention_sweep_failed",
                    error_code=type(exc).__name__,
                    status="error",
                )

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            thread, self._thread = self._thread, None
            self._stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

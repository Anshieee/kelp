"""Durable chat store (KEL-8).

SQLite read model plus the append-only wire log described in
``docs/CHAT_SPEC.md`` §13. One writer, ``0600``, WAL. Every state change and the
event that announces it are written under one transaction, so a snapshot can
never disagree with the stream.

The store knows nothing about HTTP, providers, or cards: payloads are opaque
JSON objects produced by ``chat.cards`` and ``chat.service``.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

__all__ = [
    "APPROVAL_TRANSITIONS",
    "ChatStore",
    "LIVE_TURN_STATES",
    "SHARE_ID_LENGTH",
    "ThreadBusyError",
    "ThreadGoneError",
    "iso_now",
    "new_id",
    "scrub_artifact_handles",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    title_source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    superseded_by TEXT,
    sources_json TEXT NOT NULL DEFAULT '[]',
    sources_total INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS messages_thread ON messages (thread_id, created_at, id);

CREATE TABLE IF NOT EXISTS blocks (
    message_id TEXT NOT NULL,
    block_index INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (message_id, block_index)
);

CREATE TABLE IF NOT EXISTS events (
    thread_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    ts TEXT NOT NULL,
    PRIMARY KEY (thread_id, seq)
);

CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    user_message_id TEXT NOT NULL,
    assistant_message_id TEXT NOT NULL,
    state TEXT NOT NULL,
    reason TEXT,
    started_at TEXT,
    finished_at TEXT,
    budget_json TEXT,
    usage_json TEXT,
    client_turn_id TEXT,
    retry_of TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS turns_client
    ON turns (thread_id, client_turn_id)
    WHERE client_turn_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS turns_thread ON turns (thread_id, created_at, id);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    turn_id TEXT,
    call_id TEXT,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_at TEXT,
    consumed_at TEXT
);
CREATE INDEX IF NOT EXISTS approvals_thread ON approvals (thread_id, requested_at);

CREATE TABLE IF NOT EXISTS artifacts (
    handle TEXT PRIMARY KEY,
    thread_id TEXT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    mime TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    deleted_at TEXT,
    pages INTEGER,
    rows INTEGER
);
CREATE INDEX IF NOT EXISTS artifacts_thread ON artifacts (thread_id, created_at);

-- KEL-24 M4-C: owner device sessions. Only the SHA-256 of the session id is
-- stored; the CSRF half is derived from it with a server-side secret, so no
-- session or CSRF credential is ever written here.
CREATE TABLE IF NOT EXISTS sessions (
    id_hash TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_seen REAL NOT NULL,
    expires_at REAL NOT NULL,
    revoked_at REAL
);
CREATE INDEX IF NOT EXISTS sessions_live ON sessions (expires_at);
CREATE INDEX IF NOT EXISTS sessions_seen ON sessions (last_seen);

-- KEL-28 M4-F: per-thread read-only share links. The raw token is never
-- stored; the primary key is its SHA-256. ``snapshot_json`` is the frozen
-- projection materialised at creation, so a share read never joins
-- ``threads`` and can never resolve a different thread. ``owner_id`` is the
-- one column that names the trust boundary (§9 of ADR 0002).
CREATE TABLE IF NOT EXISTS shares (
    token_hash TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL DEFAULT 'owner',
    thread_id TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    snapshot_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS shares_thread ON shares (thread_id, created_at);
CREATE INDEX IF NOT EXISTS shares_expiry ON shares (expires_at);

-- Aggregate daily share views, keyed by token hash. There is deliberately no
-- per-request row and no viewer identity, IP address, or user agent.
CREATE TABLE IF NOT EXISTS share_views (
    day TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    views INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, token_hash)
);
"""

_ARTIFACT_HANDLE_RE = re.compile(r"artifact:[A-Za-z0-9_-]{8,128}")
#: Public id of a share in the owner's listing (and in a revoke path): the
#: first 16 hex characters of ``sha256(token)``. The API and the store must
#: agree on this width, so the constant lives here.
SHARE_ID_LENGTH = 16
_SHARE_ID_RE = re.compile(rf"^[0-9a-f]{{{SHARE_ID_LENGTH}}}$")


def scrub_artifact_handles(text: str) -> str:
    """Replace every artifact handle embedded in free text with a marker.

    Handles are dropped structurally from the share projection, but a user or
    a tool can also type one into a message, a summary, or a citation. Those
    fields are legitimate display text, so they are scrubbed with the same
    pattern the orphan sweep uses rather than passed through.
    """

    return _ARTIFACT_HANDLE_RE.sub("[attachment]", text)


def iso_now(moment: datetime | None = None) -> str:
    """ISO-8601 UTC with a ``Z`` suffix, matching the chat wire format."""

    value = moment or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


# Which states may move into a given state. A settled approval is terminal, so
# no writer can reopen it: approve/deny once, consume once (§7.4).
APPROVAL_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "approved": ("pending",),
    "denied": ("pending",),
    "expired": ("pending", "approved"),
    "consumed": ("approved",),
}


def _dump(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))


def _load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:  # pragma: no cover - internal invariant
        return default


LIVE_TURN_STATES = ("queued", "running", "stopping")


class ThreadBusyError(RuntimeError):
    """A purge found a live turn in the thread and aborted before deleting.

    Raised from inside :meth:`ChatStore.purge_thread`'s transaction, so the
    row check and the deletes are one atomic decision: either the thread is
    quiet and everything goes, or nothing goes and the caller retries. The
    service maps this to ``409 turn_in_progress``.
    """

    def __init__(self, thread_id: str, turn_ids: Sequence[str]):
        self.thread_id = thread_id
        self.turn_ids = list(turn_ids)
        super().__init__(
            f"thread {thread_id} still has live turns: {', '.join(self.turn_ids)}"
        )


class ThreadGoneError(RuntimeError):
    """A write was aimed at a thread that no longer exists.

    :meth:`ChatStore.share_create` re-checks the parent inside its own write
    transaction: the conditional ``INSERT ... WHERE EXISTS`` inserts nothing
    when the thread was purged (by another process on the same file) after the
    caller read it. The API maps this to the ordinary ``404 unknown_thread``;
    the store never keeps an orphan share row.
    """

    def __init__(self, thread_id: str):
        self.thread_id = thread_id
        super().__init__(f"thread {thread_id} is gone")


class ChatStore:
    """Threads, messages, events, turns, approvals, and artifact metadata."""

    def __init__(
        self,
        path: str | Path,
        *,
        now: Callable[[], datetime] | None = None,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._depth = 0
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        try:
            os.chmod(self.path, 0o600)
        except OSError:  # pragma: no cover - platform dependent
            pass

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def vacuum(self) -> bool:
        """Reclaim free pages after a bulk delete.

        ``VACUUM`` is a whole-database rewrite, so it runs only when this call
        owns the outermost transaction. A caller already inside
        :meth:`transaction` must commit first and vacuum afterwards; in that
        case this is a no-op and returns ``False``.
        """

        with self._lock:
            if self._depth:
                return False
            try:
                self._conn.execute("VACUUM")
            except sqlite3.OperationalError:  # pragma: no cover - busy reader
                return False
            try:
                # A WAL keeps the pre-vacuum pages alive until it is
                # checkpointed; truncate it so the file on disk actually
                # shrinks. Another reader can defer this, never fail a delete.
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.OperationalError:  # pragma: no cover - busy reader
                pass
            return True

    def __enter__(self) -> "ChatStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Re-entrant write transaction; nested blocks share the outer one."""

        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            self._conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield
            except BaseException:
                self._depth = 0
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._depth = 0
                self._conn.execute("COMMIT")

    def _now_iso(self) -> str:
        return iso_now(self._now())

    def now_iso(self) -> str:
        """The store clock as the ISO string every share comparison uses."""

        return self._now_iso()

    # -- threads -----------------------------------------------------------

    def create_thread(
        self,
        *,
        title: str | None = None,
        title_source: str = "auto",
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        thread_id = thread_id or new_id("ses")
        now = self._now_iso()
        clean_title = (title or "").strip() or "New chat"
        with self.transaction():
            self._conn.execute(
                "INSERT INTO threads (id, title, title_source, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (thread_id, clean_title, title_source, now, now),
            )
        return self.get_thread(thread_id)  # type: ignore[return-value]

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM threads WHERE id = ? AND deleted_at IS NULL",
                (thread_id,),
            ).fetchone()
            if row is None:
                return None
            return self._thread_record(row)

    def thread_exists(self, thread_id: str) -> bool:
        return self.get_thread(thread_id) is not None

    def touch_thread(self, thread_id: str, *, title: str | None = None) -> None:
        now = self._now_iso()
        with self.transaction():
            if title is not None:
                self._conn.execute(
                    "UPDATE threads SET title = ?, title_source = 'user', updated_at = ? "
                    "WHERE id = ?",
                    (title, now, thread_id),
                )
            else:
                self._conn.execute(
                    "UPDATE threads SET updated_at = ? WHERE id = ?", (now, thread_id)
                )

    def set_auto_title(self, thread_id: str, title: str) -> bool:
        """Set the title only while it is still auto-generated."""

        with self.transaction():
            cursor = self._conn.execute(
                "UPDATE threads SET title = ?, updated_at = ? "
                "WHERE id = ? AND title_source = 'auto' AND deleted_at IS NULL",
                (title, self._now_iso(), thread_id),
            )
            return cursor.rowcount > 0

    def rename_thread(self, thread_id: str, title: str) -> dict[str, Any] | None:
        with self.transaction():
            cursor = self._conn.execute(
                "UPDATE threads SET title = ?, title_source = 'user', updated_at = ? "
                "WHERE id = ? AND deleted_at IS NULL",
                (title, self._now_iso(), thread_id),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_thread(thread_id)

    def soft_delete_thread(self, thread_id: str) -> bool:
        with self.transaction():
            cursor = self._conn.execute(
                "UPDATE threads SET deleted_at = ?, updated_at = ? "
                "WHERE id = ? AND deleted_at IS NULL",
                (self._now_iso(), self._now_iso(), thread_id),
            )
            return cursor.rowcount > 0

    def list_threads(
        self, *, limit: int = 50, cursor: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        limit = max(1, min(int(limit), 100))
        params: list[Any] = []
        where = "deleted_at IS NULL"
        if cursor:
            marker = _decode_cursor(cursor)
            if marker is not None:
                where += " AND (updated_at < ? OR (updated_at = ? AND id < ?))"
                params.extend([marker[0], marker[0], marker[1]])
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM threads WHERE {where} "
                "ORDER BY updated_at DESC, id DESC LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        threads = [self._thread_record(row) for row in rows]
        next_cursor = None
        if has_more and rows:
            next_cursor = _encode_cursor(rows[-1]["updated_at"], rows[-1]["id"])
        return threads, next_cursor

    def _thread_record(self, row: sqlite3.Row) -> dict[str, Any]:
        thread_id = row["id"]
        active = self._conn.execute(
            "SELECT id FROM turns WHERE thread_id = ? "
            "AND state IN ('queued', 'running', 'stopping') LIMIT 1",
            (thread_id,),
        ).fetchone()
        count = self._conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE thread_id = ?", (thread_id,)
        ).fetchone()["n"]
        preview = ""
        last = self._conn.execute(
            "SELECT id FROM messages WHERE thread_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
        if last is not None:
            blocks = self.get_blocks(last["id"])
            for block in reversed(blocks):
                if block.get("type") == "text" and block.get("text"):
                    preview = " ".join(str(block["text"]).split())[:80]
                    break
        return {
            "id": thread_id,
            "title": row["title"],
            "title_source": row["title_source"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_message_preview": preview,
            "message_count": count,
            "active_turn_id": active["id"] if active is not None else None,
        }

    # -- messages ----------------------------------------------------------

    def create_message(
        self,
        *,
        thread_id: str,
        turn_id: str,
        role: str,
        status: str,
        blocks: Sequence[Mapping[str, Any]] = (),
        message_id: str | None = None,
    ) -> dict[str, Any]:
        message_id = message_id or new_id("msg")
        now = self._now_iso()
        with self.transaction():
            self._conn.execute(
                "INSERT INTO messages (id, thread_id, turn_id, role, status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (message_id, thread_id, turn_id, role, status, now, now),
            )
            self._write_blocks(message_id, blocks)
        return self.get_message(message_id)  # type: ignore[return-value]

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            if row is None:
                return None
            return self._message_record(row)

    def _message_record(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "thread_id": row["thread_id"],
            "turn_id": row["turn_id"],
            "role": row["role"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "superseded_by": row["superseded_by"],
            "blocks": self.get_blocks(row["id"]),
            "sources": _load(row["sources_json"], []),
            "sources_total": row["sources_total"],
        }

    def update_message(
        self,
        message_id: str,
        *,
        status: str | None = None,
        superseded_by: str | None = None,
        blocks: Sequence[Mapping[str, Any]] | None = None,
        sources: Sequence[Mapping[str, Any]] | None = None,
        sources_total: int | None = None,
    ) -> None:
        now = self._now_iso()
        with self.transaction():
            if status is not None:
                self._conn.execute(
                    "UPDATE messages SET status = ?, updated_at = ? WHERE id = ?",
                    (status, now, message_id),
                )
            if superseded_by is not None:
                self._conn.execute(
                    "UPDATE messages SET superseded_by = ?, updated_at = ? WHERE id = ?",
                    (superseded_by, now, message_id),
                )
            if blocks is not None:
                self._write_blocks(message_id, blocks)
                self._conn.execute(
                    "UPDATE messages SET updated_at = ? WHERE id = ?", (now, message_id)
                )
            if sources is not None:
                total = sources_total if sources_total is not None else len(sources)
                self._conn.execute(
                    "UPDATE messages SET sources_json = ?, sources_total = ?, "
                    "updated_at = ? WHERE id = ?",
                    (_dump(list(sources)), total, now, message_id),
                )

    def _write_blocks(
        self, message_id: str, blocks: Sequence[Mapping[str, Any]]
    ) -> None:
        self._conn.execute("DELETE FROM blocks WHERE message_id = ?", (message_id,))
        for index, block in enumerate(blocks):
            self._conn.execute(
                "INSERT INTO blocks (message_id, block_index, kind, payload_json) "
                "VALUES (?, ?, ?, ?)",
                (message_id, index, str(block.get("type", "unknown")), _dump(block)),
            )

    def get_blocks(self, message_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload_json FROM blocks WHERE message_id = ? "
                "ORDER BY block_index",
                (message_id,),
            ).fetchall()
        return [_load(row["payload_json"], {}) for row in rows]

    def list_messages(
        self,
        thread_id: str,
        *,
        limit: int = 50,
        before: str | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        limit = max(1, min(int(limit), 100))
        params: list[Any] = [thread_id]
        clause = ""
        if before:
            marker = self._conn.execute(
                "SELECT created_at, id FROM messages WHERE id = ? AND thread_id = ?",
                (before, thread_id),
            ).fetchone()
            if marker is not None:
                clause = " AND (created_at < ? OR (created_at = ? AND id < ?))"
                params.extend([marker["created_at"], marker["created_at"], marker["id"]])
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE thread_id = ?" + clause +
                " ORDER BY created_at DESC, id DESC LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        messages = [self._message_record(row) for row in reversed(rows)]
        return messages, has_more

    # -- turns -------------------------------------------------------------

    def create_turn(
        self,
        *,
        thread_id: str,
        user_message_id: str,
        assistant_message_id: str,
        client_turn_id: str | None,
        budget: Mapping[str, Any],
        retry_of: str | None = None,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        turn_id = turn_id or new_id("turn")
        with self.transaction():
            self._conn.execute(
                "INSERT INTO turns (id, thread_id, user_message_id, "
                "assistant_message_id, state, budget_json, client_turn_id, retry_of, "
                "created_at) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
                (
                    turn_id,
                    thread_id,
                    user_message_id,
                    assistant_message_id,
                    _dump(dict(budget)),
                    client_turn_id,
                    retry_of,
                    self._now_iso(),
                ),
            )
        return self.get_turn(turn_id)  # type: ignore[return-value]

    def get_turn(self, turn_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM turns WHERE id = ?", (turn_id,)
            ).fetchone()
        return self._turn_record(row) if row is not None else None

    def _turn_record(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "thread_id": row["thread_id"],
            "user_message_id": row["user_message_id"],
            "assistant_message_id": row["assistant_message_id"],
            "state": row["state"],
            "reason": row["reason"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "budget": _load(row["budget_json"], {}),
            "usage": _load(row["usage_json"], {}),
            "client_turn_id": row["client_turn_id"],
            "retry_of": row["retry_of"],
            "created_at": row["created_at"],
        }

    def find_turn_by_client_id(
        self, thread_id: str, client_turn_id: str
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM turns WHERE thread_id = ? AND client_turn_id = ?",
                (thread_id, client_turn_id),
            ).fetchone()
        return self._turn_record(row) if row is not None else None

    def latest_turn(self, thread_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM turns WHERE thread_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
        return self._turn_record(row) if row is not None else None

    def update_turn(
        self,
        turn_id: str,
        *,
        state: str | None = None,
        reason: str | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
        usage: Mapping[str, Any] | None = None,
    ) -> None:
        fields: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("state", state),
            ("reason", reason),
            ("started_at", started_at),
            ("finished_at", finished_at),
        ):
            if value is not None:
                fields.append(f"{column} = ?")
                values.append(value)
        if usage is not None:
            fields.append("usage_json = ?")
            values.append(_dump(dict(usage)))
        if not fields:
            return
        values.append(turn_id)
        with self.transaction():
            self._conn.execute(
                f"UPDATE turns SET {', '.join(fields)} WHERE id = ?", values
            )

    def running_turns(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM turns WHERE state IN ('queued', 'running', 'stopping') "
                "ORDER BY created_at"
            ).fetchall()
        return [self._turn_record(row) for row in rows]

    # -- events ------------------------------------------------------------

    def append_event(
        self,
        thread_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        ts: str | None = None,
    ) -> dict[str, Any]:
        body = dict(payload or {})
        with self.transaction():
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM events WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            seq = int(row["next"])
            stamp = ts or self._now_iso()
            self._conn.execute(
                "INSERT INTO events (thread_id, seq, type, payload_json, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (thread_id, seq, event_type, _dump(body), stamp),
            )
        return {
            "seq": seq,
            "thread_id": thread_id,
            "type": event_type,
            "ts": stamp,
            **body,
        }

    def events_after(
        self, thread_id: str, after: int = 0, *, limit: int = 500
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE thread_id = ? AND seq > ? "
                "ORDER BY seq LIMIT ?",
                (thread_id, int(after), max(1, int(limit))),
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            event = {
                "seq": row["seq"],
                "thread_id": row["thread_id"],
                "type": row["type"],
                "ts": row["ts"],
            }
            event.update(_load(row["payload_json"], {}))
            events.append(event)
        return events

    def last_seq(self, thread_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS last FROM events WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        return int(row["last"])

    # -- approvals ---------------------------------------------------------

    def save_approval(self, card: Mapping[str, Any]) -> dict[str, Any]:
        """Insert a card once; a later write never moves its state columns.

        The payload is refreshed so a caller that re-saves the same card gets
        its own shape back, but ``status``/``decided_at``/``consumed_at`` belong
        to :meth:`transition_approval`. Otherwise a stale writer could undo a
        decision or a consumption.
        """

        card = dict(card)
        with self.transaction():
            self._conn.execute(
                "INSERT INTO approvals (id, thread_id, turn_id, call_id, source, "
                "status, payload_json, requested_at, expires_at, decided_at, "
                "consumed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload_json = excluded.payload_json",
                (
                    card["id"],
                    card.get("thread_id") or "",
                    card.get("turn_id"),
                    card.get("call_id"),
                    card.get("source") or "skill",
                    card.get("status") or "pending",
                    _dump(card),
                    card.get("requested_at") or self._now_iso(),
                    card.get("expires_at") or self._now_iso(),
                    card.get("decided_at"),
                    card.get("consumed_at"),
                ),
            )
        return self.get_approval(card["id"])  # type: ignore[return-value]

    def transition_approval(
        self,
        approval_id: str,
        status: str,
        *,
        allowed_from: Sequence[str] = ("pending",),
        decided_at: str | None = None,
        consumed_at: str | None = None,
    ) -> dict[str, Any] | None:
        """Guarded, atomic status change. ``None`` means another caller won.

        The guard is the SQL predicate itself (``WHERE status IN (...)``) and
        the winner is ``rowcount``, so two concurrent decisions — or a decision
        racing a settle — cannot both move the row.
        """

        fields = ["status = ?"]
        values: list[Any] = [status]
        if decided_at is not None:
            fields.append("decided_at = ?")
            values.append(decided_at)
        if consumed_at is not None:
            fields.append("consumed_at = ?")
            values.append(consumed_at)
        allowed = tuple(dict.fromkeys(str(item) for item in allowed_from))
        if not allowed:  # pragma: no cover - callers always name a source state
            raise ValueError("allowed_from must name at least one status")
        placeholders = ", ".join("?" for _ in allowed)
        with self.transaction():
            cursor = self._conn.execute(
                f"UPDATE approvals SET {', '.join(fields)} "
                f"WHERE id = ? AND status IN ({placeholders})",
                (*values, approval_id, *allowed),
            )
            if cursor.rowcount == 0:
                return None
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        return self._approval_record(row) if row is not None else None

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        return self._approval_record(row) if row is not None else None

    def _approval_record(self, row: sqlite3.Row) -> dict[str, Any]:
        card = _load(row["payload_json"], {})
        card["status"] = row["status"]
        card["decided_at"] = row["decided_at"]
        card["consumed_at"] = row["consumed_at"]
        return card

    def list_approvals(
        self,
        *,
        status: str | None = None,
        thread_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if thread_id:
            clauses.append("thread_id = ?")
            params.append(thread_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM approvals" + where +
                " ORDER BY requested_at DESC LIMIT ?",
                (*params, max(1, min(int(limit), 200))),
            ).fetchall()
        return [self._approval_record(row) for row in rows]

    def set_approval_status(
        self,
        approval_id: str,
        status: str,
        *,
        decided_at: str | None = None,
        consumed_at: str | None = None,
        allowed_from: Sequence[str] | None = None,
    ) -> dict[str, Any] | None:
        """Move an approval along its lifecycle; never reopens a settled one."""

        if self.get_approval(approval_id) is None:
            return None
        allowed = (
            allowed_from
            if allowed_from is not None
            else APPROVAL_TRANSITIONS.get(status, ())
        )
        updated = self.transition_approval(
            approval_id,
            status,
            allowed_from=allowed,
            decided_at=decided_at,
            consumed_at=consumed_at,
        )
        return updated if updated is not None else self.get_approval(approval_id)

    # -- artifacts ---------------------------------------------------------

    def create_artifact(self, meta: Mapping[str, Any]) -> dict[str, Any]:
        card = dict(meta)
        with self.transaction():
            self._conn.execute(
                "INSERT INTO artifacts (handle, thread_id, name, kind, mime, "
                "size_bytes, sha256, storage_path, created_at, pages, rows) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    card["handle"],
                    card.get("thread_id"),
                    card["name"],
                    card["kind"],
                    card["mime"],
                    int(card["size_bytes"]),
                    card["sha256"],
                    card["storage_path"],
                    card.get("created_at") or self._now_iso(),
                    card.get("pages"),
                    card.get("rows"),
                ),
            )
        return self.get_artifact(card["handle"])  # type: ignore[return-value]

    def get_artifact(self, handle: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM artifacts WHERE handle = ? AND deleted_at IS NULL",
                (handle,),
            ).fetchone()
        return self._artifact_record(row) if row is not None else None

    def _artifact_record(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "handle": row["handle"],
            "thread_id": row["thread_id"],
            "name": row["name"],
            "kind": row["kind"],
            "mime": row["mime"],
            "size_bytes": row["size_bytes"],
            "sha256": row["sha256"],
            "storage_path": row["storage_path"],
            "created_at": row["created_at"],
            "pages": row["pages"],
            "rows": row["rows"],
        }

    def bind_artifact(self, handle: str, thread_id: str) -> bool:
        """Claim an unbound upload for one thread. Never re-points a bound one."""

        with self.transaction():
            cursor = self._conn.execute(
                "UPDATE artifacts SET thread_id = ? "
                "WHERE handle = ? AND thread_id IS NULL AND deleted_at IS NULL",
                (thread_id, handle),
            )
            return cursor.rowcount > 0

    def delete_artifacts_for_thread(self, thread_id: str) -> None:
        with self.transaction():
            self._conn.execute(
                "UPDATE artifacts SET deleted_at = ? WHERE thread_id = ?",
                (self._now_iso(), thread_id),
            )

    # -- owner sessions (KEL-24 M4-C) --------------------------------------

    def session_insert(
        self,
        id_hash: str,
        label: str,
        created_at: float,
        last_seen: float,
        expires_at: float,
    ) -> None:
        """Persist one session row. The primary key is the hashed id only."""

        with self.transaction():
            self._conn.execute(
                "INSERT INTO sessions "
                "(id_hash, label, created_at, last_seen, expires_at, revoked_at) "
                "VALUES (?, ?, ?, ?, ?, NULL) "
                "ON CONFLICT(id_hash) DO UPDATE SET "
                "label = excluded.label, created_at = excluded.created_at, "
                "last_seen = excluded.last_seen, expires_at = excluded.expires_at, "
                "revoked_at = NULL",
                (id_hash, label, created_at, last_seen, expires_at),
            )

    def session_get(self, id_hash: str, *, now: float) -> dict[str, Any] | None:
        """Return a live, unrevoked session row, or ``None``."""

        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE id_hash = ? "
                "AND revoked_at IS NULL AND expires_at > ?",
                (id_hash, now),
            ).fetchone()
        return dict(row) if row is not None else None

    def session_touch(
        self, id_hash: str, *, last_seen: float, expires_at: float
    ) -> bool:
        """Roll one live session forward; ``False`` if it is gone or expired."""

        with self.transaction():
            cursor = self._conn.execute(
                "UPDATE sessions SET last_seen = ?, expires_at = ? "
                "WHERE id_hash = ? AND revoked_at IS NULL AND expires_at > ?",
                (last_seen, expires_at, id_hash, last_seen),
            )
            return cursor.rowcount > 0

    def session_revoke(self, id_hash: str, *, revoked_at: float) -> bool:
        """Mark one session revoked. Revoking twice is idempotent."""

        with self.transaction():
            cursor = self._conn.execute(
                "UPDATE sessions SET revoked_at = ? "
                "WHERE id_hash = ? AND revoked_at IS NULL",
                (revoked_at, id_hash),
            )
            return cursor.rowcount > 0

    def session_revoke_all(self, *, revoked_at: float) -> int:
        with self.transaction():
            return self._conn.execute(
                "UPDATE sessions SET revoked_at = ? WHERE revoked_at IS NULL",
                (revoked_at,),
            ).rowcount

    def session_list(self, *, now: float) -> list[dict[str, Any]]:
        """Live sessions, most recently seen first. No label is ever logged."""

        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sessions WHERE revoked_at IS NULL AND expires_at > ? "
                "ORDER BY last_seen DESC, id_hash ASC",
                (now,),
            ).fetchall()
        return [dict(row) for row in rows]

    def session_find_prefix(
        self, prefix: str, *, now: float
    ) -> dict[str, Any] | None:
        """Resolve a short id prefix only when exactly one live row matches."""

        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sessions WHERE revoked_at IS NULL AND expires_at > ? "
                "AND substr(id_hash, 1, ?) = ? LIMIT 2",
                (now, len(prefix), prefix),
            ).fetchall()
        return dict(rows[0]) if len(rows) == 1 else None

    def session_count(self, *, now: float) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM sessions "
                "WHERE revoked_at IS NULL AND expires_at > ?",
                (now,),
            ).fetchone()
        return int(row["n"])

    def session_enforce_max(self, max_entries: int, *, now: float) -> int:
        """Delete the oldest live sessions until at most ``max_entries`` remain."""

        removed = 0
        with self.transaction():
            while True:
                row = self._conn.execute(
                    "SELECT id_hash FROM sessions "
                    "WHERE revoked_at IS NULL AND expires_at > ? "
                    "ORDER BY last_seen ASC, id_hash ASC LIMIT 1",
                    (now,),
                ).fetchone()
                live = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM sessions "
                    "WHERE revoked_at IS NULL AND expires_at > ?",
                    (now,),
                ).fetchone()["n"]
                if row is None or live <= max_entries:
                    return removed
                self._conn.execute(
                    "DELETE FROM sessions WHERE id_hash = ?", (row["id_hash"],)
                )
                removed += 1

    def session_prune(self, *, now: float, revoked_before: float) -> int:
        """Delete expired rows and revoked rows past their audit grace.

        A revoked row is kept only until ``expires_at`` (already bounded by the
        session TTL), so the table cannot grow without limit even when
        devices are revoked rather than evicted.
        """

        with self.transaction():
            return self._conn.execute(
                "DELETE FROM sessions "
                "WHERE expires_at <= ? OR (revoked_at IS NOT NULL AND revoked_at <= ?)",
                (now, revoked_before),
            ).rowcount

    # -- share links (KEL-28 M4-F) -----------------------------------------

    def share_create(
        self,
        *,
        token_hash: str,
        thread_id: str,
        snapshot: Mapping[str, Any],
        ttl_seconds: float,
    ) -> dict[str, Any]:
        """Insert one frozen snapshot. The raw token never reaches this layer.

        The store clock stamps ``created_at``/``expires_at`` into the snapshot
        as well as the row, so the response and every later read agree on one
        clock. A new share is always a new row and a new token; there is no
        update-in-place path (ADR 0002 §16).

        The insert re-checks the parent inside this write transaction with
        ``WHERE EXISTS``: a purge that committed between the caller's read and
        this statement (another process on the same file) inserts no row and
        raises :class:`ThreadGoneError`, so an orphan share can never exist.
        """

        now = self._now()
        created_at = iso_now(now)
        expires_at = iso_now(now + timedelta(seconds=max(0.0, float(ttl_seconds))))
        frozen = dict(snapshot)
        frozen["created_at"] = created_at
        frozen["expires_at"] = expires_at
        payload = _dump(frozen)
        with self.transaction():
            cursor = self._conn.execute(
                "INSERT INTO shares (token_hash, owner_id, thread_id, "
                "snapshot_json, snapshot_bytes, created_at, expires_at, "
                "revoked_at) SELECT ?, 'owner', ?, ?, ?, ?, ?, NULL "
                "WHERE EXISTS (SELECT 1 FROM threads t WHERE t.id = ? "
                "AND t.deleted_at IS NULL)",
                (
                    token_hash,
                    thread_id,
                    payload,
                    len(payload.encode("utf-8")),
                    created_at,
                    expires_at,
                    thread_id,
                ),
            )
            if cursor.rowcount == 0:
                raise ThreadGoneError(thread_id)
        return self.share_get_any(token_hash)  # type: ignore[return-value]

    def _share_record(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "token_hash": row["token_hash"],
            "owner_id": row["owner_id"],
            "thread_id": row["thread_id"],
            "snapshot": _load(row["snapshot_json"], {}),
            "snapshot_bytes": int(row["snapshot_bytes"]),
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "revoked_at": row["revoked_at"],
        }

    def share_get(
        self, token_hash: str, *, now: str | None = None
    ) -> dict[str, Any] | None:
        """Resolve a live link by its hash. Unknown, expired and revoked are ``None``.

        This is the whole read path: one keyed lookup, no join to ``threads``,
        and no mutation, so every failure is indistinguishable by construction.
        """

        moment = now or self._now_iso()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM shares WHERE token_hash = ? "
                "AND revoked_at IS NULL AND expires_at > ?",
                (token_hash, moment),
            ).fetchone()
        return self._share_record(row) if row is not None else None

    def share_get_any(self, token_hash: str) -> dict[str, Any] | None:
        """Return a row whatever its state, for the owner's own listing."""

        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM shares WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        return self._share_record(row) if row is not None else None

    def share_list(self, thread_id: str) -> list[dict[str, Any]]:
        """All rows for one thread, newest first. Includes revoked and expired."""

        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM shares WHERE thread_id = ? "
                "ORDER BY created_at DESC, token_hash ASC",
                (thread_id,),
            ).fetchall()
        return [self._share_record(row) for row in rows]

    def share_revoke(self, token_hash: str, *, revoked_at: str | None = None) -> bool:
        """Set ``revoked_at`` once. The row is kept for the sweep, not deleted."""

        stamp = revoked_at or self._now_iso()
        with self.transaction():
            cursor = self._conn.execute(
                "UPDATE shares SET revoked_at = ? "
                "WHERE token_hash = ? AND revoked_at IS NULL",
                (stamp, token_hash),
            )
            return cursor.rowcount > 0

    def share_revoke_thread(self, thread_id: str) -> list[str]:
        """Revoke every live link for one thread in one transaction."""

        stamp = self._now_iso()
        with self.transaction():
            rows = self._conn.execute(
                "SELECT token_hash FROM shares WHERE thread_id = ? "
                "AND revoked_at IS NULL",
                (thread_id,),
            ).fetchall()
            hashes = [str(row["token_hash"]) for row in rows]
            if hashes:
                self._conn.execute(
                    "UPDATE shares SET revoked_at = ? WHERE thread_id = ? "
                    "AND revoked_at IS NULL",
                    (stamp, thread_id),
                )
        return hashes

    def share_revoke_prefix(
        self, thread_id: str, prefix: str, *, revoked_at: str | None = None
    ) -> dict[str, Any] | None:
        """Revoke one link by its public id, only when the shape is exact.

        The listing's ``id`` is the first :data:`SHARE_ID_LENGTH` hex characters
        of the token hash. Anything else — empty, a shorter prefix, uppercase,
        a full token — is not a public id and is refused without a query, so
        the short id is the only thing this route ever accepts.
        """

        if not _SHARE_ID_RE.fullmatch(prefix or ""):
            return None
        stamp = revoked_at or self._now_iso()
        with self.transaction():
            rows = self._conn.execute(
                "SELECT * FROM shares WHERE thread_id = ? "
                "AND revoked_at IS NULL AND substr(token_hash, 1, ?) = ? "
                "LIMIT 2",
                (thread_id, SHARE_ID_LENGTH, prefix),
            ).fetchall()
            if len(rows) != 1:
                return None
            record = self._share_record(rows[0])
            self._conn.execute(
                "UPDATE shares SET revoked_at = ? WHERE token_hash = ?",
                (stamp, record["token_hash"]),
            )
            record["revoked_at"] = stamp
        return record

    def share_record_view(
        self, token_hash: str, *, day: str | None = None
    ) -> int:
        """Increment the aggregate counter and return that day's total.

        One row per ``(day, token_hash)``. No viewer identity, address, or
        agent is stored; a first view of the day is also the only audit event
        the API writes for that day.
        """

        bucket = day or self._now_iso()[:10]
        with self.transaction():
            self._conn.execute(
                "INSERT INTO share_views (day, token_hash, views) VALUES (?, ?, 1) "
                "ON CONFLICT(day, token_hash) DO UPDATE SET views = views + 1",
                (bucket, token_hash),
            )
            row = self._conn.execute(
                "SELECT views FROM share_views WHERE day = ? AND token_hash = ?",
                (bucket, token_hash),
            ).fetchone()
        return int(row["views"]) if row is not None else 0

    def share_views_total(self, token_hash: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(views), 0) AS n FROM share_views "
                "WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
        return int(row["n"])

    def share_sweep(
        self, *, revoked_grace_seconds: float = 0.0
    ) -> dict[str, int]:
        """Delete expired, revoked and orphaned share rows outside a read path.

        Boot/daily maintenance and share creation call this. The share GET
        never does, so a concurrent reader cannot lose its row mid-request.

        "Orphaned" means the parent thread is gone *or soft-deleted*: the read
        path already treats a ``deleted_at`` row as absent, so a link that still
        resolved against it would outlive the thread it copies.
        """

        moment = self._now()
        now = iso_now(moment)
        revoked_before = iso_now(
            moment - timedelta(seconds=max(0.0, float(revoked_grace_seconds)))
        )
        with self.transaction():
            rows = self._conn.execute(
                "SELECT token_hash FROM shares WHERE expires_at <= ? "
                "OR (revoked_at IS NOT NULL AND revoked_at <= ?) "
                "OR NOT EXISTS (SELECT 1 FROM threads t WHERE t.id = shares.thread_id "
                "AND t.deleted_at IS NULL)",
                (now, revoked_before),
            ).fetchall()
            hashes = [str(row["token_hash"]) for row in rows]
            views = 0
            for start in range(0, len(hashes), 500):
                chunk = hashes[start : start + 500]
                placeholders = ", ".join("?" for _ in chunk)
                views += self._conn.execute(
                    f"DELETE FROM share_views WHERE token_hash IN ({placeholders})",
                    chunk,
                ).rowcount
            removed = 0
            if hashes:
                for start in range(0, len(hashes), 500):
                    chunk = hashes[start : start + 500]
                    placeholders = ", ".join("?" for _ in chunk)
                    removed += self._conn.execute(
                        f"DELETE FROM shares WHERE token_hash IN ({placeholders})",
                        chunk,
                    ).rowcount
        return {"shares": removed, "views": views}

    # -- retention ---------------------------------------------------------

    def purge_thread(
        self,
        thread_id: str,
        *,
        on_artifacts: Callable[[Sequence[Mapping[str, Any]]], Any] | None = None,
        vacuum: bool = True,
    ) -> dict[str, Any]:
        """Hard-delete one thread and everything that belongs to it.

        Every row is removed in a single transaction: messages, their blocks,
        the wire log, turns, approvals, artifact registry rows, and the thread
        itself. Nothing is left flagged ``deleted_at`` — a second call finds no
        rows and returns zero counts instead of failing.

        The live-turn check runs *inside* that transaction, before any delete:
        a turn in ``queued``, ``running`` or ``stopping`` makes a purge
        impossible to do cleanly (its finalize step would append events and
        update rows that no longer exist), so the transaction aborts with
        :class:`ThreadBusyError` and the caller retries after the turn settles.
        No partial delete can be observed.

        Filesystem work cannot join a SQLite transaction, so the artifact
        metadata is captured before the delete and handed to ``on_artifacts``
        *after* commit. That callback owns unlinking bytes and dropping the
        derived index. A callback failure is reported, not raised: the rows are
        already gone, and the orphan sweep will retry the leftovers.

        ``VACUUM`` runs after commit when this call owns the transaction, so
        the space a thread occupied is actually returned to the filesystem.
        """

        artifact_rows: list[dict[str, Any]] = []
        counts = {
            "messages": 0,
            "blocks": 0,
            "events": 0,
            "turns": 0,
            "approvals": 0,
            "artifacts": 0,
            "shares": 0,
            "share_views": 0,
        }
        deleted_thread = False
        with self.transaction():
            live = self._conn.execute(
                "SELECT id FROM turns WHERE thread_id = ? AND state IN "
                f"({', '.join('?' for _ in LIVE_TURN_STATES)}) "
                "ORDER BY created_at, rowid",
                (thread_id, *LIVE_TURN_STATES),
            ).fetchall()
            if live:
                raise ThreadBusyError(thread_id, [row["id"] for row in live])
            artifact_rows = [
                self._artifact_record(row)
                for row in self._conn.execute(
                    "SELECT * FROM artifacts WHERE thread_id = ?", (thread_id,)
                ).fetchall()
            ]
            counts["blocks"] = self._conn.execute(
                "DELETE FROM blocks WHERE message_id IN "
                "(SELECT id FROM messages WHERE thread_id = ?)",
                (thread_id,),
            ).rowcount
            counts["messages"] = self._conn.execute(
                "DELETE FROM messages WHERE thread_id = ?", (thread_id,)
            ).rowcount
            counts["events"] = self._conn.execute(
                "DELETE FROM events WHERE thread_id = ?", (thread_id,)
            ).rowcount
            counts["approvals"] = self._conn.execute(
                "DELETE FROM approvals WHERE thread_id = ?", (thread_id,)
            ).rowcount
            counts["turns"] = self._conn.execute(
                "DELETE FROM turns WHERE thread_id = ?", (thread_id,)
            ).rowcount
            counts["artifacts"] = self._conn.execute(
                "DELETE FROM artifacts WHERE thread_id = ?", (thread_id,)
            ).rowcount
            # A share never survives its thread: the snapshot, every link and
            # the aggregate view counters go in the same transaction (§8).
            share_hashes = [
                str(row["token_hash"])
                for row in self._conn.execute(
                    "SELECT token_hash FROM shares WHERE thread_id = ?",
                    (thread_id,),
                ).fetchall()
            ]
            counts["shares"] = self._conn.execute(
                "DELETE FROM shares WHERE thread_id = ?", (thread_id,)
            ).rowcount
            for start in range(0, len(share_hashes), 500):
                chunk = share_hashes[start : start + 500]
                placeholders = ", ".join("?" for _ in chunk)
                counts["share_views"] += self._conn.execute(
                    f"DELETE FROM share_views WHERE token_hash IN ({placeholders})",
                    chunk,
                ).rowcount
            deleted_thread = (
                self._conn.execute(
                    "DELETE FROM threads WHERE id = ?", (thread_id,)
                ).rowcount
                > 0
            )

        report: dict[str, Any] = {
            "thread_id": thread_id,
            "deleted": deleted_thread,
            **counts,
            "artifact_handles": [row["handle"] for row in artifact_rows],
            "vacuumed": False,
        }
        if on_artifacts is not None and artifact_rows:
            try:
                report["files"] = on_artifacts(artifact_rows)
            except Exception as exc:  # noqa: BLE001 - rows are already gone
                report["files"] = {
                    "error": type(exc).__name__,
                    "retry": "orphan_sweep",
                }
        if vacuum:
            report["vacuumed"] = self.vacuum()
        return report

    def _orphan_filter(
        self,
        alias: str,
        cutoff: str,
        skip_handles: Sequence[str],
    ) -> tuple[str, list[Any]]:
        """SQL predicate for "older than the grace window and parentless".

        ``parentless`` is evaluated by the database at the moment the
        statement runs: a row whose ``thread_id`` is unset, whose parent thread
        no longer exists, or whose parent is soft-deleted. A soft-deleted
        thread is invisible to every read, so its uploads are unreachable the
        same way an interrupted delete's rows are. Both the read and the delete
        go through this one predicate so they cannot drift apart.
        """

        clauses = [
            f"{alias}.created_at < ?",
            f"({alias}.thread_id IS NULL OR NOT EXISTS "
            f"(SELECT 1 FROM threads t WHERE t.id = {alias}.thread_id "
            "AND t.deleted_at IS NULL))",
        ]
        params: list[Any] = [cutoff]
        skipped = tuple(dict.fromkeys(str(handle) for handle in skip_handles))
        if skipped:
            placeholders = ", ".join("?" for _ in skipped)
            clauses.append(f"{alias}.handle NOT IN ({placeholders})")
            params.extend(skipped)
        return " AND ".join(clauses), params

    def orphan_artifact_rows(
        self,
        cutoff: str,
        *,
        skip_handles: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        """Artifact rows older than ``cutoff`` with no live parent thread.

        A row is orphaned when it was never claimed (``thread_id IS NULL``) or
        when its thread row is gone (an interrupted delete). Callers pass the
        handles still referenced by a live turn in ``skip_handles`` so a sweep
        can never race a turn that is about to claim an upload.

        This is a read-only view for diagnostics; :meth:`delete_orphan_artifacts`
        is the sweep's writer, because only a delete under the same predicate
        is atomic with respect to a concurrent claim.
        """

        where, params = self._orphan_filter("a", cutoff, skip_handles)
        with self._lock:
            rows = self._conn.execute(
                "SELECT a.* FROM artifacts a WHERE " + where, params
            ).fetchall()
        return [self._artifact_record(row) for row in rows]

    def delete_orphan_artifacts(
        self,
        cutoff: str,
        *,
        skip_handles: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        """Atomically delete parentless artifact rows older than ``cutoff``.

        Selection and deletion share one transaction, and the ``DELETE``
        re-evaluates the parentless predicate itself, so a row claimed by a
        thread between the two statements (another process on the same file)
        survives and is not reported. The returned records are exactly the
        rows that were removed — never "the rows we meant to remove" — which
        keeps caller counts and filesystem cleanup honest.
        """

        where, params = self._orphan_filter("artifacts", cutoff, skip_handles)
        with self.transaction():
            rows = self._conn.execute(
                "SELECT * FROM artifacts WHERE " + where, params
            ).fetchall()
            records = [self._artifact_record(row) for row in rows]
            handles = [record["handle"] for record in records]
            if not handles:
                return []
            gone: set[str] = set()
            # SQLite's bound-variable limit is 999 on older builds.
            for start in range(0, len(handles), 500):
                chunk = handles[start : start + 500]
                placeholders = ", ".join("?" for _ in chunk)
                self._conn.execute(
                    f"DELETE FROM artifacts WHERE handle IN ({placeholders}) "
                    "AND (thread_id IS NULL OR NOT EXISTS "
                    "(SELECT 1 FROM threads t WHERE t.id = artifacts.thread_id "
                    "AND t.deleted_at IS NULL))",
                    chunk,
                )
                survivors = self._conn.execute(
                    f"SELECT handle FROM artifacts WHERE handle IN ({placeholders})",
                    chunk,
                ).fetchall()
                gone.update(
                    handle
                    for handle in chunk
                    if handle not in {row["handle"] for row in survivors}
                )
            deleted = [record for record in records if record["handle"] in gone]
        return deleted

    def delete_artifacts(self, handles: Sequence[str]) -> int:
        """Hard-delete artifact registry rows by handle. Idempotent."""

        unique = tuple(dict.fromkeys(str(handle) for handle in handles))
        if not unique:
            return 0
        deleted = 0
        with self.transaction():
            # SQLite's bound-variable limit is 999 on older builds.
            for start in range(0, len(unique), 500):
                chunk = unique[start : start + 500]
                placeholders = ", ".join("?" for _ in chunk)
                deleted += self._conn.execute(
                    f"DELETE FROM artifacts WHERE handle IN ({placeholders})",
                    chunk,
                ).rowcount
        return deleted

    def artifact_handles(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute("SELECT handle FROM artifacts").fetchall()
        return {row["handle"] for row in rows}

    def artifact_storage_paths(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT storage_path FROM artifacts"
            ).fetchall()
        return {row["storage_path"] for row in rows}

    def referenced_artifact_handles(self) -> set[str]:
        """Handles mentioned in any message block, live turn or not.

        Used by the orphan sweep: an upload that is still referenced must not
        be deleted out from under a turn, even when its registry row looks
        unbound.
        """

        with self._lock:
            rows = self._conn.execute("SELECT payload_json FROM blocks").fetchall()
        handles: set[str] = set()
        for row in rows:
            handles.update(_ARTIFACT_HANDLE_RE.findall(row["payload_json"] or ""))
        return handles

    # -- recovery ----------------------------------------------------------

    def snapshot(self, thread_id: str, *, limit: int = 50, before: str | None = None
                 ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], int, bool]:
        thread = self.get_thread(thread_id)
        if thread is None:
            return None, [], 0, False
        messages, has_more = self.list_messages(thread_id, limit=limit, before=before)
        return thread, messages, self.last_seq(thread_id), has_more


def _encode_cursor(updated_at: str, thread_id: str) -> str:
    raw = json.dumps([updated_at, thread_id], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[str, str] | None:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        value = json.loads(raw)
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(value, list) or len(value) != 2:
        return None
    return str(value[0]), str(value[1])

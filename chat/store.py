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
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

__all__ = ["ChatStore", "iso_now", "new_id"]

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
"""


def iso_now(moment: datetime | None = None) -> str:
    """ISO-8601 UTC with a ``Z`` suffix, matching the chat wire format."""

    value = moment or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _dump(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))


def _load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:  # pragma: no cover - internal invariant
        return default


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
        card = dict(card)
        with self.transaction():
            self._conn.execute(
                "INSERT INTO approvals (id, thread_id, turn_id, call_id, source, "
                "status, payload_json, requested_at, expires_at, decided_at, "
                "consumed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET status = excluded.status, "
                "payload_json = excluded.payload_json, "
                "decided_at = excluded.decided_at, consumed_at = excluded.consumed_at",
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
    ) -> dict[str, Any] | None:
        card = self.get_approval(approval_id)
        if card is None:
            return None
        card["status"] = status
        if decided_at is not None:
            card["decided_at"] = decided_at
        if consumed_at is not None:
            card["consumed_at"] = consumed_at
        return self.save_approval(card)

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

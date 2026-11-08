"""P2-1: tenant columns, tenant-scoped reads, and old-row backfill.

The point of these tests is not that a column exists. A cross-tenant call must
return the same empty/deny answer it would get for an unknown id, and an
existing database must come forward without losing a row. Where a boundary is
missing, a leak shows up here as a populated result rather than a schema diff.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from chat.store import ChatStore, ThreadGoneError
from chat.tenancy import (
    OWNER_TENANT_ID,
    Principal,
    StaticTenantResolver,
    TENANT_MISMATCH,
    TenantContext,
    TenantIdError,
    TenantMismatchError,
    ensure_tenant,
    is_valid_tenant_id,
    validate_tenant_id,
)
from kernel.usage import UsageRecord
from openshell.audit import AuditLog


def make_store(tmp_path: Path) -> ChatStore:
    return ChatStore(
        tmp_path / "chat.db",
        now=lambda: datetime(2026, 11, 9, 9, 0, 0, tzinfo=timezone.utc),
    )


# -- the contract's identity shapes ---------------------------------------


def test_tenant_context_field_order_and_defaults() -> None:
    assert [field.name for field in dataclasses.fields(TenantContext)] == [
        "tenant_id",
        "actor_id",
        "role",
        "surface",
    ]
    context = TenantContext()
    assert context == TenantContext(
        tenant_id="owner",
        actor_id="owner",
        role="owner",
        surface="web",
    )
    assert Principal is TenantContext
    assert OWNER_TENANT_ID == "owner"


def test_slug_validation_accepts_a_dns_label_and_rejects_everything_else() -> None:
    valid = [
        "owner",
        "a",
        "acme",
        "acme-2",
        "a--b",
        "x" * 63,
    ]
    for value in valid:
        assert is_valid_tenant_id(value) is True, value
        assert validate_tenant_id(value) == value

    invalid = [
        "",
        "Owner",
        "owner_1",
        "-owner",
        "owner-",
        "a b",
        "owner/1",
        "ünicode",
        "x" * 64,
        None,
        123,
    ]
    for value in invalid:
        assert is_valid_tenant_id(value) is False, value
        with pytest.raises(TenantIdError):
            validate_tenant_id(value)


def test_context_and_store_writes_reject_a_bad_tenant_id(tmp_path: Path) -> None:
    with pytest.raises(TenantIdError):
        TenantContext(tenant_id="Owner")
    store = make_store(tmp_path)
    try:
        with pytest.raises(TenantIdError):
            store.create_thread(tenant_id="../other")
    finally:
        store.close()


def test_static_resolver_maps_only_the_owner_token_to_owner() -> None:
    resolver = StaticTenantResolver("root-token")
    assert resolver.resolve("root-token") == TenantContext()

    with pytest.raises(TenantMismatchError) as excinfo:
        resolver.resolve("another-token")
    assert excinfo.value.code == TENANT_MISMATCH
    assert excinfo.value.status == 403
    assert "another-token" not in str(excinfo.value)

    # Auth disabled: there is no credential to compare, so the caller is the
    # single owner. This is the existing deployment's shape.
    assert StaticTenantResolver().resolve().tenant_id == "owner"
    # The token never renders, even through repr.
    assert "root-token" not in repr(resolver)


def test_ensure_tenant_is_a_typed_mismatch() -> None:
    assert ensure_tenant("owner") == "owner"
    with pytest.raises(TenantMismatchError):
        ensure_tenant("acme")


# -- thread roots ----------------------------------------------------------


def test_cross_tenant_thread_get_list_delete_and_rename_fail(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    try:
        store.create_thread(thread_id="ses_acme", tenant_id="acme", title="acme")

        # Unknown from the owner's side, exactly like a missing id.
        assert store.get_thread("ses_acme") is None
        assert store.thread_exists("ses_acme") is False
        assert store.list_threads(tenant_id="owner")[0] == []

        # The owning tenant still sees it.
        assert store.get_thread("ses_acme", tenant_id="acme")["title"] == "acme"
        assert [row["id"] for row in store.list_threads(tenant_id="acme")[0]] == [
            "ses_acme"
        ]

        # Cross-tenant mutation is a no-op, not a silent write.
        assert store.rename_thread("ses_acme", "hijacked") is None
        store.touch_thread("ses_acme", title="hijacked")
        assert store.set_auto_title("ses_acme", "hijacked") is False
        assert store.soft_delete_thread("ses_acme") is False
        payload = store.get_thread("ses_acme", tenant_id="acme")
        assert payload["title"] == "acme"
        assert store.get_thread("ses_acme", tenant_id="acme") is not None

        # The owning tenant can still do all of it.
        assert store.soft_delete_thread("ses_acme", tenant_id="acme") is True
        assert store.get_thread("ses_acme", tenant_id="acme") is None
    finally:
        store.close()


def test_cross_tenant_purge_deletes_nothing(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    try:
        thread = store.create_thread(thread_id="ses_acme", tenant_id="acme")
        store.create_message(
            thread_id=thread["id"],
            turn_id="turn_acme",
            role="user",
            status="complete",
            blocks=[{"type": "text", "text": "secret"}],
        )

        report = store.purge_thread("ses_acme")
        assert report["deleted"] is False
        assert report["messages"] == 0
        assert store.get_thread("ses_acme", tenant_id="acme") is not None

        report = store.purge_thread("ses_acme", tenant_id="acme")
        assert report["deleted"] is True
        assert report["messages"] == 1
        assert store.get_thread("ses_acme", tenant_id="acme") is None
    finally:
        store.close()


def test_client_turn_lookup_is_scoped_by_the_parent_thread_tenant(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    try:
        thread = store.create_thread(thread_id="ses_acme", tenant_id="acme")
        turn = store.create_turn(
            thread_id=thread["id"],
            user_message_id="msg_u",
            assistant_message_id="msg_a",
            client_turn_id="ct-acme",
            budget={},
        )
        assert store.find_turn_by_client_id("ses_acme", "ct-acme") is None
        found = store.find_turn_by_client_id(
            "ses_acme", "ct-acme", tenant_id="acme"
        )
        assert found is not None
        assert found["id"] == turn["id"]
    finally:
        store.close()


# -- shares ----------------------------------------------------------------


def test_cross_tenant_share_create_list_get_and_revoke_fail(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    try:
        store.create_thread(thread_id="ses_acme", tenant_id="acme")
        token_hash = "a" * 64
        record = store.share_create(
            token_hash=token_hash,
            thread_id="ses_acme",
            snapshot={"messages": []},
            ttl_seconds=60,
            tenant_id="acme",
        )
        assert record["tenant_id"] == "acme"

        assert store.share_list("ses_acme") == []
        assert store.share_list("ses_acme", tenant_id="acme")[0][
            "token_hash"
        ] == token_hash
        assert store.share_get(token_hash) is None
        assert store.share_get(token_hash, tenant_id="acme") is not None
        assert store.share_get_any(token_hash) is None
        assert store.share_revoke(token_hash) is False
        assert store.share_get_any(token_hash, tenant_id="acme")["revoked_at"] is None
        assert store.share_revoke(token_hash, tenant_id="acme") is True

        with pytest.raises(ThreadGoneError):
            store.share_create(
                token_hash="b" * 64,
                thread_id="ses_acme",
                snapshot={},
                ttl_seconds=60,
            )
    finally:
        store.close()


# -- artifacts -------------------------------------------------------------


def test_cross_tenant_artifact_get_bind_and_delete_fail(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    try:
        store.create_thread(thread_id="ses_acme", tenant_id="acme")
        store.create_thread(thread_id="ses_owner")
        meta = store.create_artifact(
            {
                "handle": "artifact:acmefile01",
                "name": "acme.pdf",
                "kind": "pdf",
                "mime": "application/pdf",
                "size_bytes": 10,
                "sha256": "abc",
                "storage_path": "acme.pdf",
            },
            tenant_id="acme",
        )
        assert meta["tenant_id"] == "acme"

        assert store.get_artifact("artifact:acmefile01") is None
        assert store.get_artifact("artifact:acmefile01", tenant_id="acme") is not None
        assert store.bind_artifact("artifact:acmefile01", "ses_owner") is False
        assert store.bind_artifact("artifact:acmefile01", "ses_acme") is False
        assert (
            store.bind_artifact(
                "artifact:acmefile01", "ses_acme", tenant_id="acme"
            )
            is True
        )
        store.delete_artifacts_for_thread("ses_acme")
        assert store.get_artifact("artifact:acmefile01", tenant_id="acme") is not None
        store.delete_artifacts_for_thread("ses_acme", tenant_id="acme")
        assert store.get_artifact("artifact:acmefile01", tenant_id="acme") is None

        with pytest.raises(TenantMismatchError):
            store.create_artifact(
                {
                    "handle": "artifact:wrong01",
                    "thread_id": "ses_acme",
                    "name": "wrong.pdf",
                    "kind": "pdf",
                    "mime": "application/pdf",
                    "size_bytes": 1,
                    "sha256": "def",
                    "storage_path": "wrong.pdf",
                }
            )
    finally:
        store.close()


# -- approvals -------------------------------------------------------------


def test_cross_tenant_approval_get_list_and_transition_fail(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    try:
        store.create_thread(thread_id="ses_acme", tenant_id="acme")
        card = {
            "id": "apr_acme",
            "thread_id": "ses_acme",
            "turn_id": None,
            "call_id": None,
            "source": "skill",
            "status": "pending",
            "requested_at": "2026-11-09T09:00:00Z",
            "expires_at": "2099-01-01T00:00:00Z",
        }
        store.save_approval(card, tenant_id="acme")

        assert store.get_approval("apr_acme") is None
        assert store.list_approvals() == []
        assert store.list_approvals(thread_id="ses_acme") == []
        assert (
            store.transition_approval("apr_acme", "approved", allowed_from=("pending",))
            is None
        )
        assert store.set_approval_status("apr_acme", "approved") is None

        assert store.get_approval("apr_acme", tenant_id="acme")["status"] == "pending"
        assert [row["id"] for row in store.list_approvals(tenant_id="acme")] == [
            "apr_acme"
        ]
        approved = store.transition_approval(
            "apr_acme", "approved", tenant_id="acme"
        )
        assert approved is not None and approved["status"] == "approved"

        with pytest.raises(TenantMismatchError):
            store.save_approval({**card, "id": "apr_wrong"})
    finally:
        store.close()


# -- old databases ---------------------------------------------------------


_LEGACY_SCHEMA = """
CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    title_source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE TABLE artifacts (
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
CREATE TABLE shares (
    token_hash TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL DEFAULT 'owner',
    thread_id TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    snapshot_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE TABLE sessions (
    id_hash TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_seen REAL NOT NULL,
    expires_at REAL NOT NULL,
    revoked_at REAL
);
"""


def _legacy_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(_LEGACY_SCHEMA)
    connection.execute(
        "INSERT INTO threads VALUES "
        "('ses_legacy', 'old chat', 'user', '2026-01-01T00:00:00Z', "
        "'2026-01-01T00:00:00Z', NULL)"
    )
    connection.execute(
        "INSERT INTO artifacts VALUES "
        "('artifact:legacy01', 'ses_legacy', 'old.pdf', 'pdf', 'application/pdf', "
        "1, 'sha', 'old.pdf', '2026-01-01T00:00:00Z', NULL, NULL, NULL)"
    )
    connection.execute(
        "INSERT INTO shares VALUES "
        "('c' || substr(hex(randomblob(64)), 1, 63), 'owner', 'ses_legacy', '{}', 2, "
        "'2026-01-01T00:00:00Z', '2099-01-01T00:00:00Z', NULL)"
    )
    connection.execute(
        "INSERT INTO sessions VALUES "
        "('legacyhash', 'Old laptop', 1.0, 1.0, 2099.0, NULL)"
    )
    connection.commit()
    connection.close()


def test_existing_rows_are_backfilled_to_owner_and_migration_is_idempotent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chat.db"
    _legacy_database(path)

    store = ChatStore(path)
    try:
        for table in ("threads", "artifacts", "shares", "sessions"):
            columns = {
                row["name"]
                for row in store._conn.execute(f"PRAGMA table_info({table})")
            }
            assert "tenant_id" in columns, table

        thread = store.get_thread("ses_legacy")
        assert thread is not None and thread["tenant_id"] == "owner"

        artifact = store.get_artifact("artifact:legacy01")
        assert artifact is not None and artifact["tenant_id"] == "owner"

        share_rows = store.share_list("ses_legacy")
        assert len(share_rows) == 1 and share_rows[0]["tenant_id"] == "owner"

        session = store.session_get("legacyhash", now=10.0)
        assert session is not None and session["tenant_id"] == "owner"

        owner_rows = store._conn.execute("SELECT id FROM tenants").fetchall()
        assert [row["id"] for row in owner_rows] == ["owner"]
    finally:
        store.close()

    # Opening the migrated file again must be a no-op, not a duplicate ALTER.
    reopened = ChatStore(path)
    try:
        assert reopened.get_thread("ses_legacy")["tenant_id"] == "owner"
        assert reopened.get_artifact("artifact:legacy01")["tenant_id"] == "owner"
    finally:
        reopened.close()


# -- usage and audit records ----------------------------------------------


def test_usage_and_audit_records_carry_tenant_id_by_default(tmp_path: Path) -> None:
    record = UsageRecord(
        turn_id="turn_1",
        session_id=None,
        model="m",
        input_tokens=1,
        output_tokens=2,
        total_tokens=3,
        estimated_cost_usd=0.01,
        cost_known=True,
        usage_reported=True,
        steps=1,
        tool_calls=0,
        started_at="2026-11-09T09:00:00Z",
        finished_at="2026-11-09T09:00:01Z",
        duration_ms=1000,
        outcome="stop",
    )
    assert record.to_dict()["tenant_id"] == "owner"

    audit = AuditLog(tmp_path / "audit.jsonl")
    entry = audit.record("tenant_check")
    assert entry["tenant_id"] == "owner"
    assert audit.entries(event="tenant_check")[0]["tenant_id"] == "owner"

"""P2-1: tenant columns, tenant-scoped reads, and old-row backfill.

The point of these tests is not that a column exists. A cross-tenant call must
return the same empty/deny answer it would get for an unknown id, and an
existing database must come forward without losing a row. Where a boundary is
missing, a leak shows up here as a populated result rather than a schema diff.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

from chat import ChatAPI, ChatService, ChatStore, Request
from chat.api import SessionStore
from chat.errors import ChatError
from chat.artifacts import ArtifactError
from chat.store import ThreadGoneError
from chat.tenancy import (
    OWNER_TENANT_ID,
    Principal,
    SHARE_TOKEN_MODEL,
    StaticTenantResolver,
    TENANT_MISMATCH,
    TenantContext,
    TenantIdError,
    TenantMismatchError,
    bind_tenant,
    current_tenant,
    ensure_tenant,
    is_valid_tenant_id,
    share_read_tenant,
    tenant_for,
    validate_tenant_id,
)
from kernel.usage import UsageRecord
from openshell.audit import AuditLog

from .chatkit import MODEL, StubProvider, text_script


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


# -- P2-2: the tenant travels with the service call ------------------------


def _acme_thread(store: ChatStore, thread_id: str = "ses_acme") -> dict[str, Any]:
    return store.create_thread(
        thread_id=thread_id, tenant_id="acme", title="acme"
    )


def _acme_turn(store: ChatStore, thread_id: str = "ses_acme") -> dict[str, Any]:
    return store.create_turn(
        thread_id=thread_id,
        user_message_id="msg_acme_user",
        assistant_message_id="msg_acme_assistant",
        client_turn_id="ct-acme",
        budget={},
    )


def _pending_approval(
    store: ChatStore,
    *,
    approval_id: str = "apr_acme",
    thread_id: str = "ses_acme",
    tenant_id: str = "acme",
) -> dict[str, Any]:
    card = {
        "id": approval_id,
        "thread_id": thread_id,
        "turn_id": None,
        "call_id": None,
        "source": "skill",
        "status": "pending",
        "tool": "shell.exec",
        "requested_at": "2026-11-09T09:00:00Z",
        "expires_at": "2099-01-01T00:00:00Z",
    }
    store.save_approval(card, tenant_id=tenant_id)
    return card


def test_bound_tenant_defaults_to_owner_and_is_reset() -> None:
    assert current_tenant() == TenantContext()
    assert tenant_for(None) == TenantContext()
    acme = TenantContext(tenant_id="acme")
    with bind_tenant(acme):
        assert current_tenant() == acme
        assert tenant_for(None) == acme
    assert current_tenant() == TenantContext()

    assert tenant_for("owner").tenant_id == "owner"
    assert SHARE_TOKEN_MODEL == "tenant_scoped_token"
    assert share_read_tenant("owner") == "owner"
    with pytest.raises(TenantMismatchError):
        share_read_tenant("acme")


def test_service_denies_foreign_thread_turn_and_approval_ids(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    service = ChatService(store=store)
    try:
        _acme_thread(store)
        _acme_turn(store)
        _pending_approval(store)

        with pytest.raises(ChatError) as thread_error:
            service.get_thread("ses_acme")
        assert thread_error.value.code == "unknown_thread"
        assert service.list_threads() == ([], None)
        with pytest.raises(ChatError):
            service.rename_thread("ses_acme", "hijacked")

        with pytest.raises(ChatError) as stop_error:
            asyncio.run(service.stop("turn_acme"))
        assert stop_error.value.code == "unknown_turn"
        with pytest.raises(ChatError) as retry_error:
            asyncio.run(service.retry("turn_acme"))
        assert retry_error.value.code == "unknown_turn"

        assert service.list_approvals() == []
        with pytest.raises(ChatError) as approval_error:
            service.get_approval("apr_acme")
        assert approval_error.value.code == "unknown_approval"
        with pytest.raises(ChatError):
            service.decide("apr_acme", "approve")

        # The same calls with the owning context still work, so the denial is
        # scoping rather than a missing feature.
        acme = TenantContext(tenant_id="acme")
        assert service.get_thread("ses_acme", tenant=acme)["thread"]["id"] == "ses_acme"
        assert [row["id"] for row in service.list_threads(tenant=acme)[0]] == [
            "ses_acme"
        ]
        assert service.get_approval("apr_acme", tenant=acme)["status"] == "pending"
        decided = service.decide("apr_acme", "approve", tenant=acme)
        assert decided["approval"]["status"] == "approved"
        assert store.get_approval("apr_acme", tenant_id="acme")["status"] == "approved"
    finally:
        store.close()


def test_service_send_refuses_a_foreign_thread_before_creating_rows(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    service = ChatService(store=store)
    try:
        _acme_thread(store)
        with pytest.raises(ChatError) as excinfo:
            asyncio.run(
                service.send(
                    "ses_acme",
                    text="secret",
                    client_turn_id="ct-owner",
                )
            )
        assert excinfo.value.code == "unknown_thread"
        assert store.list_messages("ses_acme") == ([], False)
        assert store.latest_turn("ses_acme") is None
    finally:
        store.close()


def test_exec_approval_is_created_under_the_thread_tenant(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    service = ChatService(store=store)
    try:
        _acme_thread(store)
        card = service.open_exec_approval(
            thread_id="ses_acme",
            turn_id=None,
            call_id=None,
            tool="shell.exec",
            command=["true"],
            tenant=TenantContext(tenant_id="acme"),
        )
        assert store.get_approval(card["id"], tenant_id="acme") is not None
        assert store.get_approval(card["id"], tenant_id="owner") is None

        with pytest.raises(ChatError):
            service.open_exec_approval(
                thread_id="ses_acme",
                turn_id=None,
                call_id=None,
                tool="shell.exec",
                command=["true"],
            )
    finally:
        store.close()


def test_artifact_reads_are_scoped_to_the_bound_tenant(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    service = ChatService(store=store)
    try:
        meta = service.artifacts.save_upload("rows.csv", b"a,b\n1,2\n")
        assert service.artifacts.card(meta["handle"])["handle"] == meta["handle"]

        acme = TenantContext(tenant_id="acme")
        with pytest.raises(ArtifactError):
            service.artifacts.card(meta["handle"], tenant=acme)
        # A non-owner tenant can upload, but the bytes and row are its own.
        own = service.artifacts.save_upload(
            "rows.csv", b"a,b\n1,2\n", tenant=acme
        )
        assert own["tenant_id"] == "acme"
        assert service.artifacts.card(own["handle"], tenant=acme)["handle"] == own["handle"]
        with pytest.raises(ArtifactError):
            service.artifacts.card(own["handle"])

        _acme_thread(store)
        store.create_artifact(
            {
                "handle": "artifact:acmefile01",
                "name": "acme.pdf",
                "kind": "pdf",
                "mime": "application/pdf",
                "size_bytes": 1,
                "sha256": "abc",
                "storage_path": "acme.pdf",
            },
            tenant_id="acme",
        )
        with pytest.raises(ArtifactError):
            service.artifacts.card("artifact:acmefile01")
    finally:
        store.close()


# -- P2-3: per-tenant filesystem roots ------------------------------------


def _tenant_service(
    tmp_path: Path,
) -> tuple[ChatService, ChatStore, Path]:
    store = make_store(tmp_path)
    workspace = tmp_path / "workspace"
    service = ChatService(store=store, workspace_root=str(workspace))
    return service, store, workspace


def test_owner_artifacts_stay_at_the_legacy_root(tmp_path: Path) -> None:
    service, store, workspace = _tenant_service(tmp_path)
    try:
        meta = service.artifacts.save_upload("rows.csv", b"a,b\n1,2\n")
        assert meta["tenant_id"] == OWNER_TENANT_ID
        # Legacy shape: a bare filename directly inside the configured root.
        assert "/" not in meta["storage_path"]
        legacy = service.artifacts.root / meta["storage_path"]
        assert legacy.is_file()
        assert legacy.parent == service.artifacts.root
        assert not (workspace / "tenants").exists()
    finally:
        store.close()


def test_non_owner_artifacts_live_under_the_tenant_root_and_stay_hidden(
    tmp_path: Path,
) -> None:
    service, store, workspace = _tenant_service(tmp_path)
    try:
        acme = TenantContext(tenant_id="acme")
        meta = service.artifacts.save_upload(
            "rows.csv", b"a,b\n1,2\n", tenant=acme
        )
        assert meta["tenant_id"] == "acme"
        filename = meta["storage_path"].rsplit("/", 1)[-1]
        assert meta["storage_path"] == f"tenants/acme/{filename}"
        tenant_file = (
            workspace / "tenants" / "acme" / "var" / "artifacts" / filename
        )
        assert tenant_file.is_file()
        assert not (service.artifacts.root / filename).exists()

        assert (
            service.artifacts.card(meta["handle"], tenant=acme)["handle"]
            == meta["handle"]
        )
        assert (
            service.artifacts.content(meta["handle"], tenant=acme).status == 200
        )
        with pytest.raises(ArtifactError):
            service.artifacts.card(meta["handle"])
        with pytest.raises(ArtifactError):
            service.artifacts.card(
                meta["handle"], tenant=TenantContext(tenant_id="globex")
            )
    finally:
        store.close()


def test_artifact_tenant_paths_reject_evil_slugs_and_traversal(
    tmp_path: Path,
) -> None:
    service, store, workspace = _tenant_service(tmp_path)
    try:
        for evil in ("../acme", "acme/../owner", "Owner", "acme_1", "..", ""):
            with pytest.raises(TenantIdError):
                service.artifacts.save_upload(
                    "rows.csv", b"a,b\n1,2\n", tenant=evil
                )

        acme = TenantContext(tenant_id="acme")
        forged_paths = (
            "tenants/../escape.csv",
            "tenants/acme/../../escape.csv",
            "tenants/acme/nested/data.csv",
            "acme.pdf",  # an owner-shaped path on a tenant row
        )
        for index, storage_path in enumerate(forged_paths):
            handle = f"artifact:forged{index:04d}"
            store.create_artifact(
                {
                    "handle": handle,
                    "name": "forged.csv",
                    "kind": "csv",
                    "mime": "text/csv; charset=utf-8",
                    "size_bytes": 1,
                    "sha256": "abc",
                    "storage_path": storage_path,
                },
                tenant_id="acme",
            )
            with pytest.raises(ArtifactError):
                service.artifacts.get(handle, tenant=acme)

        # Nothing escaped into the workspace or the owner root.
        assert not (workspace / "escape.csv").exists()
        assert not (workspace / "acme.pdf").exists()
    finally:
        store.close()


def test_schedule_roots_are_per_tenant_while_owner_stays_legacy(
    tmp_path: Path,
) -> None:
    from chat.scheduler import ScheduleStore

    workspace = tmp_path / "workspace"
    owner = ScheduleStore(workspace)
    assert owner.root == workspace / "var" / "scheduler"

    acme = ScheduleStore(workspace, tenant_id="acme")
    assert acme.root == workspace / "tenants" / "acme" / "var" / "scheduler"
    task = acme.create_task(
        title="tenant task",
        prompt="p",
        expr="* * * * *",
        timezone="UTC",
    )
    assert (acme.root / f"{task['task_id']}.json").is_file()
    assert not owner.root.exists()

    with pytest.raises(TenantIdError):
        ScheduleStore(workspace, tenant_id="../acme")


# -- P2-2: API gate --------------------------------------------------------


AUTH = {"authorization": "Bearer tok"}
CSRF = {"x-csrf-token": "csrf"}


def make_api(tmp_path: Path, **service_kwargs: Any) -> tuple[ChatAPI, ChatService, ChatStore]:
    store = ChatStore(tmp_path / "chat.db")
    service = ChatService(
        store=store,
        provider=StubProvider([text_script("Hello")]),
        model=MODEL,
        **service_kwargs,
    )
    api = ChatAPI(service, auth_token="tok", csrf_token="csrf")
    return api, service, store


def api_call(
    api: ChatAPI,
    method: str,
    path: str,
    body: Mapping[str, Any] | None = None,
    *,
    auth: bool = True,
    csrf: bool = True,
) -> Any:
    headers: dict[str, str] = {}
    if auth:
        headers.update(AUTH)
    if csrf and method.upper() in {"POST", "PATCH", "DELETE"}:
        headers.update(CSRF)
    if body is not None:
        headers["content-type"] = "application/json"
    raw = json.dumps(body).encode() if body is not None else b""
    return api.handle(Request(method, path, {}, headers, raw))


def response_body(response: Any) -> dict[str, Any]:
    return json.loads(response.body)


def test_body_tenant_that_disagrees_is_typed_before_any_run(
    tmp_path: Path,
) -> None:
    api, _service, store = make_api(tmp_path)
    try:
        denied = api_call(
            api,
            "POST",
            "/api/threads",
            {"title": "acme", "tenant_id": "acme"},
        )
        assert denied.status == 403
        assert response_body(denied)["error"]["code"] == TENANT_MISMATCH
        assert store.list_threads(tenant_id="owner")[0] == []

        created = api_call(api, "POST", "/api/threads", {"title": "ok"})
        assert created.status == 201
        thread = response_body(created)["thread"]

        rejected_send = api_call(
            api,
            "POST",
            f"/api/threads/{thread['id']}/turns",
            {
                "client_turn_id": "ct-tenant-1",
                "text": "hello",
                "tenant": "acme",
            },
        )
        assert rejected_send.status == 403
        assert response_body(rejected_send)["error"]["code"] == TENANT_MISMATCH
        assert store.list_messages(thread["id"]) == ([], False)
        assert store.latest_turn(thread["id"]) is None

        own_tenant = api_call(
            api,
            "POST",
            "/api/threads",
            {"title": "owner", "tenant_id": "owner"},
        )
        assert own_tenant.status == 201
    finally:
        api.close()
        store.close()


def test_public_share_token_is_scoped_to_the_tenant_that_minted_it(
    tmp_path: Path,
) -> None:
    import hashlib

    api, _service, store = make_api(tmp_path)
    try:
        created = api_call(api, "POST", "/api/threads", {"title": "shared"})
        thread = response_body(created)["thread"]
        share = api_call(api, "POST", f"/api/threads/{thread['id']}/share", {})
        assert share.status == 201
        token = response_body(share)["share"]["token"]

        public = api_call(api, "GET", f"/api/shared/{token}", auth=False)
        assert public.status == 200
        assert response_body(public)["share"]["title"] == "shared"

        # A row owned by another tenant is not a live capability for this
        # deployment: the token carries its tenant and the lookup is scoped.
        _acme_thread(store, thread_id="ses_acme_share")
        acme_token = "acme-share-token"
        acme_hash = hashlib.sha256(acme_token.encode()).hexdigest()
        store.share_create(
            token_hash=acme_hash,
            thread_id="ses_acme_share",
            snapshot={"title": "acme", "messages": []},
            ttl_seconds=3600,
            tenant_id="acme",
        )
        foreign = api_call(api, "GET", f"/api/shared/{acme_token}", auth=False)
        assert foreign.status == 404
        assert store.share_get(acme_hash) is None
        assert store.share_get(acme_hash, tenant_id="acme") is not None
    finally:
        api.close()
        store.close()


def test_audit_rows_carry_the_acting_tenant(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    audit = AuditLog(tmp_path / "audit.jsonl")
    service = ChatService(store=store, audit=audit)
    try:
        _acme_thread(store)
        _pending_approval(store)
        service.decide(
            "apr_acme", "approve", tenant=TenantContext(tenant_id="acme")
        )

        store.create_thread(thread_id="ses_owner", title="owner")
        _pending_approval(
            store,
            approval_id="apr_owner",
            thread_id="ses_owner",
            tenant_id="owner",
        )
        service.decide("apr_owner", "approve")

        rows = audit.entries(event="approval_resolved")
        assert [row["tenant_id"] for row in rows] == ["acme", "owner"]
        assert all(row["actor"] == "owner" for row in rows)
    finally:
        store.close()


def test_cookie_session_carries_the_owner_tenant(tmp_path: Path) -> None:
    store = ChatStore(tmp_path / "chat.db")
    service = ChatService(
        store=store,
        provider=StubProvider([text_script("Hello")]),
        model=MODEL,
    )
    sessions = SessionStore(store=store)
    api = ChatAPI(
        service,
        auth_token="tok",
        csrf_token="csrf",
        sessions=sessions,
    )
    try:
        sign_in = api.handle(
            Request(
                "POST",
                "/api/auth/session",
                {},
                {"authorization": "Bearer tok"},
                b"",
            )
        )
        assert sign_in.status == 201
        cookie = next(
            value.split(";", 1)[0]
            for value in sign_in.cookies
            if value.startswith("session=")
        )
        listed = api.handle(
            Request("GET", "/api/threads", {}, {"cookie": cookie}, b"")
        )
        assert listed.status == 200

        # The row was minted with a tenant even though the cookie lookup runs
        # before any request tenant is known.
        rows = store.session_list(now=sessions.now(), tenant_id="owner")
        assert rows and rows[0]["tenant_id"] == OWNER_TENANT_ID
        assert store.session_list(now=sessions.now(), tenant_id="acme") == []
    finally:
        api.close()
        store.close()

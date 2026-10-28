"""Approval store tests: 10-minute TTL, single use, replay and denial (KEL-7)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from openshell import (
    APPROVAL_TTL_SECONDS,
    ApprovalAlreadyUsed,
    ApprovalDenied,
    ApprovalExpired,
    ApprovalNotGranted,
    ApprovalNotFound,
    ApprovalStore,
)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def make_store(clock: Clock) -> ApprovalStore:
    return ApprovalStore(now_fn=clock)


def create(store: ApprovalStore):
    return store.create(
        tool="shell.exec",
        command=("echo", "hello"),
        effects=("execute",),
        blast_radius=("exec:echo",),
        actor="owner",
        metadata={"cwd": "/workspace"},
    )


def test_default_ttl_is_ten_minutes() -> None:
    clock = Clock()
    store = make_store(clock)
    request = create(store)

    assert APPROVAL_TTL_SECONDS == 600
    assert store.ttl_seconds == 600
    assert request.single_use is True
    assert request.status == "pending"
    requested = datetime.fromisoformat(request.requested_at)
    expires = datetime.fromisoformat(request.expires_at)
    assert expires - requested == timedelta(seconds=600)
    assert request.argument_hash.startswith("sha256:")
    assert request.metadata == {"cwd": "/workspace"}


def test_approve_then_consume_once() -> None:
    clock = Clock()
    store = make_store(clock)
    request = create(store)

    approved = store.approve(request.id, actor="owner")
    assert approved.status == "approved"
    assert approved.decided_at is not None

    consumed = store.consume(request.id)
    assert consumed.status == "consumed"
    assert consumed.consumed_at is not None

    with pytest.raises(ApprovalAlreadyUsed) as info:
        store.consume(request.id)
    assert info.value.code == "already_used"
    with pytest.raises(ApprovalAlreadyUsed):
        store.approve(request.id)


def test_denied_request_cannot_run() -> None:
    clock = Clock()
    store = make_store(clock)
    request = create(store)

    denied = store.deny(request.id, actor="owner")
    assert denied.status == "denied"

    with pytest.raises(ApprovalDenied):
        store.consume(request.id)
    with pytest.raises(ApprovalDenied):
        store.approve(request.id)


def test_approved_request_cannot_be_denied() -> None:
    clock = Clock()
    store = make_store(clock)
    request = create(store)
    store.approve(request.id)

    with pytest.raises(ApprovalNotGranted):
        store.deny(request.id)


def test_pending_request_cannot_be_consumed() -> None:
    clock = Clock()
    store = make_store(clock)
    request = create(store)

    with pytest.raises(ApprovalNotGranted) as info:
        store.consume(request.id)
    assert info.value.code == "not_approved"


def test_expired_request_cannot_be_approved_or_replayed() -> None:
    clock = Clock()
    store = make_store(clock)
    request = create(store)
    store.approve(request.id)

    clock.advance(601)
    assert store.get(request.id).status == "expired"

    with pytest.raises(ApprovalExpired):
        store.consume(request.id)
    with pytest.raises(ApprovalExpired):
        store.approve(request.id)


def test_exactly_at_expiry_is_expired() -> None:
    clock = Clock()
    store = make_store(clock)
    request = create(store)
    clock.advance(600)

    assert store.get(request.id).status == "expired"


def test_unknown_request() -> None:
    store = make_store(Clock())
    with pytest.raises(ApprovalNotFound) as info:
        store.approve("apr_missing")
    assert info.value.code == "unknown_approval"


def test_pending_list_excludes_decided_requests() -> None:
    clock = Clock()
    store = make_store(clock)
    first = create(store)
    second = create(store)
    store.approve(first.id)

    pending_ids = [request.id for request in store.pending()]
    assert pending_ids == [second.id]


def test_argument_hash_is_deterministic_and_sensitive() -> None:
    clock = Clock()
    store = make_store(clock)
    first = store.create(
        tool="shell.exec",
        command=("echo", "hello"),
        effects=("execute",),
    )
    second = store.create(
        tool="shell.exec",
        command=("echo", "hello"),
        effects=("execute",),
    )
    third = store.create(
        tool="shell.exec",
        command=("echo", "bye"),
        effects=("execute",),
    )
    assert first.argument_hash == second.argument_hash
    assert first.argument_hash != third.argument_hash


def test_approval_to_dict_hides_command_on_request() -> None:
    clock = Clock()
    store = make_store(clock)
    request = create(store)
    public = request.to_dict(include_command=False)
    assert "command" not in public
    assert "metadata" not in public
    assert public["single_use"] is True

"""Single-use, 10-minute approval requests.

The store is the authority for state transitions: ``pending -> approved ->
consumed`` (or ``denied`` / ``expired``). ``consume`` is atomic, so two
concurrent callers cannot both run the same approved command.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from .errors import (
    ApprovalAlreadyUsed,
    ApprovalDenied,
    ApprovalExpired,
    ApprovalNotGranted,
    ApprovalNotFound,
)

__all__ = [
    "APPROVAL_STATUSES",
    "APPROVAL_TTL_SECONDS",
    "ApprovalRequest",
    "ApprovalStore",
]

APPROVAL_TTL_SECONDS = 600

PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"
CONSUMED = "consumed"
EXPIRED = "expired"

APPROVAL_STATUSES = frozenset({PENDING, APPROVED, DENIED, CONSUMED, EXPIRED})


@dataclass(frozen=True)
class ApprovalRequest:
    id: str
    tool: str
    command: tuple[str, ...]
    argument_hash: str
    effects: tuple[str, ...]
    blast_radius: tuple[str, ...]
    requested_at: str
    expires_at: str
    actor: str
    status: str = PENDING
    single_use: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)
    decided_at: str | None = None
    consumed_at: str | None = None

    def to_dict(self, *, include_command: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "tool": self.tool,
            "argument_hash": self.argument_hash,
            "effects": list(self.effects),
            "blast_radius": list(self.blast_radius),
            "requested_at": self.requested_at,
            "expires_at": self.expires_at,
            "actor": self.actor,
            "status": self.status,
            "single_use": self.single_use,
        }
        if include_command:
            payload["command"] = list(self.command)
            payload["metadata"] = dict(self.metadata or {})
        if self.decided_at is not None:
            payload["decided_at"] = self.decided_at
        if self.consumed_at is not None:
            payload["consumed_at"] = self.consumed_at
        return payload


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class ApprovalStore:
    def __init__(
        self,
        *,
        ttl_seconds: int = APPROVAL_TTL_SECONDS,
        now_fn: Callable[[], datetime] | None = None,
    ):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl = int(ttl_seconds)
        self._now = now_fn or _utcnow
        self._requests: dict[str, ApprovalRequest] = {}
        self._lock = threading.RLock()

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    def create(
        self,
        *,
        tool: str,
        command: Sequence[str],
        effects: Sequence[str],
        blast_radius: Sequence[str] = (),
        actor: str = "owner",
        metadata: Mapping[str, Any] | None = None,
    ) -> ApprovalRequest:
        now = _aware(self._now())
        request_id = f"apr_{uuid.uuid4().hex}"
        canonical = json.dumps(
            {
                "tool": tool,
                "command": list(command),
                "metadata": dict(metadata or {}),
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        request = ApprovalRequest(
            id=request_id,
            tool=tool,
            command=tuple(str(part) for part in command),
            argument_hash="sha256:" + hashlib.sha256(canonical).hexdigest(),
            effects=tuple(effects),
            blast_radius=tuple(blast_radius),
            requested_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=self._ttl)).isoformat(),
            actor=actor,
            metadata=dict(metadata or {}),
        )
        with self._lock:
            self._requests[request_id] = request
        return request

    def get(self, request_id: str) -> ApprovalRequest:
        with self._lock:
            return self._refresh(request_id)

    def approve(self, request_id: str, *, actor: str = "owner") -> ApprovalRequest:
        with self._lock:
            request = self._refresh(request_id)
            if request.status == EXPIRED:
                raise ApprovalExpired("Approval request has expired.", request_id=request_id)
            if request.status == DENIED:
                raise ApprovalDenied("Approval request was denied.", request_id=request_id)
            if request.status == CONSUMED:
                raise ApprovalAlreadyUsed(
                    "Approval request was already used.", request_id=request_id
                )
            if request.status == APPROVED:
                return request
            updated = replace(
                request,
                status=APPROVED,
                actor=actor,
                decided_at=self._now().isoformat(),
            )
            self._requests[request_id] = updated
            return updated

    def deny(self, request_id: str, *, actor: str = "owner") -> ApprovalRequest:
        with self._lock:
            request = self._refresh(request_id)
            if request.status == EXPIRED:
                raise ApprovalExpired("Approval request has expired.", request_id=request_id)
            if request.status == DENIED:
                return request
            if request.status in {APPROVED, CONSUMED}:
                raise ApprovalNotGranted(
                    "Approval request is already approved and cannot be denied.",
                    request_id=request_id,
                )
            updated = replace(
                request,
                status=DENIED,
                actor=actor,
                decided_at=self._now().isoformat(),
            )
            self._requests[request_id] = updated
            return updated

    def consume(self, request_id: str) -> ApprovalRequest:
        """Atomically mark an approved request used and return it."""

        with self._lock:
            request = self._refresh(request_id)
            if request.status == EXPIRED:
                raise ApprovalExpired("Approval request has expired.", request_id=request_id)
            if request.status == CONSUMED:
                raise ApprovalAlreadyUsed(
                    "Approval request was already used.", request_id=request_id
                )
            if request.status == DENIED:
                raise ApprovalDenied("Approval request was denied.", request_id=request_id)
            if request.status == PENDING:
                raise ApprovalNotGranted(
                    "Approval request has not been approved.", request_id=request_id
                )
            updated = replace(
                request,
                status=CONSUMED,
                consumed_at=self._now().isoformat(),
            )
            self._requests[request_id] = updated
            return updated

    def pending(self) -> list[ApprovalRequest]:
        with self._lock:
            results: list[ApprovalRequest] = []
            for request_id in list(self._requests):
                request = self._refresh(request_id)
                if request.status == PENDING:
                    results.append(request)
            return sorted(results, key=lambda request: request.requested_at)

    def requests(self) -> list[ApprovalRequest]:
        with self._lock:
            return [self._refresh(request_id) for request_id in list(self._requests)]

    # -- internals ---------------------------------------------------------

    def _lookup(self, request_id: str) -> ApprovalRequest:
        try:
            return self._requests[request_id]
        except KeyError:
            raise ApprovalNotFound(
                f"Unknown approval request {request_id!r}.", request_id=request_id
            ) from None

    def _refresh(self, request_id: str) -> ApprovalRequest:
        request = self._lookup(request_id)
        if request.status in {PENDING, APPROVED}:
            try:
                expires_at = datetime.fromisoformat(request.expires_at)
            except ValueError:  # pragma: no cover - internal invariant
                expires_at = self._now()
            if _aware(self._now()) >= _aware(expires_at):
                request = replace(request, status=EXPIRED)
                self._requests[request_id] = request
        return request

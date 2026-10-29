"""Boot recovery for turns killed by a process restart (KEL-11, ``CHAT_SPEC`` §10.4).

``ChatService.recover_interrupted`` closes a running turn and announces it. A
crash can also leave two other kinds of state behind:

* a tool block stuck in ``running`` or ``waiting_approval`` — the process that
  was going to finish it is gone, so the block is failed with
  ``error.code = "interrupted"``;
* a pending approval card — its waiter died with the process, so it must be
  expired before a late Approve can promise a run that will never happen.

Both are settled here, once, before the HTTP server starts. The append-only
event log gets the same ``approval_resolved`` event a normal decision would
write, and every expiry is audited without command text or secret values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from openshell.audit import AuditLog

from .service import ChatService
from .store import ChatStore, iso_now

__all__ = ["RecoveryReport", "settle_interrupted"]

_TOOL_STATUSES = frozenset({"running", "waiting_approval"})
_INTERRUPTED_ERROR = {
    "code": "interrupted",
    "message": "The server restarted before this tool finished.",
}
_PENDING_LIMIT = 200


@dataclass(frozen=True)
class RecoveryReport:
    """What boot recovery settled. Safe to print: ids and counts only."""

    closed_turns: tuple[str, ...] = ()
    failed_tool_calls: int = 0
    expired_approvals: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "closed_turns": list(self.closed_turns),
            "failed_tool_calls": self.failed_tool_calls,
            "expired_approvals": list(self.expired_approvals),
        }


def _fail_running_tools(blocks: Iterable[dict[str, Any]]) -> int:
    failed = 0
    for block in blocks:
        if block.get("type") != "tool_call":
            continue
        if block.get("status") not in _TOOL_STATUSES:
            continue
        block["status"] = "failed"
        detail = block.setdefault("detail", {})
        if isinstance(detail, dict):
            detail["error"] = dict(_INTERRUPTED_ERROR)
        failed += 1
    return failed


def _apply_expiry(
    blocks: Iterable[dict[str, Any]], approval_id: str, card: Mapping[str, Any]
) -> bool:
    """Update the embedded approval block; never reopen a failed tool block."""

    changed = False
    for block in blocks:
        if block.get("type") != "approval":
            continue
        approval = block.get("approval")
        if not isinstance(approval, dict):
            continue
        if approval.get("id") != approval_id:
            continue
        approval["status"] = card.get("status")
        approval["decided_at"] = card.get("decided_at")
        approval["consumed_at"] = card.get("consumed_at")
        changed = True
    return changed


def _assistant_message_id(store: ChatStore, card: Mapping[str, Any]) -> str | None:
    turn_id = card.get("turn_id")
    if not isinstance(turn_id, str) or not turn_id:
        return None
    turn = store.get_turn(turn_id)
    if turn is None:
        return None
    return str(turn["assistant_message_id"])


def settle_interrupted(
    service: ChatService, store: ChatStore, audit: AuditLog
) -> RecoveryReport:
    """Recover a database left behind by an unclean shutdown.

    Call exactly once during boot, before accepting requests. The method is
    idempotent in effect: a second call finds no running turns and no pending
    approvals, so it emits no duplicate ``turn_finished`` or
    ``approval_resolved`` events.
    """

    # This is the one and only call in the boot path.
    closed = tuple(service.recover_interrupted())
    failed_tool_calls = 0

    for turn_id in closed:
        turn = store.get_turn(turn_id)
        if turn is None:  # pragma: no cover - recovery just read it
            continue
        message_id = str(turn["assistant_message_id"])
        blocks = store.get_blocks(message_id)
        failed = _fail_running_tools(blocks)
        if failed:
            store.update_message(message_id, blocks=blocks)
        failed_tool_calls += failed
        audit.record(
            "turn_interrupted",
            turn_id=turn_id,
            outcome="interrupted",
            extra={
                "reason": "server_restart",
                "message_id": message_id,
                "failed_tool_calls": failed,
            },
        )

    expired: list[str] = []
    seen: set[str] = set()
    while True:
        pending = store.list_approvals(status="pending", limit=_PENDING_LIMIT)
        fresh = [
            card
            for card in pending
            if isinstance(card.get("id"), str) and card["id"] not in seen
        ]
        if not fresh:
            break
        for card in fresh:
            approval_id = str(card["id"])
            seen.add(approval_id)
            decided = iso_now()
            updated = store.transition_approval(
                approval_id,
                "expired",
                allowed_from=("pending",),
                decided_at=decided,
            )
            if updated is None:
                # Another writer settled it first; respect that decision.
                continue
            expired.append(approval_id)

            message_id = _assistant_message_id(store, updated)
            if message_id:
                blocks = store.get_blocks(message_id)
                if _apply_expiry(blocks, approval_id, updated):
                    store.update_message(message_id, blocks=blocks)

            thread_id = str(updated.get("thread_id") or "")
            if thread_id and store.get_thread(thread_id) is not None:
                store.append_event(
                    thread_id,
                    "approval_resolved",
                    {
                        "approval_id": approval_id,
                        "thread_id": thread_id,
                        "turn_id": updated.get("turn_id"),
                        "status": "expired",
                        "decided_at": updated.get("decided_at") or decided,
                        "actor": "system",
                        "executed": False,
                    },
                )

            audit.record(
                "approval_expired",
                turn_id=(
                    str(updated["turn_id"])
                    if updated.get("turn_id") is not None
                    else None
                ),
                request_id=approval_id,
                tool=(
                    str(updated["tool"])
                    if updated.get("tool") is not None
                    else None
                ),
                decision="expired",
                outcome="interrupted",
                extra={
                    "reason": "server_restart",
                    "expires_at": updated.get("expires_at"),
                },
            )

    return RecoveryReport(
        closed_turns=closed,
        failed_tool_calls=failed_tool_calls,
        expired_approvals=tuple(expired),
    )

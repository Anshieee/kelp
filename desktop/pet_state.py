"""Pet state mapping: streamed event -> desk-pet state.

Source of truth for the table mirrored in ``desktop/src/orb.js``
(:func:`petStateFromEvent`). Keep both in sync; the pytest suite pins this
module and the ``node --test`` suite pins the JS copy.

The keys are the event names ``chat/service.py`` actually emits. Anything not
listed keeps the current state (``None``), which is what stops a noisy event
from resetting the pet's animation mid-loop.
"""

from __future__ import annotations

PET_STATES = ("working", "waiting", "done", "idle")

EVENT_TO_STATE: dict[str, str] = {
    "turn_started": "working",
    "token_delta": "working",
    "tool_call_started": "working",
    "tool_call_finished": "working",
    "approval_requested": "waiting",
    "approval_resolved": "working",
    "turn_finished": "done",
}

#: A turn that ended badly settles rather than celebrating.
IDLE_REASONS = frozenset({"error", "cancelled"})


def pet_state_from_event(event: dict | None) -> str | None:
    """Map one streamed event to a pet state, or None to keep the current state."""
    if not isinstance(event, dict):
        return None
    event_type = event.get("type")
    if not isinstance(event_type, str):
        return None
    if event_type == "turn_finished":
        return "idle" if event.get("reason") in IDLE_REASONS else "done"
    return EVENT_TO_STATE.get(event_type)

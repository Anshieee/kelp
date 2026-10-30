"""Pet state mapping: SSE event type -> desk-pet state.

Source of truth for the table mirrored in ``desktop/src/orb.js``
(:func:`petStateFromEvent`). Keep both in sync; the pytest suite pins this
module and the ``node --test`` suite pins the JS copy.
"""

from __future__ import annotations

PET_STATES = ("working", "waiting", "done", "idle")

EVENT_TO_STATE: dict[str, str] = {
    "turn_started": "working",
    "turn_stopped": "idle",
    "token_delta": "working",
    "tool_call": "working",
    "approval_created": "waiting",
    "approval_pending": "waiting",
    "approval_decided": "working",
    "turn_finished": "done",
    "turn_failed": "idle",
    "stream_closed": "idle",
}


def pet_state_from_event(event: dict | None) -> str | None:
    """Map one SSE event to a pet state, or None to keep the current state."""
    if not isinstance(event, dict):
        return None
    event_type = event.get("type")
    if not isinstance(event_type, str):
        return None
    return EVENT_TO_STATE.get(event_type)

import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { petStateFromEvent, PET_STATES, ORB_FLAVOURS } from "../src/orb.js";

// The keys must be the event names `chat/service.py` actually emits. The
// previous table was written against names the server never sent, so half of
// it could not fire — `approval_requested`/`approval_resolved` are the real
// pair, not `approval_created`/`approval_decided`.

describe("petStateFromEvent", () => {
  it("maps the events the server actually emits", () => {
    // `turn_started` means the turn was accepted but no token has arrived: the
    // model is thinking. Streaming switches the pet to its working loop.
    assert.equal(petStateFromEvent({ type: "turn_started" }), "thinking");
    assert.equal(petStateFromEvent({ type: "token_delta" }), "working");
    assert.equal(petStateFromEvent({ type: "tool_call_started" }), "working");
    assert.equal(petStateFromEvent({ type: "tool_call_finished" }), "working");
    assert.equal(petStateFromEvent({ type: "approval_requested" }), "waiting");
    assert.equal(petStateFromEvent({ type: "approval_resolved" }), "working");
  });

  it("celebrates a clean finish and settles a bad one", () => {
    assert.equal(petStateFromEvent({ type: "turn_finished", reason: "stop" }), "done");
    assert.equal(petStateFromEvent({ type: "turn_finished", reason: "length" }), "done");
    assert.equal(petStateFromEvent({ type: "turn_finished", reason: "budget" }), "done");
    assert.equal(petStateFromEvent({ type: "turn_finished", reason: "error" }), "idle");
    assert.equal(petStateFromEvent({ type: "turn_finished", reason: "cancelled" }), "idle");
    // No reason at all still reads as a completion.
    assert.equal(petStateFromEvent({ type: "turn_finished" }), "done");
  });

  it("keeps the current state for the noisy events", () => {
    // Resetting on these would restart the animation mid-loop on every token
    // batch, which is exactly what the kit's README warns against.
    for (const type of [
      "usage_updated",
      "sources_ready",
      "artifact_ready",
      "thread_updated",
      "message_created",
      "error",
      "heartbeat",
    ]) {
      assert.equal(petStateFromEvent({ type }), null, type);
    }
  });

  it("no longer knows the event names the server never sends", () => {
    for (const type of [
      "approval_created",
      "approval_pending",
      "approval_decided",
      "tool_call",
      "turn_stopped",
      "turn_failed",
      "stream_closed",
    ]) {
      assert.equal(petStateFromEvent({ type }), null, type);
    }
  });

  it("ignores malformed input", () => {
    assert.equal(petStateFromEvent({}), null);
    assert.equal(petStateFromEvent(null), null);
    assert.equal(petStateFromEvent(undefined), null);
    assert.equal(petStateFromEvent({ type: 42 }), null);
  });

  it("covers every pet state with a flavour", () => {
    assert.deepEqual([...PET_STATES].sort(), ["done", "idle", "thinking", "waiting", "working"]);
    assert.deepEqual(
      ORB_FLAVOURS.map((f) => f.state).sort(),
      [...PET_STATES].sort(),
    );
  });
});

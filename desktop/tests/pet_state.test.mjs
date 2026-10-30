import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { petStateFromEvent, PET_STATES, ORB_FLAVOURS } from "../src/orb.js";

describe("petStateFromEvent", () => {
  it("maps the live SSE stream to working/waiting/done/idle", () => {
    assert.equal(petStateFromEvent({ type: "turn_started" }), "working");
    assert.equal(petStateFromEvent({ type: "token_delta" }), "working");
    assert.equal(petStateFromEvent({ type: "approval_created" }), "waiting");
    assert.equal(petStateFromEvent({ type: "turn_finished" }), "done");
    assert.equal(petStateFromEvent({ type: "turn_failed" }), "idle");
  });

  it("keeps the current state for unknown or malformed events", () => {
    assert.equal(petStateFromEvent({ type: "heartbeat" }), null);
    assert.equal(petStateFromEvent({}), null);
    assert.equal(petStateFromEvent(null), null);
    assert.equal(petStateFromEvent({ type: 42 }), null);
  });

  it("covers every pet state with a flavour", () => {
    assert.deepEqual([...PET_STATES].sort(), ["done", "idle", "waiting", "working"]);
    assert.deepEqual(
      ORB_FLAVOURS.map((f) => f.state).sort(),
      [...PET_STATES].sort(),
    );
  });
});

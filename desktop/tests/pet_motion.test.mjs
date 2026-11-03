import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  CLICK_STATE,
  FIDGET_POOL,
  HOVER_STATE,
  ONE_SHOT_MS,
  canFidget,
  motionState,
  nextFidgetDelay,
  nextFidgetGap,
  pickFidget,
  resolveState,
} from "../src/pet_motion.js";

const IDLE = {
  base: "idle",
  oneShot: null,
  hovering: false,
  hidden: false,
  reducedMotion: false,
};

describe("pet motion layer", () => {
  it("keeps every state that means something out of the fidget pool", () => {
    assert.deepEqual([...FIDGET_POOL].sort(), ["curious", "happy", "surprised"]);
    // These describe real app state; a random draw would be the pet lying.
    for (const reserved of [
      "idle",
      "working",
      "waiting",
      "done",
      "offline",
      "thinking",
      "excited",
    ]) {
      assert.ok(!FIDGET_POOL.includes(reserved), reserved);
    }
  });

  it("picks a fidget inside the pool for any random value", () => {
    for (const value of [0, 0.2, 0.5, 0.99, 1]) {
      const picked = pickFidget(() => value);
      assert.ok(FIDGET_POOL.includes(picked), `${value} -> ${picked}`);
    }
  });

  it("keeps the fidget timing inside the agreed windows", () => {
    assert.equal(nextFidgetDelay(() => 0), 20_000);
    assert.equal(nextFidgetDelay(() => 1), 40_000);
    assert.equal(nextFidgetGap(() => 0), 30_000);
    assert.equal(nextFidgetGap(() => 1), 90_000);
  });

  it("maps each pointer motion to a kit state", () => {
    assert.equal(motionState("hover"), HOVER_STATE);
    assert.equal(motionState("click"), CLICK_STATE);
    assert.ok(FIDGET_POOL.includes(motionState("fidget")));
    assert.equal(motionState("nonsense"), null);
  });

  it("prefers a one-shot over a held hover over the turn state", () => {
    assert.equal(resolveState({ base: "working" }), "working");
    assert.equal(resolveState({ base: "working", hovering: true }), HOVER_STATE);
    assert.equal(
      resolveState({
        base: "working",
        hovering: true,
        oneShot: { kind: "click", state: CLICK_STATE },
      }),
      CLICK_STATE,
    );
  });

  it("only fidgets from a settled, unoccupied idle", () => {
    assert.equal(canFidget(IDLE), true);
    for (const base of ["working", "waiting", "done", "offline"]) {
      assert.equal(canFidget({ ...IDLE, base }), false, base);
    }
    assert.equal(canFidget({ ...IDLE, hovering: true }), false, "hovering");
    assert.equal(
      canFidget({ ...IDLE, oneShot: { kind: "click", state: CLICK_STATE } }),
      false,
      "one-shot running",
    );
    assert.equal(canFidget({ ...IDLE, hidden: true }), false, "document hidden");
    assert.equal(canFidget({ ...IDLE, reducedMotion: true }), false, "reduced motion");
  });

  it("runs a one-shot for exactly one kit loop", () => {
    // 64 frames at 40 ms, per the kit's manifest.
    assert.equal(ONE_SHOT_MS, 2560);
  });
});

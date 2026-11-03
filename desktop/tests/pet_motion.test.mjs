import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  CLICK_STATE,
  FIDGET_POOL,
  GAZE_TRAVEL_X,
  GAZE_TRAVEL_Y,
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
  it("gives the idle mascot a restless repertoire", () => {
    assert.deepEqual([...FIDGET_POOL].sort(), [
      "curious",
      "excited",
      "happy",
      "surprised",
      "thinking",
    ]);
  });

  it("keeps the states that mean something out of the fidget pool", () => {
    // `waiting` is actionable here (an approval is pending, sometimes about a
    // payment or a credential), so a random draw must not claim it. `idle`,
    // `working` and `done` would be indistinguishable from real state, and
    // `offline` would claim the server is down.
    for (const reserved of ["idle", "working", "waiting", "done", "offline"]) {
      assert.ok(!FIDGET_POOL.includes(reserved), reserved);
    }
  });

  it("picks a fidget inside the pool for any random value", () => {
    for (const value of [0, 0.2, 0.5, 0.99, 1]) {
      const picked = pickFidget(() => value);
      assert.ok(FIDGET_POOL.includes(picked), `${value} -> ${picked}`);
    }
  });

  it("never draws the same fidget twice in a row", () => {
    for (const avoid of FIDGET_POOL) {
      for (const value of [0, 0.25, 0.5, 0.75, 1]) {
        const picked = pickFidget(() => value, avoid);
        assert.notEqual(picked, avoid, `avoid ${avoid} at ${value}`);
        assert.ok(FIDGET_POOL.includes(picked), picked);
      }
    }
  });

  it("keeps the restlessness within the agreed windows", () => {
    assert.equal(nextFidgetDelay(() => 0), 6_000);
    assert.equal(nextFidgetDelay(() => 1), 14_000);
    assert.equal(nextFidgetGap(() => 0), 8_000);
    assert.equal(nextFidgetGap(() => 1), 25_000);
  });

  it("maps each pointer motion to a kit state", () => {
    assert.equal(motionState("hover"), HOVER_STATE);
    assert.equal(motionState("click"), CLICK_STATE);
    assert.ok(FIDGET_POOL.includes(motionState("fidget")));
    assert.equal(motionState("nonsense"), null);
  });

  it("keeps the gaze lean inside the element's overhang", () => {
    // styles.css puts the element at inset -8px, so any lean up to 8px still
    // covers the 96px window and never reveals an edge.
    assert.ok(GAZE_TRAVEL_X > 0 && GAZE_TRAVEL_X <= 8, `${GAZE_TRAVEL_X}`);
    assert.ok(GAZE_TRAVEL_Y > 0 && GAZE_TRAVEL_Y <= 8, `${GAZE_TRAVEL_Y}`);
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

// Transient motion layer for the desk pet.
//
// The kit ships ten loops. Four describe the turn (`working`/`waiting`/`done`/
// `idle`) and one describes the bridge (`offline`); the rest are expressions,
// and they are what hover, click and the idle fidget draw from. They live
// outside `PET_STATES` on purpose: a pointer is not a turn.
//
// Everything here is pure so the cadence and precedence rules are testable
// without a DOM.

/** One loop of the kit, from manifest.json: 64 frames at 40 ms. */
export const ONE_SHOT_MS = 2560;

/** How long a one-shot runs past the loop before it is cut short, so a
 *  dropped frame cannot leave the pet parked mid-hop. */
export const ONE_SHOT_SLACK_MS = 120;

/**
 * How long `done` plays before the pet settles back to idle.
 *
 * The celebration is a landing loop, not a state to live in: held, it reads as
 * a permanently happy mascot and the idle float never comes back. One loop is
 * long enough to register and short enough not to linger.
 */
export const SETTLE_MS = ONE_SHOT_MS + ONE_SHOT_SLACK_MS;

/** Is this turn state spent after one loop, rather than held until the next
 *  event? Only `done` is; `working`, `thinking` and `waiting` mean something
 *  is still happening. */
export const settlesToIdle = (state) => state === "done";

/** Pointer-driven states. Hover is held while the pointer stays; click fires. */
export const HOVER_STATE = "curious";
export const CLICK_STATE = "excited";

/** How far the mascot leans toward the cursor, in px, at full deflection.
 *  Kept small: the element overhangs its 96px window by 8px a side, so this
 *  both stays legible and never reveals an edge. */
export const GAZE_TRAVEL_X = 5;
export const GAZE_TRAVEL_Y = 4;

/**
 * Loops the idle fidget draws from — the mascot's restless repertoire:
 * listening (`curious`), mulling something over (`thinking`), startled
 * (`surprised`), pleased (`happy`), and briefly delighted (`excited`).
 *
 * Deliberately excluded:
 * - `idle`/`working`/`done`: they would be indistinguishable from real state;
 * - `offline`: it would claim the server is down;
 * - `waiting`: in *this* app that state means "an approval is pending", which
 *   is actionable and occasionally about payments or credentials. A random
 *   draw would raise a false alarm, so it stays reserved.
 */
export const FIDGET_POOL = ["curious", "thinking", "surprised", "happy", "excited"];

/** Fidget soon after settling: restlessness is the point (6–14 s). */
export const FIDGET_AFTER_MS = [6_000, 14_000];
/** Then keep it up, but never back to back (8–25 s). */
export const FIDGET_GAP_MS = [8_000, 25_000];

const between = ([low, high], random) => low + random() * (high - low);

/** Wall-clock delay before the first fidget after settling into idle. */
export const nextFidgetDelay = (random = Math.random) => between(FIDGET_AFTER_MS, random);

/** Wall-clock gap enforced after a fidget has played. */
export const nextFidgetGap = (random = Math.random) => between(FIDGET_GAP_MS, random);

/**
 * Pick a fidget, avoiding an immediate repeat so two draws never look like the
 * same beat twice. `avoid` is the state last played, if any.
 */
export function pickFidget(random = Math.random, avoid = null) {
  const candidates = FIDGET_POOL.filter((state) => state !== avoid);
  const pool = candidates.length ? candidates : FIDGET_POOL;
  const index = Math.min(pool.length - 1, Math.floor(random() * pool.length));
  return pool[index];
}

/** The kit state one kind of transient motion renders. */
export function motionState(kind, random = Math.random, avoid = null) {
  if (kind === "hover") return HOVER_STATE;
  if (kind === "click") return CLICK_STATE;
  if (kind === "fidget") return pickFidget(random, avoid);
  return null;
}

/**
 * May a fidget play right now? Only from a settled, unoccupied idle: never
 * over another state, never while the pointer is on the pet, never while
 * another one-shot is running, and never under reduced motion or a hidden
 * document (the kit's component pauses itself for those, but the timer that
 * *starts* a fidget is ours to hold).
 */
export function canFidget({ base, oneShot, hovering, hidden, reducedMotion }) {
  if (reducedMotion || hidden || hovering || oneShot) return false;
  return base === "idle";
}

/**
 * What the pet should actually render. A one-shot beats a held hover, which
 * beats the streamed turn state — so clicking while hovering hops, then falls
 * back to `curious` because the pointer is still there.
 */
export function resolveState({ base, oneShot, hovering }) {
  if (oneShot) return oneShot.state;
  if (hovering) return HOVER_STATE;
  return base;
}

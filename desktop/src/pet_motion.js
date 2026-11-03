// Transient motion layer for the desk pet.
//
// The kit ships ten loops, but only four of them describe the turn
// (`working`/`waiting`/`done`/`idle`) and one describes the bridge
// (`offline`). The rest are reactions, and they are what hover, click and the
// idle fidget use. They deliberately live outside `PET_STATES`, which stays the
// streamed turn vocabulary: a pointer is not a turn.
//
// Everything here is pure so the timing and precedence rules are testable
// without a DOM.

/** One loop of the kit, from manifest.json: 64 frames at 40 ms. */
export const ONE_SHOT_MS = 2560;

/** How long a one-shot runs past the loop before it is cut short, so a
 *  dropped frame cannot leave the pet stuck mid-hop. */
export const ONE_SHOT_SLACK_MS = 120;

/** Pointer-driven states. Hover is held while the pointer stays; click fires. */
export const HOVER_STATE = "curious";
export const CLICK_STATE = "excited";

/**
 * Loops the idle fidget may draw from. Deliberately excludes the six that mean
 * something: `idle`/`working`/`waiting`/`done`/`offline` describe real app
 * state, `thinking` implies processing that is not happening, and `excited` is
 * kept for the click so a click always reads as *the* celebration. A random
 * draw from those would be the pet lying about what is going on.
 */
export const FIDGET_POOL = ["curious", "happy", "surprised"];

/** Idle must last this long before the first fidget (20–40 s). */
export const FIDGET_AFTER_MS = [20_000, 40_000];
/** And at least this long between fidgets (30–90 s). */
export const FIDGET_GAP_MS = [30_000, 90_000];

const between = ([low, high], random) => low + random() * (high - low);

/** Wall-clock delay before the next fidget may play. */
export const nextFidgetDelay = (random = Math.random) => between(FIDGET_AFTER_MS, random);

/** Wall-clock gap enforced after a fidget has played. */
export const nextFidgetGap = (random = Math.random) => between(FIDGET_GAP_MS, random);

export function pickFidget(random = Math.random) {
  const index = Math.min(
    FIDGET_POOL.length - 1,
    Math.floor(random() * FIDGET_POOL.length),
  );
  return FIDGET_POOL[index];
}

/** The kit state one kind of transient motion renders. */
export function motionState(kind, random = Math.random) {
  if (kind === "hover") return HOVER_STATE;
  if (kind === "click") return CLICK_STATE;
  if (kind === "fidget") return pickFidget(random);
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

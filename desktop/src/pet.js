// Desk-pet overlay: the animated avatar, and the interaction states over it.
//
// State arrives from the Rust bridge, never from a browser EventSource: the
// server has no CORS layer and its CSRF gate rejects a tauri:// origin, so the
// webview cannot reach it directly (docs/DESKTOP_API_BRIDGE_PLAN.md). The bridge
// already broadcasts `chat-event` / `chat-stream-state` to every window, so the
// pet only listens. It must never open its own stream: the bridge keys streams
// by thread id, so a second opener would cancel the main window's.
//
// Layers, lowest first: the streamed turn state, a held hover, a one-shot
// (click or fidget). A real state change preempts a one-shot; hover is held
// until the pointer leaves. `offline` is reachability rather than a turn state,
// so the bridge decides it, not the event table.
//
// Between real turns the mascot fidgets on its own — a restless rotation of
// thinking / curious / surprised / happy / excited expressions, never twice in
// a row (see pet_motion.js for what is deliberately excluded and why).
//
// Gaze leans the mascot toward the cursor; the kit's loops have their eyes
// baked in, so the lean is what stands in for eye tracking.
//
// Right-click still mutes motion by switching the kit to its still.

import { petStateFromEvent } from "./orb.js";
import {
  GAZE_TRAVEL_X,
  GAZE_TRAVEL_Y,
  ONE_SHOT_MS,
  ONE_SHOT_SLACK_MS,
  SETTLE_MS,
  canFidget,
  motionState,
  nextFidgetDelay,
  nextFidgetGap,
  resolveState,
  settlesToIdle,
} from "./pet_motion.js";

const host = document.querySelector("[data-orb]");
const companion = document.getElementById("companion");
const fallback = document.querySelector(".pet-fallback");
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

let base = "idle";
let oneShot = null;
let hovering = false;
let muted = false;
let lastFidget = null;
let oneShotTimer = 0;
let fidgetTimer = 0;
let settleTimer = 0;

// One source of truth for the lean distance; styles.css consumes these.
host?.style.setProperty("--gaze-x", `${GAZE_TRAVEL_X}px`);
host?.style.setProperty("--gaze-y", `${GAZE_TRAVEL_Y}px`);

function render() {
  const state = resolveState({ base, oneShot, hovering });
  host.dataset.petState = state;
  host.dataset.petMotion = oneShot ? oneShot.kind : hovering ? "hover" : "";
  companion?.setAttribute("state", state);
}

function cancelOneShot() {
  clearTimeout(oneShotTimer);
  oneShot = null;
}

/* -- transient motion --------------------------------------------------- */

function playOneShot(kind) {
  const state = motionState(kind, Math.random, kind === "fidget" ? lastFidget : null);
  if (!state) return;
  if (kind === "fidget") lastFidget = state;
  oneShot = { kind, state };
  clearTimeout(oneShotTimer);
  // The kit's loops run 2.56 s; the slack absorbs a dropped frame so the pet
  // can never be left parked mid-hop.
  oneShotTimer = setTimeout(() => {
    oneShot = null;
    render();
    scheduleFidget(nextFidgetGap());
  }, ONE_SHOT_MS + ONE_SHOT_SLACK_MS);
  render();
}

function scheduleFidget(delay) {
  clearTimeout(fidgetTimer);
  if (reduceMotion.matches) return;
  fidgetTimer = setTimeout(() => {
    const allowed = canFidget({
      base,
      oneShot,
      hovering,
      hidden: document.hidden,
      reducedMotion: reduceMotion.matches,
    });
    if (allowed) playOneShot("fidget");
    else scheduleFidget(nextFidgetGap());
  }, delay ?? nextFidgetDelay());
}

/* -- streamed state ----------------------------------------------------- */

/**
 * A finished turn celebrates for one loop and then settles. Without this the
 * pet parks on `done` — the happy landing — and never floats again, which is
 * exactly what "it only shows the happy kelp" looks like.
 */
function settleAfterCelebration() {
  clearTimeout(settleTimer);
  if (!settlesToIdle(base)) return;
  settleTimer = setTimeout(() => {
    if (!settlesToIdle(base)) return; // a real event overtook the celebration
    base = "idle";
    render();
    scheduleFidget(nextFidgetGap());
  }, SETTLE_MS);
}

function applyEvent(type, reason) {
  const state = petStateFromEvent({ type, reason });
  if (!state || state === base) return;
  base = state;
  // A real state change preempts a one-shot. Hover is not one, so a pointer
  // resting on the pet keeps its own expression through the change.
  if (oneShot) cancelOneShot();
  render();
  if (base === "idle") scheduleFidget(nextFidgetGap());
  else clearTimeout(fidgetTimer);
  settleAfterCelebration();
}

/** Reachability, not a turn state: only the bridge can decide this. */
function setOffline(offline) {
  if (offline) {
    if (base === "offline") return;
    base = "offline";
    cancelOneShot();
    clearTimeout(fidgetTimer);
    clearTimeout(settleTimer);
  } else {
    if (base !== "offline") return; // never stomp a live turn state
    base = "idle";
    scheduleFidget(nextFidgetGap());
  }
  render();
}

void (async () => {
  try {
    const { listen } = await import("@tauri-apps/api/event");
    await listen("chat-event", (event) => {
      const payload = event.payload || {};
      const data = payload.data || {};
      applyEvent(payload.event ?? data.type, data.reason);
    });
    // A stream that closes is not an unreachable server; only an error is.
    await listen("chat-stream-state", (event) => {
      if (event.payload?.state === "error") setOffline(true);
    });
    await listen("bridge-reachability", (event) => {
      setOffline(event.payload?.reachable === false);
    });
  } catch {
    /* Plain browser: no bridge, so the pet simply stays idle. */
  }
})();

/* -- fallback ----------------------------------------------------------- */

function showFallback() {
  if (!fallback) return;
  fallback.hidden = false;
  host.dataset.petFallback = "true";
}

// The kit's own stills already cover reduced motion, so this only covers what
// the component cannot: its assets missing or unreadable.
if (!customElements.get("kelp-pet")) {
  showFallback();
} else {
  companion?.shadowRoot?.querySelector("img")?.addEventListener("error", showFallback);
}

/* -- gaze --------------------------------------------------------------- */

// The kit's loops have their eyes baked into the artwork, so the mascot cannot
// have independently moving pupils. What it can do — and what reads as
// tracking — is lean toward the cursor: the eyes sit high in the body, so
// shifting the whole sprite moves them. The same custom properties drive the
// fallback's vector eyes, which *do* move independently.
function setGaze(x, y) {
  if (!Number.isFinite(x) || !Number.isFinite(y)) return;
  host.style.setProperty("--gx", Math.max(-1, Math.min(1, x)).toFixed(3));
  host.style.setProperty("--gy", Math.max(-1, Math.min(1, y)).toFixed(3));
}

window.addEventListener(
  "pointermove",
  (event) => {
    const rect = host.getBoundingClientRect();
    if (!rect.width) return;
    setGaze(
      ((event.clientX - rect.left) / rect.width) * 2 - 1,
      ((event.clientY - rect.top) / rect.height) * 2 - 1,
    );
  },
  { passive: true },
);

// The shell feeds the cursor from anywhere on screen: a webview only hears
// pointer events inside its own window.
void (async () => {
  try {
    const { listen } = await import("@tauri-apps/api/event");
    await listen("pet-gaze", (event) => {
      const gaze = event.payload || {};
      setGaze(Number(gaze.x), Number(gaze.y));
    });
  } catch {
    /* Plain browser: the pointermove handler above is all there is. */
  }
})();

/* -- interaction -------------------------------------------------------- */

host?.addEventListener("pointerenter", () => {
  hovering = true;
  clearTimeout(fidgetTimer); // never fidget under the pointer
  render();
});

host?.addEventListener("pointerleave", () => {
  hovering = false;
  render();
  scheduleFidget(nextFidgetGap());
});

// Click: summon or dismiss the panel. The hop is a one-shot and must never
// delay the toggle — the panel opens while the pet is still moving.
host?.addEventListener("click", () => {
  playOneShot("click");
  void togglePanel();
});

host?.addEventListener("contextmenu", (event) => {
  event.preventDefault();
  toggleMute();
});

async function togglePanel() {
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("toggle_overlay");
  } catch {
    /* Plain browser: there is no panel window to summon. */
  }
}

export function toggleMute() {
  muted = !muted;
  document.body.classList.toggle("motion-paused", muted);
  companion?.toggleAttribute("paused", muted); // the component shows its still
  if (muted) cancelOneShot();
  render();
  return muted;
}

// Keep the fidget loop in step with the OS setting. Pausing under reduced
// motion is correct, but a loop that never re-arms once reduce is switched back
// off leaves a permanently still pet — so re-arm on the change.
reduceMotion.addEventListener("change", () => {
  if (reduceMotion.matches) clearTimeout(fidgetTimer);
  else scheduleFidget(nextFidgetGap());
});

render();
scheduleFidget();

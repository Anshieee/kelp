// Desk-pet overlay wiring: orb + live SSE state + interactivity.
// Settled scope: notifications + one-tap Approve/Deny, click-to-open, mute.
//
// - Click pet: open (or focus) the main app window.
// - Approval notification: tap focuses the app on that approval card (one
//   more tap decides); the decision receipt renders in-app. Server caps
//   (single use, TTL, idempotent replay) are enforced server-side.
// - Notifications use the Web Notification API, so OS do-not-disturb
//   silences them automatically; the pet never buzzes through quiet hours.
// - Right-click: mute motion / quit (Tauri menu; no-op in plain browser).
// - Respects OS reduced-motion: CSS face only, no canvas loop.
// - Never shows text beyond a tooltip.

import { approvals, connectStream } from "./api.js";
import { SERVER_URL } from "./config.js";
import { initPetOrb, petStateFromEvent } from "./orb.js";

const host = document.querySelector("[data-orb]");
const orb = initPetOrb(host);
let muted = false;
let currentThread = new URLSearchParams(location.search).get("thread") || null;
let streamHandle = null;

function setState(state) {
  if (!state) return;
  host.dataset.petState = state; // SVG tints by state even with no GL
  orb?.setPetState?.(state);
}

// Pointer gaze for the SVG eyes (CSS vars drive .pet-gaze transform).
window.addEventListener(
  "pointermove",
  (e) => {
    const r = host.getBoundingClientRect();
    if (!r.width) return;
    const gx = (((e.clientX - r.left) / r.width) * 2 - 1).toFixed(3);
    const gy = (((e.clientY - r.top) / r.height) * 2 - 1).toFixed(3);
    host.style.setProperty("--gx", gx);
    host.style.setProperty("--gy", gy);
  },
  { passive: true },
);

/** Hook the pet to a thread's SSE stream (called by main window / deep link). */
export function watchThread(threadId) {
  if (streamHandle || !threadId || threadId === currentThread) return;
  currentThread = threadId;
  streamHandle = connectStream(threadId, {
    onEvent: (data) => {
      const state = petStateFromEvent(data);
      if (state) setState(state);
      if (data?.type === "approval_created") void notifyApproval(data.approval);
    },
  });
}

// Emit the server's receipt so the main window renders it in-app.
function emitReceipt(receipt) {
  const invoke = window.__TAURI__?.core?.invoke ?? window.__TAURI__?.invoke;
  invoke?.("plugin:event|emit", { event: "pet-decision-receipt", payload: receipt }).catch(() => {});
  try {
    localStorage.setItem("kelp.pet-receipt", JSON.stringify({ ...receipt, at: Date.now() }));
  } catch { /* private mode: Tauri bridge already carried it */ }
}

async function notifyApproval(approval) {
  if (!("Notification" in window)) return;
  if (Notification.permission !== "granted") {
    try {
      await Notification.requestPermission();
    } catch { /* ignore */ }
  }
  if (Notification.permission !== "granted" || !approval?.id) return;
  // Quiet under OS do-not-disturb: the OS suppresses this automatically.
  // No sound, no vibration, no custom priority — a plain banner.
  const n = new Notification("Kelp needs approval", {
    body: approval.title || approval.tool || "Review the pending action.",
    tag: approval.id,
    silent: true,
  });
  // One tap focuses the app on the approval card; the receipt comes back
  // in-app after Approve/Deny.
  n.onclick = () => focusMain(approval.id);
}

async function decideQuickAction(approvalId, decision) {
  try {
    await approvals.decide(approvalId, decision);
    setState("working");
    emitReceipt({ id: approvalId, decision, ok: true });
  } catch (e) {
    setState("waiting"); // stay put so the owner sees it still needs them
    emitReceipt({ id: approvalId, decision, ok: false, error: e.message });
  }
}

// Click: open / focus the main window, optionally on an approval card.
function focusMain(approvalId) {
  const url = approvalId
    ? `./index.html?approval=${encodeURIComponent(approvalId)}`
    : "./index.html";
  const invoke = window.__TAURI__?.core?.invoke ?? window.__TAURI__?.invoke;
  if (invoke) {
    invoke("plugin:window|show", { label: "main" }).catch(() => {});
    invoke("plugin:window|set_focus", { label: "main" }).catch(() => {});
    invoke("plugin:event|emit", {
      event: "pet-focus-approval",
      payload: approvalId ?? null,
    }).catch(() => {});
  } else {
    window.open(url, "_blank");
  }
}

host?.addEventListener("click", () => focusMain());

// Right-click: mute motion / quit (Tauri menu; no-op in plain browser).
host?.addEventListener("contextmenu", (e) => {
  e.preventDefault();
  toggleMute();
});

export function toggleMute() {
  muted = !muted;
  document.body.classList.toggle("motion-paused", muted);
  if (muted) host?.classList.remove("e-orb--webgl");
  return muted;
}

// Tauri deep-link / event bridge: main window forwards pet states + quick
// actions when both windows run under the same Tauri process.
if (window.__TAURI__?.event?.listen) {
  window.__TAURI__.event.listen("pet-state", (e) => setState(e.payload));
  window.__TAURI__.event.listen("pet-watch", (e) => watchThread(e.payload));
  window.__TAURI__.event.listen("pet-approve", (e) => {
    if (e.payload?.id) void decideQuickAction(e.payload.id, "approve");
  });
  window.__TAURI__.event.listen("pet-deny", (e) => {
    if (e.payload?.id) void decideQuickAction(e.payload.id, "deny");
  });
}

// Query-string bootstrap (?thread=…) for the plain-browser dev path.
if (currentThread) watchThread(currentThread)

// Keep SERVER_URL referenced so a misconfigured build fails loudly here,
// not at first fetch: every pet request must hit the one configured host.
if (!SERVER_URL.startsWith("https://") && !SERVER_URL.startsWith("http://127.0.0.1")) {
  console.warn(`[pet] SERVER_URL looks unconfigured: ${SERVER_URL}`);
}

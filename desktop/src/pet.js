// Desk-pet overlay wiring: orb + live SSE state + interactivity.
// Owner decisions: notifications + quick actions, click-to-open, mute.
//
// - Click pet: open (or focus) the main app window.
// - Right-click: context menu → mute motion (CSS face only) / quit.
// - `waiting` (approval pending): desktop notification with Approve / Deny
//   quick actions that call POST /api/approvals/{id}/decision.
// - Respects OS reduced-motion: CSS face only, no canvas loop.
// - Never shows text beyond the tooltip.

import { approvals, connectStream } from "./api.js";
import { initPetOrb, petStateFromEvent } from "./orb.js";

const host = document.querySelector("[data-orb]");
const orb = initPetOrb(host);
let muted = false;
let currentThread = new URLSearchParams(location.search).get("thread") || null;
let streamHandle = null;

function setState(state) {
  if (state) orb?.setPetState?.(state);
}

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

async function notifyApproval(approval) {
  if (!("Notification" in window)) return;
  if (Notification.permission !== "granted") {
    try {
      await Notification.requestPermission();
    } catch { /* ignore */ }
  }
  if (Notification.permission !== "granted" || !approval?.id) return;
  const n = new Notification("Kelp needs approval", {
    body: approval.title || approval.tool || "Review the pending action.",
    tag: approval.id,
  });
  n.onclick = () => focusMain();
}

async function decideQuickAction(approvalId, decision) {
  try {
    await approvals.decide(approvalId, decision);
    setState("working");
  } catch {
    setState("waiting"); // stay put so the owner sees it still needs them
  }
}

// Click: open / focus the main window.
function focusMain() {
  const invoke = window.__TAURI__?.core?.invoke ?? window.__TAURI__?.invoke;
  if (invoke) {
    invoke("plugin:window|show", { label: "main" }).catch(() => {});
    invoke("plugin:window|set_focus", { label: "main" }).catch(() => {});
  } else {
    window.open("./index.html", "_blank");
  }
}

host?.addEventListener("click", focusMain);

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
if (currentThread) watchThread(currentThread);

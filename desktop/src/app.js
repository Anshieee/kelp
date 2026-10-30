import { approvals, connectStream, devices, metrics, signIn, threads, wallet, parseSpendGauges } from "./api.js";
import { keychain } from "./keychain.js";
import { petStateFromEvent } from "./orb.js";
import {
  isDemo,
  demoThreads,
  demoMessages,
  demoApprovals,
  demoDevices,
  demoSpendText,
  demoCannedReply,
} from "./demo.js";

const DEMO = isDemo();

const $ = (id) => document.getElementById(id);

let currentThread = null;
let streamHandle = null;
export const petBus = new EventTarget(); // pet overlay subscribes in Tauri multi-window

// --- local diagnostics: in-memory ring buffer, never leaves the device -----
const DIAG_MAX = 100;
const diagLog = [];
function diag(event, detail) {
  diagLog.push(`${new Date().toISOString()} ${event}${detail ? " " + detail : ""}`);
  if (diagLog.length > DIAG_MAX) diagLog.shift();
  const el = $("diag");
  if (el) el.textContent = diagLog.join("\n");
}
$("copyDiag").onclick = async () => {
  try {
    await navigator.clipboard.writeText(diagLog.join("\n"));
  } catch { /* clipboard unavailable: text stays on screen */ }
};

function notifyPet(event) {
  const state = petStateFromEvent(event);
  if (state) {
    petBus.dispatchEvent(new CustomEvent("pet-state", { detail: state }));
    const invoke = window.__TAURI__?.core?.invoke ?? window.__TAURI__?.invoke;
    invoke?.("plugin:event|emit", { event: "pet-state", payload: state }).catch(() => {});
  }
}

function toast(msg) {
  $("signinMsg").textContent = msg;
}

// Receipt for a decision made from a pet notification: the server's answer,
// rendered in-app. Same receipt path as an in-app card tap.
export function showReceipt({ id, decision, ok, error }) {
  const el = $("receipt");
  el.textContent = ok
    ? `Approval ${id}: recorded as ${decision}d.`
    : `Approval ${id}: not recorded (${error || "request failed"}).`;
  diag("approval-receipt", `${id} ${decision} ok=${!!ok}`);
  void loadApprovals();
}

// Pet-window decisions arrive via Tauri events, or via localStorage for the
// plain-browser dev path (two windows, same origin).
if (window.__TAURI__?.event?.listen) {
  window.__TAURI__.event.listen("pet-decision-receipt", (e) => showReceipt(e.payload));
  window.__TAURI__.event.listen("pet-focus-approval", (e) => highlightApproval(e.payload));
}
window.addEventListener("storage", (e) => {
  if (e.key === "kelp.pet-receipt" && e.newValue) {
    try {
      showReceipt(JSON.parse(e.newValue));
    } catch { /* ignore malformed receipt */ }
  }
});

// Notification tap deep-link: ?approval= highlights the card (one more tap
// to decide). Applied on load and on focus.
function highlightApproval(id) {
  if (!id) return;
  const card = document.querySelector(`[data-approval="${CSS.escape(id)}"]`);
  if (card) {
    card.scrollIntoView({ block: "center" });
    card.classList.add("flash");
    setTimeout(() => card.classList.remove("flash"), 3000);
  }
}
window.addEventListener("focus", () => {
  highlightApproval(new URLSearchParams(location.search).get("approval"));
});
highlightApproval(new URLSearchParams(location.search).get("approval"));

// --- sign-in: one bearer exchange, then OS-keychain session only (§12.2) ----
if (DEMO) {
  // Demo mode: skip sign-in entirely, render fixtures, label everything.
  $("signin").hidden = true;
  $("demoBanner").hidden = false;
  $("main").hidden = false;
  $("connState").textContent = "demo";
  diag("demo", "fixtures only, no server");
  const liveBtn = document.createElement("button");
  liveBtn.textContent = "Use live server instead";
  liveBtn.className = "ghost";
  liveBtn.type = "button";
  liveBtn.onclick = () => {
    try {
      localStorage.setItem("kelp.live", "1");
    } catch { /* private mode: query param still applies */ }
    location.search = "?live=1";
  };
  $("demoBanner").append(" ", liveBtn);
  loadThreads();
  loadApprovals();
  loadDevices();
  loadSpend();
}
$("signinBtn").onclick = async () => {
  const bearer = $("bearer").value.trim();
  if (!bearer) return toast("Paste the root bearer first.");
  try {
    const res = await signIn(bearer, $("deviceLabel").value.trim());
    await keychain.setBearer(bearer); // OS keychain only, never localStorage
    $("bearer").value = "";
    $("main").hidden = false;
    $("connState").textContent = "online";
    $("connState").classList.add("on");
    diag("sign-in", res.session.device.label);
    toast(`Signed in as ${res.session.device.label}.`);
    await Promise.all([loadThreads(), loadApprovals(), loadDevices(), loadSpend()]);
  } catch (e) {
    diag("sign-in-failed", String(e.status ?? "?"));
    toast(`Sign in failed (${e.status ?? "?"}): ${e.message}`);
  }
};

$("signoutBtn").onclick = async () => {
  try {
    await devices.signOut();
  } catch { /* already signed out server-side */ }
  await keychain.clearBearer();
  streamHandle?.close();
  $("main").hidden = true;
  $("connState").textContent = "offline";
  $("connState").classList.remove("on");
  diag("sign-out");
  toast("Signed out.");
};

// --- threads + streaming chat ----------------------------------------------
async function loadThreads() {
  const ul = $("threadList");
  ul.innerHTML = "";
  const list = DEMO ? demoThreads : (await threads.list()).threads;
  for (const t of list) {
    const li = document.createElement("li");
    li.innerHTML = `<span></span>`;
    li.firstChild.textContent = t.title || t.id;
    const open = document.createElement("button");
    open.textContent = "Open";
    open.className = "ghost";
    open.onclick = () => openThread(t.id);
    li.append(open);
    ul.append(li);
  }
}

async function openThread(id) {
  currentThread = id;
  streamHandle?.close();
  $("stream").textContent = "";
  if (DEMO) {
    for (const m of demoMessages[id] ?? []) {
      $("stream").textContent += `${m.role}: ${m.content}\n`;
    }
    return;
  }
  const snap = await threads.get(id);
  for (const m of snap.messages ?? []) {
    if (m.role && m.content) $("stream").textContent += `${m.role}: ${m.content}\n`;
  }
  streamHandle = connectStream(id, {
    onState: (s) => {
      $("connState").textContent = s === "open" ? "online" : s;
      diag("stream", s);
      notifyPet({ type: s === "open" ? "stream_closed" : "turn_started" });
    },
    onEvent: (data) => {
      notifyPet(data);
      if (data.type === "token_delta" && data.text) $("stream").textContent += data.text;
      if (data.type === "approval_created") void loadApprovals();
      if (data.type === "turn_finished") $("stream").textContent += "\n";
    },
  });
}

$("newThread").onclick = async () => {
  const { thread } = await threads.create("Desktop chat");
  await loadThreads();
  await openThread(thread.id);
};
$("refreshThreads").onclick = loadThreads;

$("sendBtn").onclick = async () => {
  const text = $("chatInput").value.trim();
  if (!text || !currentThread) return toast(currentThread ? "Type a message." : "Open a thread first.");
  $("chatInput").value = "";
  if (DEMO) {
    notifyPet({ type: "turn_started" });
    $("stream").textContent += `user: ${text}\nassistant: ${demoCannedReply(text)}\n`;
    notifyPet({ type: "turn_finished" });
    return;
  }
  notifyPet({ type: "turn_started" });
  await threads.send(currentThread, text);
};

// --- approvals ---------------------------------------------------------------
async function decide(id, decision) {
  if (DEMO) {
    notifyPet({ type: "approval_decided" });
    showReceipt({ id, decision, ok: true });
    diag("demo-decide", `${id} ${decision} (not recorded)`);
    return;
  }
  try {
    await approvals.decide(id, decision);
    notifyPet({ type: "approval_decided" });
    showReceipt({ id, decision, ok: true });
  } catch (e) {
    showReceipt({ id, decision, ok: false, error: e.message });
  }
}

async function loadApprovals() {
  const list = DEMO ? demoApprovals : (await approvals.list("?status=pending")).approvals;
  const box = $("approvals");
  box.innerHTML = "";
  if (!list.length) {
    box.innerHTML = '<p class="hint">No pending approvals.</p>';
    return;
  }
  notifyPet({ type: "approval_pending" });
  for (const a of list) {
    const div = document.createElement("div");
    div.className = "approval";
    div.dataset.approval = a.id;
    div.innerHTML = `<strong></strong><p></p>`;
    div.querySelector("strong").textContent = a.title || a.id;
    div.querySelector("p").textContent = a.detail || a.tool || "";
    for (const d of ["approve", "deny"]) {
      const b = document.createElement("button");
      b.textContent = d === "approve" ? "Approve" : "Deny";
      if (d === "deny") b.className = "ghost";
      b.onclick = () => decide(a.id, d);
      div.append(b, " ");
    }
    box.append(div);
  }
}

// --- devices -----------------------------------------------------------------
async function loadDevices() {
  const list = DEMO ? demoDevices : (await devices.list()).devices;
  const ul = $("deviceList");
  ul.innerHTML = "";
  for (const d of list) {
    const li = document.createElement("li");
    li.innerHTML = `<span></span>`;
    li.firstChild.textContent = `${d.label}${d.current ? " (this device)" : ""} — last seen ${d.last_seen}`;
    const b = document.createElement("button");
    b.textContent = "Revoke";
    b.className = "ghost";
    b.onclick = async () => {
      await devices.revoke(d.id);
      diag("device-revoke", d.id);
      await loadDevices();
    };
    li.append(b);
    ul.append(li);
  }
}
$("refreshDevices").onclick = loadDevices;

// --- spend: mirrors the server -----------------------------------------------
// Warn at 80% of cap; at 100% a plain sentence, no numbers-as-alarm.
// Saved cards show amounts context only as brand + last4 + expiry —
// never card details.
function money(n) {
  return `$${Number(n).toFixed(2)}`;
}

async function loadSpend() {
  const warn = $("spendWarn");
  warn.hidden = true;
  warn.textContent = "";
  try {
    const text = DEMO ? demoSpendText : await metrics.spendText();
    const s = DEMO ? { window: "demo", usedUsd: 1.2, capUsd: 40, capped: false, warned: false } : parseSpendGauges(text);
    if (!s) {
      $("spend").textContent = "(no spend data reported)";
      return;
    }
    const cap = s.capUsd == null ? "no cap" : money(s.capUsd);
    $("spend").textContent = `${s.window}: ${money(s.usedUsd)} of ${cap}`;
    if (s.capped) {
      warn.hidden = false;
      warn.textContent = "Monthly spend cap reached. New turns are paused until the cap resets.";
      diag("spend-capped", money(s.usedUsd));
    } else if (s.warned) {
      warn.hidden = false;
      warn.textContent = `Spend is at ${Math.round(s.ratio * 100)}% of the ${s.window} cap.`;
      diag("spend-warn", String(Math.round(s.ratio * 100)));
    }
  } catch (e) {
    $("spend").textContent = `(unavailable: ${e.message})`;
  }
  try {
    const cards = await wallet.cards();
    const ul = $("cardList");
    ul.innerHTML = "";
    if (cards === null) return; // endpoint not mounted: section stays empty
    for (const c of cards) {
      const li = document.createElement("li");
      li.textContent = `${c.brand ?? "card"} •••• ${c.last4 ?? "????"}${c.exp ? ` exp ${c.exp}` : ""}`;
      ul.append(li);
    }
  } catch {
    /* cards are informational; spend gauges are the source of truth */
  }
}
$("refreshSpend").onclick = loadSpend;

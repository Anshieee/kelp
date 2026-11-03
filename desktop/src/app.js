import { approvals, connectStream, credentialRequests, devices, metrics, signIn, threads, wallet, parseSpendGauges } from "./api.js";
import { keychain } from "./keychain.js";
import { petStateFromEvent } from "./orb.js";

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
    await Promise.all([loadThreads(), loadApprovals(), loadCredentialRequests(), loadDevices(), loadSpend()]);
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
  const { threads: list } = await threads.list();
  const ul = $("threadList");
  ul.innerHTML = "";
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
  notifyPet({ type: "turn_started" });
  await threads.send(currentThread, text);
};

// --- approvals ---------------------------------------------------------------
async function decide(id, decision) {
  try {
    await approvals.decide(id, decision);
    notifyPet({ type: "approval_decided" });
    showReceipt({ id, decision, ok: true });
  } catch (e) {
    showReceipt({ id, decision, ok: false, error: e.message });
  }
}

async function loadApprovals() {
  const { approvals: list } = await approvals.list("?status=pending");
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

// Values remain only in password controls until the explicit private submit.
// Never echo values into DOM text, diagnostics, receipts, or persistent storage.
function credentialExpiry(value) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "Expiry unavailable" : `Expires ${date.toLocaleString()}`;
}

async function loadCredentialRequests() {
  const box = $("credentialRequests");
  try {
    const { requests = [] } = await credentialRequests.list();
    box.replaceChildren();
    if (!requests.length) { box.innerHTML = '<p class="hint">No credential requests. New requests will appear here.</p>'; return; }
    for (const request of requests) box.append(renderCredentialRequest(request));
  } catch {
    box.innerHTML = '<p class="hint" role="status">Could not load private sign-in requests. Refresh to try again.</p>';
  }
}

function renderCredentialRequest(request) {
  const card = document.createElement("article"); card.className = "credential-request";
  const heading = document.createElement("h3"); heading.textContent = request.origin;
  const status = document.createElement("p"); status.className = "credential-status";
  status.textContent = request.status === "pending" ? "Waiting on you" : request.status.replaceAll("_", " ");
  const expiry = document.createElement("p"); expiry.className = "hint"; expiry.textContent = credentialExpiry(request.expires_at);
  const intent = document.createElement("p"); intent.className = "hint";
  intent.textContent = request.submission?.enabled
    ? `The browser will fill these fields and press “${request.submission.label || "Submit"}”.`
    : "The browser will fill these fields only. It will not submit the form.";
  const fieldList = document.createElement("ul"); fieldList.className = "credential-fields";
  for (const field of request.fields ?? []) { const li = document.createElement("li"); li.textContent = field.label; fieldList.append(li); }
  card.append(heading, status, document.createTextNode(`Destination: ${request.origin}`), fieldList, intent, expiry);
  if (request.status === "pending") {
    const form = document.createElement("form"); form.className = "credential-entry";
    const inputs = [];
    for (const field of request.fields ?? []) {
      const label = document.createElement("label"); label.textContent = field.label;
      const input = document.createElement("input"); input.type = "password"; input.name = field.id;
      input.required = true; input.autocomplete = "new-password"; input.spellcheck = false;
      input.setAttribute("aria-label", `${field.label} (private)`); label.append(input); form.append(label); inputs.push(input);
    }
    const saveLabel = document.createElement("label"); saveLabel.className = "save-credential";
    const save = document.createElement("input"); save.type = "checkbox";
    save.disabled = (request.fields ?? []).some((field) => field.kind === "otp");
    saveLabel.append(save, document.createTextNode(" Save for this site")); form.append(saveLabel);
    const actions = document.createElement("div"); actions.className = "credential-actions";
    const approve = document.createElement("button"); approve.type = "submit"; approve.textContent = "Approve & send";
    const deny = document.createElement("button"); deny.type = "button"; deny.className = "ghost"; deny.textContent = "Deny";
    const message = document.createElement("p"); message.className = "hint"; message.setAttribute("role", "status");
    actions.append(approve, deny); form.append(actions, message);
    form.addEventListener("submit", async (event) => {
      event.preventDefault(); approve.disabled = true; deny.disabled = true; message.textContent = "Sending privately…";
      const values = Object.fromEntries(inputs.map((input) => [input.name, input.value]));
      try { await credentialRequests.submit(request.id, values, save.checked); inputs.forEach((input) => { input.value = ""; }); diag("credential-submitted", request.id); await loadCredentialRequests(); }
      catch { inputs.forEach((input) => { input.value = ""; }); message.textContent = "Could not send this request. It may have expired; refresh to check."; approve.disabled = false; deny.disabled = false; }
    });
    deny.addEventListener("click", async () => {
      approve.disabled = true; deny.disabled = true; message.textContent = "Denying request…";
      try { await credentialRequests.deny(request.id); inputs.forEach((input) => { input.value = ""; }); diag("credential-denied", request.id); await loadCredentialRequests(); }
      catch { message.textContent = "Could not deny this request. Refresh to check its status."; approve.disabled = false; deny.disabled = false; }
    });
    card.append(form);
  }
  if (["completed", "failed", "denied", "expired"].includes(request.status)) {
    const release = document.createElement("button"); release.type = "button"; release.className = "ghost"; release.textContent = "Allow browser checks to resume";
    release.addEventListener("click", async () => { release.disabled = true; try { await credentialRequests.release(request.id); await loadCredentialRequests(); } catch { release.disabled = false; } });
    card.append(release);
  }
  return card;
}
$("refreshCredentials").addEventListener("click", loadCredentialRequests);

// --- devices -----------------------------------------------------------------
async function loadDevices() {
  const { devices: list } = await devices.list();
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
    const text = await metrics.spendText();
    const s = parseSpendGauges(text);
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

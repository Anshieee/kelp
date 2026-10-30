import { api, approvals, connectStream, devices, getServerUrl, setServerUrl, shares, signIn, threads, metrics } from "./api.js";
import { keychain } from "./keychain.js";
import { petStateFromEvent } from "./orb.js";

const $ = (id) => document.getElementById(id);

let currentThread = null;
let streamHandle = null;
export const petBus = new EventTarget(); // pet overlay subscribes in Tauri multi-window

function notifyPet(event) {
  const state = petStateFromEvent(event);
  if (state) petBus.dispatchEvent(new CustomEvent("pet-state", { detail: state }));
}

function toast(msg) {
  $("signinMsg").textContent = msg;
}

// --- server URL ------------------------------------------------------------
$("serverUrl").value = getServerUrl();
$("saveServer").onclick = () => {
  setServerUrl($("serverUrl").value.trim() || "http://127.0.0.1:8000");
  $("serverUrl").value = getServerUrl();
  toast(`Server URL saved: ${getServerUrl()}`);
};

// --- sign-in: bearer exchanged once for a session cookie (§12.2) -----------
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
    toast(`Signed in as ${res.session.device.label}.`);
    await Promise.all([loadThreads(), loadApprovals(), loadDevices(), loadSpend()]);
  } catch (e) {
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
  const after = snap.messages?.length ? undefined : undefined;
  streamHandle = connectStream(id, {
    after,
    onState: (s) => {
      $("connState").textContent = s === "open" ? "online" : s;
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
    div.innerHTML = `<strong></strong><p></p>`;
    div.querySelector("strong").textContent = a.title || a.id;
    div.querySelector("p").textContent = a.detail || a.tool || "";
    for (const d of ["approve", "deny"]) {
      const b = document.createElement("button");
      b.textContent = d === "approve" ? "Approve" : "Deny";
      if (d === "deny") b.className = "ghost";
      b.onclick = async () => {
        await approvals.decide(a.id, d === "approve" ? "approve" : "deny");
        notifyPet({ type: "approval_decided" });
        await loadApprovals();
      };
      div.append(b, " ");
    }
    box.append(div);
  }
}

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
      await loadDevices();
    };
    li.append(b);
    ul.append(li);
  }
}
$("refreshDevices").onclick = loadDevices;

// --- shares ------------------------------------------------------------------
$("shareBtn").onclick = async () => {
  if (!currentThread) return toast("Open a thread first.");
  const { share } = await api(`/api/threads/${currentThread}/share`, { method: "POST", body: {} });
  toast(`Share created: ${share.url ?? share.token ?? share.id}`);
  await loadShares();
};
async function loadShares() {
  if (!currentThread) return;
  const { shares: list } = await shares.list(currentThread);
  const ul = $("shareList");
  ul.innerHTML = "";
  for (const s of list) {
    const li = document.createElement("li");
    li.innerHTML = `<span></span>`;
    li.firstChild.textContent = `${s.id} — ${s.status}`;
    const b = document.createElement("button");
    b.textContent = "Revoke";
    b.className = "ghost";
    b.onclick = async () => {
      await shares.revoke(currentThread, s.id);
      await loadShares();
    };
    li.append(b);
    ul.append(li);
  }
}

// --- spend -------------------------------------------------------------------
async function loadSpend() {
  try {
    const data = await metrics.spend();
    $("spend").textContent = JSON.stringify(data, null, 2);
  } catch (e) {
    $("spend").textContent = `(unavailable: ${e.message})`;
  }
}
$("refreshSpend").onclick = loadSpend;

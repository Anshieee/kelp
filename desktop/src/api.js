// Thin client over the Kelp server API.
// Auth: bearer is exchanged ONCE via POST /api/auth/session (§12.2); after
// that the HttpOnly session cookie + CSRF double-submit pair authorize
// every call. The bearer is never attached to routine requests.
// Telemetry: none. The client only ever talks to SERVER_URL (see
// config.js); diagnostics stay in a local in-memory log (app.js).

import { SERVER_URL } from "./config.js";

const BASE = SERVER_URL.replace(/\/+$/, "");

function csrfToken() {
  const m = document.cookie.match(/(?:^|;\s*)csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

export async function api(path, { method = "GET", body, bearer, csrf = true } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (bearer) headers["Authorization"] = `Bearer ${bearer}`;
  // Sign-in proves possession of the root bearer and is CSRF-exempt; every
  // other mutation carries the double-submit pair + same-origin Origin.
  if (csrf && !bearer && method !== "GET") {
    headers["X-CSRF-Token"] = csrfToken();
  }
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers,
    credentials: "include",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.message || `request failed: ${res.status}`);
    err.status = res.status;
    err.code = data.code;
    throw err;
  }
  return data;
}

/** Bearer exchange (§12.2): mint a server-side session, set cookies. */
export async function signIn(bearer, deviceLabel) {
  return api("/api/auth/session", {
    method: "POST",
    bearer,
    body: { device_label: deviceLabel || "Kelp desktop" },
  });
}

export const threads = {
  list: () => api("/api/threads"),
  create: (title) => api("/api/threads", { method: "POST", body: { title } }),
  get: (id) => api(`/api/threads/${id}`),
  send: (id, text) => api(`/api/threads/${id}/turns`, { method: "POST", body: { text } }),
};

export const approvals = {
  list: (params = "") => api(`/api/approvals${params}`),
  // One-tap decision from a notification or a card. Server caps (single
  // use, TTL, idempotent replay) are enforced server-side; the client only
  // renders the receipt the server returns.
  decide: (id, decision, note) =>
    api(`/api/approvals/${id}/decision`, { method: "POST", body: { decision, note } }),
};

export const devices = {
  list: () => api("/api/auth/devices"),
  revoke: (id) => api(`/api/auth/devices/${id}`, { method: "DELETE" }),
  signOut: () => api(`/api/auth/session`, { method: "DELETE" }),
};

/** Saved cards: brand + last4 + expiry only. Never card details. */
export const wallet = {
  cards: async () => {
    try {
      const data = await api("/api/wallet/cards");
      return Array.isArray(data.cards) ? data.cards : [];
    } catch (e) {
      if (e.status === 404) return null; // endpoint not mounted: hide section
      throw e;
    }
  },
};

// ---------------------------------------------------------------------------
// Spend. /api/metrics is Prometheus text; the spend gauges are
// `spend_used_usd{window=}`, `spend_cap_usd{window=}`, `spend_cap_ratio{}`.
// Month window preferred, day as fallback. Pure parser, unit-tested.
// ---------------------------------------------------------------------------

export function parseSpendGauges(text) {
  const out = {};
  for (const line of String(text).split("\n")) {
    if (line.startsWith("#") || !line.trim()) continue;
    const m = line.match(/^(spend_used_usd|spend_cap_usd|spend_cap_ratio)\{([^}]*)\}\s+([0-9.eE+-]+)/);
    if (!m) continue;
    const labels = Object.fromEntries(
      [...m[2].matchAll(/(\w+)="([^"]*)"/g)].map((g) => [g[1], g[2]]),
    );
    const window = labels.window || "month";
    out[window] ??= {};
    out[window][m[1]] = Number(m[3]);
  }
  const pick = out.month ?? out.day ?? null;
  if (!pick || !Number.isFinite(pick.spend_used_usd)) return null;
  const used = pick.spend_used_usd;
  const cap = Number.isFinite(pick.spend_cap_usd) ? pick.spend_cap_usd : null;
  const ratio = cap ? used / cap : (Number.isFinite(pick.spend_cap_ratio) ? pick.spend_cap_ratio : 0);
  return {
    window: out.month ? "month" : "day",
    usedUsd: used,
    capUsd: cap,
    ratio,
    warned: ratio >= 0.8 && ratio < 1,
    capped: ratio >= 1,
  };
}

export const metrics = {
  spendText: async () => {
    const res = await fetch(`${BASE}/api/metrics`, { credentials: "include" });
    if (!res.ok) throw new Error(`metrics unavailable: ${res.status}`);
    return res.text();
  },
};

// ---------------------------------------------------------------------------
// SSE stream with reconnect. The server supports resume via Last-Event-ID /
// ?after=; on drop we back off (500ms → 10s max, jittered) and resume from
// the last seen id so no frames are lost or duplicated.
// ---------------------------------------------------------------------------

export function connectStream(threadId, { onEvent, onState, after } = {}) {
  let es = null;
  let lastId = after ?? null;
  let tries = 0;
  let closed = false;
  let timer = 0;

  function open() {
    if (closed) return;
    const q = lastId != null ? `?after=${encodeURIComponent(lastId)}` : "";
    es = new EventSource(`${BASE}/api/threads/${threadId}/stream${q}`, {
      withCredentials: true,
    });
    onState?.("connecting");
    es.onopen = () => {
      tries = 0;
      onState?.("open");
    };
    es.onmessage = (ev) => {
      if (ev.lastEventId) lastId = ev.lastEventId;
      let data = null;
      try {
        data = JSON.parse(ev.data);
      } catch {
        return;
      }
      onEvent?.(data, ev);
    };
    es.onerror = () => {
      es.close();
      onState?.("reconnecting");
      tries += 1;
      const backoff = Math.min(10000, 500 * 2 ** Math.min(tries, 5));
      timer = setTimeout(open, backoff * (0.8 + Math.random() * 0.4));
    };
  }

  open();
  return {
    get lastEventId() {
      return lastId;
    },
    close() {
      closed = true;
      clearTimeout(timer);
      es?.close();
      onState?.("closed");
    },
  };
}

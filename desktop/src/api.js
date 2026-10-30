// Thin client over the Kelp server API.
// Auth: bearer is exchanged ONCE via POST /api/auth/session (§12.2); after
// that the HttpOnly session cookie + CSRF double-submit pair authorize
// every call. The bearer is never attached to routine requests.

const LS_KEY = "kelp.serverUrl";
export const DEFAULT_SERVER_URL = "http://127.0.0.1:8000";

export function getServerUrl() {
  return (localStorage.getItem(LS_KEY) || DEFAULT_SERVER_URL).replace(/\/+$/, "");
}

export function setServerUrl(url) {
  localStorage.setItem(LS_KEY, url.replace(/\/+$/, ""));
}

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
  const res = await fetch(`${getServerUrl()}${path}`, {
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
  decide: (id, decision, note) =>
    api(`/api/approvals/${id}/decision`, { method: "POST", body: { decision, note } }),
};

export const devices = {
  list: () => api("/api/auth/devices"),
  revoke: (id) => api(`/api/auth/devices/${id}`, { method: "DELETE" }),
  signOut: () => api("/api/auth/session", { method: "DELETE" }),
};

export const shares = {
  list: (threadId) => api(`/api/threads/${threadId}/shares`),
  create: (threadId, ttlSeconds) =>
    api(`/api/threads/${threadId}/share`, { method: "POST", body: { ttl_seconds: ttlSeconds } }),
  revoke: (threadId, id) => api(`/api/threads/${threadId}/shares/${id}`, { method: "DELETE" }),
};

export const metrics = {
  spend: () => api("/api/metrics"),
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
    es = new EventSource(`${getServerUrl()}/api/threads/${threadId}/stream${q}`, {
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

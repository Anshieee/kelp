/*
 * Session state for the phone app.
 *
 * Security model (see docs/CHAT_SPEC.md §12.2): the root bearer token is used
 * exactly once, at pairing, and is never stored. Afterwards the browser holds
 * access in an HttpOnly session cookie; this app keeps only the CSRF token
 * (not a credential, just a cookie-pair proof) and the device label in
 * sessionStorage, so they die with the tab.
 */

export interface DeviceInfo {
  id: string;
  label: string;
}

export interface SessionInfo {
  csrfToken: string;
  device: DeviceInfo;
  expiresAt: string | null;
}

const STORAGE_KEY = "kelp.phone.session";

let session: SessionInfo | null = loadSession();

function hasSessionStorage(): boolean {
  try {
    return typeof sessionStorage !== "undefined";
  } catch {
    return false;
  }
}

function loadSession(): SessionInfo | null {
  if (!hasSessionStorage()) return null;
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<SessionInfo>;
    if (
      typeof parsed?.csrfToken !== "string" ||
      !parsed.device ||
      typeof parsed.device.id !== "string" ||
      typeof parsed.device.label !== "string"
    ) {
      return null;
    }
    return {
      csrfToken: parsed.csrfToken,
      device: { id: parsed.device.id, label: parsed.device.label },
      expiresAt: typeof parsed.expiresAt === "string" ? parsed.expiresAt : null,
    };
  } catch {
    return null;
  }
}

export function getSession(): SessionInfo | null {
  return session;
}

export function setSession(next: SessionInfo | null): void {
  session = next;
  if (!hasSessionStorage()) return;
  try {
    if (next) sessionStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    else sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // Storage may be unavailable (private mode); the in-memory copy still works.
  }
}

export function getCsrfToken(): string | null {
  return session?.csrfToken ?? null;
}

/* --- thread list cache (memory only, so the list paints instantly) --- */

export interface ThreadListCache {
  threads: ThreadSummary[];
  fetchedAt: number;
}

export interface ThreadSummary {
  id: string;
  title: string;
  updated_at: string;
  last_message_preview: string | null;
  message_count: number | null;
  active_turn_id: string | null;
}

let threadCache: ThreadListCache | null = null;

export function cacheThreads(threads: ThreadSummary[]): void {
  threadCache = { threads, fetchedAt: Date.now() };
}

export function getCachedThreads(): ThreadListCache | null {
  return threadCache;
}

// Pure helpers for the Computer tab.
//
// Kept apart from the React component so the URL rules, the transport seam,
// the reconnect cadence and the state labels can be unit-tested without a live
// backend — the RFB path itself needs a real VNC server behind a websockify
// proxy (or `tests/tools/rfb_stub_server.mjs`), and only ever runs under the
// explicit `VITE_COMPUTER_TRANSPORT=rfb-stub` dev flag.

import type RFB from "@novnc/novnc";
import type { RFBOptions } from "@novnc/novnc";

/** Where the last-used viewer URL is remembered. Query and fragment are never
 *  persisted — see `rememberViewerUrl`. */
export const VIEWER_URL_STORAGE_KEY = "kelp.computer.viewerUrl";

/**
 * Which viewer transport this build uses.
 *
 * - `"managed"` (default): the tab points at a managed Steel/Boat session URL
 *   (`https://…/session/…`) the backend hands out. There is no raw socket for
 *   the user to type, and nothing to reconnect against.
 * - `"rfb-stub"`: the local websockify test session, for development against
 *   `tests/tools/rfb_stub_server.mjs` only. Selected explicitly via
 *   `VITE_COMPUTER_TRANSPORT=rfb-stub`; any other value falls back to managed.
 */
export type ComputerTransport = "managed" | "rfb-stub";

export function computerTransport(
  env: Record<string, string | undefined> | undefined = import.meta.env,
): ComputerTransport {
  return env?.VITE_COMPUTER_TRANSPORT === "rfb-stub" ? "rfb-stub" : "managed";
}

/** True when the raw RFB/websockify path is available in this build. */
export const isRfbTransport = (
  env: Record<string, string | undefined> | undefined = import.meta.env,
): boolean => computerTransport(env) === "rfb-stub";

/** A managed session URL names a session on an https host. */
const MANAGED_SESSION_PATH = /\/session\//;

/**
 * What the viewer should do with a backend session: which transport drives it,
 * or null when the session carries nothing usable.
 *
 * Managed session URLs are accepted on any transport (they never touch RFB);
 * raw `ws(s)` endpoints are only honoured on the explicit rfb-stub transport.
 */
export type ViewerTarget =
  | { kind: "managed-session"; url: string; sessionId: string }
  | { kind: "rfb-websockify"; url: string; sessionId: string }
  | null;

export function resolveViewerTarget(
  session: { id: string; url: string | null } | null | undefined,
  env: Record<string, string | undefined> | undefined = import.meta.env,
): ViewerTarget {
  const id = session?.id?.trim();
  const raw = session?.url?.trim();
  if (!id || !raw) return null;
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    return null;
  }
  if (parsed.protocol === "https:" && parsed.host && MANAGED_SESSION_PATH.test(parsed.pathname)) {
    return { kind: "managed-session", url: parsed.toString(), sessionId: id };
  }
  if ((parsed.protocol === "wss:" || parsed.protocol === "ws:") && parsed.host) {
    if (!isRfbTransport(env)) return null;
    const normalised = normaliseViewerUrl(raw);
    return normalised ? { kind: "rfb-websockify", url: normalised, sessionId: id } : null;
  }
  return null;
}

/**
 * The local test session's default. `websockify` presents the RFB stream on a
 * WebSocket, which is the only transport noVNC speaks. Only used on the
 * rfb-stub transport; the managed default is whatever session the backend
 * reports, and there is no typed-in fallback for it.
 */
export const DEFAULT_VIEWER_URL = "ws://127.0.0.1:6080/websockify";

/**
 * The tunnelled candidate, pending the live upgrade test: the same local
 * websockify port reached through a TLS-terminating tunnel.
 */
export const TUNNEL_VIEWER_URL = "wss://127.0.0.1:6080/websockify";

export type ViewerStatus =
  | "idle"
  | "connecting"
  | "auth"
  | "connected"
  | "disconnected"
  | "error";

const WS_SCHEMES = new Set(["ws:", "wss:"]);

/** Host spellings that still mean this machine when dialled. */
const LOOPBACK_HOSTS = new Set([
  "localhost",
  "127.0.0.1",
  "::1",
  "[::1]",
  "0.0.0.0",
  // IPv4-mapped IPv6: Node's URL hexifies the v4 tail (`::ffff:7f00:1`),
  // so both spellings are listed.
  "::ffff:127.0.0.1",
  "[::ffff:127.0.0.1]",
  "::ffff:7f00:1",
  "[::ffff:7f00:1]",
]);

/**
 * Accept what people actually paste — a bare host, an http(s) URL, or a ws(s)
 * URL — and return the ws(s) form, or null when it cannot be used.
 *
 * http(s) is rewritten rather than rejected because tunnel providers hand out
 * https links, and a bare host defaults to plain `ws://` because the local test
 * session is the common case.
 */
export function normaliseViewerUrl(input: string): string | null {
  const raw = input.trim();
  if (!raw) return null;

  const hasScheme = /^[a-z][a-z0-9+.-]*:\/\//i.test(raw);
  let parsed: URL;
  try {
    parsed = new URL(hasScheme ? raw : `ws://${raw}`);
  } catch {
    return null;
  }

  if (parsed.protocol === "http:") parsed.protocol = "ws:";
  else if (parsed.protocol === "https:") parsed.protocol = "wss:";

  if (!WS_SCHEMES.has(parsed.protocol) || !parsed.host) return null;
  return parsed.toString();
}

/**
 * What stands between the pasted text and a usable viewer URL, or null.
 *
 * The embedded client speaks the raw websockify stream; `vnc.html` is the
 * standalone viewer page for testing in a browser, and is the URL people most
 * often paste here by mistake.
 */
export function viewerUrlProblem(input: string): string | null {
  const raw = input.trim();
  if (!raw) return "Enter the viewer URL.";
  // Anchor to a path segment: a mid-path "...htm..." directory must not match.
  if (/(^|\/)vnc\.html?(?:[?#]|$)/i.test(raw)) {
    return (
      "That is the standalone viewer page (vnc.html), for testing in a browser. " +
      "This viewer needs the WebSocket URL instead — it usually ends in /websockify."
    );
  }
  const normalised = normaliseViewerUrl(raw);
  if (normalised && isCleartextRemote(normalised)) {
    return (
      "That address is not on this machine and does not use TLS — the VNC " +
      "password would travel in cleartext. Use a wss:// tunnel instead."
    );
  }
  return null;
}

/**
 * Backoff for automatic reconnects after a transport failure:
 * 1s, 2s, 5s, 10s, 15s, 30s, 60s — seven attempts, then give up. A wrong
 * address, a tunnel that stays down, or a bad certificate must not retry
 * forever; any successful connection resets the budget.
 */
export const RECONNECT_DELAYS_MS = [1_000, 2_000, 5_000, 10_000, 15_000, 30_000, 60_000];

export function reconnectDelay(attempt: number): number {
  const index = Math.max(
    0,
    Math.min(RECONNECT_DELAYS_MS.length - 1, Math.trunc(attempt) - 1),
  );
  return RECONNECT_DELAYS_MS[index];
}

/** Is this the built-in endpoint, i.e. nothing the user needs to change? */
export function isDefaultViewerUrl(url: string): boolean {
  const normalised = normaliseViewerUrl(url);
  return normalised !== null && normalised === normaliseViewerUrl(DEFAULT_VIEWER_URL);
}

/**
 * What to say when a session ends.
 *
 * A socket that never opened is an address problem, not a dropped session: a
 * stale remembered URL, a websockify that is not running, or a tunnel that is
 * down. Saying "the connection dropped" for that is a lie that sends people
 * looking in the wrong place, so it names the endpoint instead — redacted,
 * because the raw URL may carry a token.
 */
export function disconnectDetail({
  url,
  everConnected,
  exhausted,
}: {
  url: string;
  everConnected: boolean;
  exhausted: boolean;
}): string {
  const shown = redactViewerUrl(url);
  if (!everConnected) {
    return exhausted
      ? `Nothing is answering at ${shown}. Start a VNC server behind websockify there, or use the default address.`
      : `Could not reach ${shown} — nothing is listening there.`;
  }
  return exhausted
    ? "Could not reach the viewer after several attempts — check the tunnel and its certificate, then Try again."
    : "The connection dropped.";
}

/**
 * The four states the Computer button advertises. Transport trouble, a
 * rejected password, and a failed handshake all read as "Disconnected" up
 * there; the panel itself explains the difference.
 */
export function buttonStatusLabel(status: ViewerStatus): string {
  switch (status) {
    case "connecting":
      return "Connecting";
    case "auth":
      return "Password needed";
    case "connected":
      return "Live";
    default:
      return "Disconnected";
  }
}

const STATUS_LABELS: Record<ViewerStatus, string> = {
  idle: "Not connected",
  connecting: "Connecting…",
  auth: "Password needed",
  connected: "Connected",
  disconnected: "Disconnected",
  error: "Can't connect",
};

export const statusLabel = (status: ViewerStatus): string => STATUS_LABELS[status];

/**
 * How often the inline chat card copies the live framebuffer. Four per second
 * is enough to read as motion at thumbnail size and costs one drawImage.
 */
export const CARD_MIRROR_MS = 250;

/** Compact elapsed time for a card: "0.4s", "4.2s", "9s", "1m 03s". */
export function formatElapsed(ms: number | null): string | null {
  if (ms === null || !Number.isFinite(ms) || ms < 0) return null;
  if (ms < 10_000) return `${(ms / 1000).toFixed(1)}s`;
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${String(seconds % 60).padStart(2, "0")}s`;
}

/** A `ws://` URL to a non-loopback host: the VNC password would travel bare. */
export function isCleartextRemote(url: string): boolean {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return false;
  }
  // URL lowercases the scheme but not always the host for IPv6 forms, and a
  // `user@host` paste must be judged by its host, not its userinfo.
  if (parsed.protocol.toLowerCase() !== "ws:") return false;
  const host = parsed.hostname.toLowerCase().replace(/\.$/, "");
  if (LOOPBACK_HOSTS.has(host)) return false;
  return true;
}

/** The host/port/path without any query or fragment — tokens never persist. */
export function stripViewerSecrets(url: string): string {
  try {
    const parsed = new URL(url);
    parsed.search = "";
    parsed.hash = "";
    return parsed.toString();
  } catch {
    return url;
  }
}

/** Failure text may name the endpoint but must never echo a `?token=`. */
export function redactViewerUrl(url: string): string {
  try {
    const parsed = new URL(url);
    if (parsed.search || parsed.hash) {
      parsed.search = "";
      parsed.hash = "";
      return `${parsed.toString()} (credentials hidden)`;
    }
    return url;
  } catch {
    return url;
  }
}

/** Read the remembered URL, or null when storage is unavailable/empty. */
export function readStoredViewerUrl(): string | null {
  try {
    const value = globalThis.localStorage?.getItem(VIEWER_URL_STORAGE_KEY);
    return value?.trim() ? value : null;
  } catch {
    return null; // private mode, or storage disabled
  }
}

/**
 * Remember the URL with its query and fragment stripped: a `?token=` is a
 * credential, and localStorage is the wrong place for one. Storage failures
 * are not worth interrupting the viewer.
 */
export function rememberViewerUrl(url: string): void {
  try {
    globalThis.localStorage?.setItem(VIEWER_URL_STORAGE_KEY, stripViewerSecrets(url));
  } catch {
    /* ignore */
  }
}

/** Backwards-compatible alias. Prefer `rememberViewerUrl` for new call sites. */
export const writeStoredViewerUrl = rememberViewerUrl;

/**
 * How every viewer connection is configured.
 *
 * noVNC 1.7's constructor reads only `shared` (plus credentials/repeater/
 * protocols) — `viewOnly` and friends are *properties* that must be set on
 * the instance afterwards, which is what `applyViewerSettings` does. Passing
 * them as options would silently run the viewer interactive.
 *
 * `shared` matters too: the agent holds the same session, so a second viewer
 * must not disconnect it.
 */
export function viewerConnectionOptions(): RFBOptions {
  return { shared: true };
}

/**
 * Apply the per-instance viewer settings. `viewOnly` is the contract of this
 * tab — the agent drives the same session, so this window must neither send
 * input nor take the session away.
 */
export function applyViewerSettings(
  viewer: Pick<RFB, "viewOnly" | "scaleViewport" | "resizeSession">,
): void {
  viewer.viewOnly = true;
  viewer.scaleViewport = true;
  viewer.resizeSession = false;
}

/**
 * How to answer a `credentialsrequired` event. Standard VNC authentication
 * asks for a password and nothing else; a server that also wants a username or
 * a target is one this view-only viewer has no business answering.
 */
export type CredentialPlan =
  | { kind: "password" }
  | { kind: "unsupported"; requested: string[] };

export function planCredentials(types: readonly string[]): CredentialPlan {
  const requested = [...new Set(types.filter((type) => type !== "password"))];
  return requested.length > 0 ? { kind: "unsupported", requested } : { kind: "password" };
}

export function unsupportedCredentialsMessage(requested: readonly string[]): string {
  const list = requested.map((type) => `"${type}"`).join(", ");
  return `The server also asks for ${list}, which a view-only viewer never sends.`;
}

/**
 * The URL a fresh viewer points at. `VITE_VNC_WS_URL` overrides the built-in
 * local default without a code change — point it at `TUNNEL_VIEWER_URL` (or
 * any websockify endpoint) when testing the tunnel. A value that cannot be
 * used is ignored rather than half-applied.
 */
export function defaultViewerUrl(
  env: Record<string, string | undefined> | undefined = import.meta.env,
): string {
  const configured = env?.VITE_VNC_WS_URL;
  return (configured ? normaliseViewerUrl(configured) : null) ?? DEFAULT_VIEWER_URL;
}

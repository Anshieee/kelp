// The viewer's connection lifecycle, owned at the app level so the RFB
// session, WebSocket, and framebuffer survive hiding the Computer panel.
//
// The panel (ComputerPane) is presentational and renders whatever this hook
// holds. Connecting starts on its own once the Kelp session is ready — nobody
// has to open the tab first — and stops on sign-out, quit, or a viewer URL
// change.

import { useCallback, useEffect, useRef, useState } from "react";

import type RFB from "@novnc/novnc";

import {
  applyViewerSettings,
  defaultViewerUrl,
  disconnectDetail,
  normaliseViewerUrl,
  planCredentials,
  readStoredViewerUrl,
  RECONNECT_DELAYS_MS,
  reconnectDelay,
  viewerConnectionOptions,
  viewerUrlProblem,
  writeStoredViewerUrl,
  type CredentialPlan,
  type ViewerStatus,
} from "./computerViewer";

interface Session {
  url: string;
  /** Bumped to force a fresh connection on retry. */
  nonce: number;
}

export function useViewer({ enabled }: { enabled: boolean }) {
  const [urlInput, setUrlInput] = useState(() => readStoredViewerUrl() ?? defaultViewerUrl());
  const [session, setSession] = useState<Session | null>(null);
  const [status, setStatus] = useState<ViewerStatus>("idle");
  const [detail, setDetail] = useState<string | null>(null);
  // What the server asked for in `credentialsrequired`, and the password being
  // typed against it. Both live only in hook state: the password is dropped
  // the moment it is submitted or the session ends, and is never stored,
  // logged, or placed in a URL.
  const [credentials, setCredentials] = useState<CredentialPlan | null>(null);
  const [password, setPassword] = useState("");

  const hostRef = useRef<HTMLDivElement | null>(null);
  const viewerRef = useRef<RFB | null>(null);
  const attemptRef = useRef(0);
  const stoppedRef = useRef(false);
  const wasEnabledRef = useRef(false);

  const start = useCallback((rawUrl: string) => {
    const problem = viewerUrlProblem(rawUrl);
    if (problem) {
      setStatus("error");
      setDetail(problem);
      return;
    }
    const normalised = normaliseViewerUrl(rawUrl);
    if (!normalised) {
      setStatus("error");
      setDetail("That does not look like a WebSocket viewer URL.");
      return;
    }
    stoppedRef.current = false;
    attemptRef.current = 0;
    setPassword("");
    setCredentials(null);
    setUrlInput(normalised);
    writeStoredViewerUrl(normalised);
    // Bumping the nonce tears down whatever is running — a viewer URL change
    // always disconnects the old session first.
    setSession((current) => ({
      url: normalised,
      nonce: (current?.nonce ?? 0) + 1,
    }));
  }, []);

  useEffect(() => {
    const host = hostRef.current;
    if (!session || !host || !enabled) return;

    let disposed = false;
    let ended = false;
    // A failed handshake is deterministic — a wrong password would be wrong
    // again — so unlike a dropped socket it must not auto-retry.
    let authFailed = false;
    let sawAuthPrompt = false;
    // A socket that never opened is an address problem, not a session that
    // ended, and the two need different words.
    let everConnected = false;
    let viewer: RFB | null = null;
    let retryTimer = 0;

    setStatus("connecting");
    setDetail(null);
    setCredentials(null);
    setPassword("");

    void (async () => {
      try {
        const { default: RFB } = await import("@novnc/novnc");
        if (disposed) return;

        const rfb = new RFB(host, session.url, viewerConnectionOptions());
        applyViewerSettings(rfb);
        viewer = rfb;
        viewerRef.current = rfb;

        // Belt and braces: `applyViewerSettings` is unit-tested, but if a
        // noVNC upgrade ever ignored it, the last thing we want is a silently
        // writable session.
        if (!rfb.viewOnly) {
          authFailed = true;
          rfb.disconnect();
          throw new Error("The viewer did not start in view-only mode.");
        }

        rfb.addEventListener("connect", () => {
          if (disposed) return;
          everConnected = true;
          attemptRef.current = 0;
          setStatus("connected");
          setDetail(null);
        });

        rfb.addEventListener("disconnect", (event) => {
          ended = true;
          viewerRef.current = null;
          // Whatever was typed dies with the connection.
          setPassword("");
          setCredentials(null);
          if (disposed || stoppedRef.current) return;
          if (authFailed) return; // securityfailure already explained it
          const clean = event.detail.clean;
          if (sawAuthPrompt && !clean) {
            setStatus("error");
            setDetail("The server closed the connection before the password was accepted.");
            return;
          }
          attemptRef.current += 1;
          const exhausted = attemptRef.current > RECONNECT_DELAYS_MS.length;
          // A retry budget is for a session that was up and dropped. A dial
          // that never opened is reported against the address it tried, so a
          // stale remembered URL stops looking like a flaky tunnel.
          setStatus(exhausted ? "error" : "disconnected");
          setDetail(disconnectDetail({ url: session.url, everConnected, exhausted }));
          if (exhausted) return;
          retryTimer = window.setTimeout(() => {
            setSession((current) =>
              current ? { url: current.url, nonce: current.nonce + 1 } : current,
            );
          }, reconnectDelay(attemptRef.current));
        });

        rfb.addEventListener("securityfailure", (event) => {
          if (disposed) return;
          authFailed = true;
          const reason = event.detail.reason;
          setStatus("error");
          setDetail(
            sawAuthPrompt
              ? reason
                ? `The server rejected the password: ${reason}`
                : "The server rejected that password."
              : reason
                ? `The server rejected the connection: ${reason}`
                : "The VNC server rejected this viewer.",
          );
        });

        rfb.addEventListener("credentialsrequired", (event) => {
          if (disposed) return;
          sawAuthPrompt = true;
          setCredentials(planCredentials(event.detail.types));
          setStatus("auth");
          setDetail(null);
        });
      } catch (error) {
        if (disposed) return;
        viewerRef.current = null;
        setStatus("error");
        setDetail(error instanceof Error ? error.message : "Could not start the viewer.");
      }
    })();

    return () => {
      disposed = true;
      window.clearTimeout(retryTimer);
      viewerRef.current = null;
      // `disconnect()` on an already-dead RFB logs an error in noVNC, so only
      // close a session that is still up.
      if (!ended) {
        try {
          viewer?.disconnect();
        } catch {
          /* already gone */
        }
      }
      // Drop the last painted frame so a dead session never looks live.
      host.replaceChildren();
    };
  }, [session, enabled]);

  // Start on our own once the Kelp session is ready, and drop everything when
  // it is not (sign-out). A manual Disconnect keeps the viewer stopped until
  // Connect, Try again, a URL change — or the next sign-in, which starts a
  // fresh session and is allowed to auto-start again.
  useEffect(() => {
    if (!enabled) {
      wasEnabledRef.current = false;
      stoppedRef.current = true;
      setSession(null);
      setStatus("idle");
      setDetail(null);
      setPassword("");
      setCredentials(null);
      return;
    }
    const becomingReady = !wasEnabledRef.current;
    wasEnabledRef.current = true;
    if (!session && (becomingReady || !stoppedRef.current)) {
      start(readStoredViewerUrl() ?? defaultViewerUrl());
    }
  }, [enabled, session, start]);

  // Quitting the app must not leave the socket half-open.
  useEffect(() => {
    const onBeforeUnload = () => {
      try {
        viewerRef.current?.disconnect();
      } catch {
        /* already gone */
      }
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, []);

  const connect = useCallback(() => start(urlInput), [start, urlInput]);

  /** Forget a remembered endpoint that does not answer and go back to ours. */
  const resetToDefault = useCallback(() => {
    const fallback = defaultViewerUrl();
    setUrlInput(fallback);
    start(fallback);
  }, [start]);

  const disconnect = useCallback(() => {
    stoppedRef.current = true;
    setPassword("");
    setCredentials(null);
    setSession(null);
    setStatus("idle");
    setDetail(null);
    hostRef.current?.replaceChildren();
  }, []);

  const submitPassword = useCallback(() => {
    const viewer = viewerRef.current;
    if (!viewer || credentials?.kind !== "password" || !password) return;
    // Handed straight to noVNC and forgotten — nothing here keeps a copy.
    viewer.sendCredentials({ password });
    setPassword("");
    setCredentials(null);
    setStatus("connecting");
    setDetail(null);
  }, [credentials, password]);

  return {
    status,
    detail,
    credentials,
    password,
    setPassword,
    urlInput,
    setUrlInput,
    hostRef,
    connect,
    resetToDefault,
    disconnect,
    submitPassword,
  };
}

export type ViewerHandle = ReturnType<typeof useViewer>;

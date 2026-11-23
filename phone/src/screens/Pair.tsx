/*
 * Pairing screen: exchange the root KELP_AUTH_TOKEN for a device session,
 * exactly once. The token is never stored — only the session cookie (managed
 * by the browser) and the CSRF token (sessionStorage) survive this screen.
 */
import { useState, type FormEvent } from "react";
import { ApiError, pair } from "../api";
import { setSession, type SessionInfo } from "../state";
import SproutMark from "../components/SproutMark";

interface PairProps {
  onPaired: (session: SessionInfo) => void;
}

export default function Pair({ onPaired }: PairProps) {
  const [token, setToken] = useState("");
  const [label, setLabel] = useState("Phone");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const trimmed = token.trim();
    if (!trimmed || busy) return;
    setBusy(true);
    setError(null);
    try {
      const session = await pair(trimmed, label.trim() || "Phone");
      setSession(session);
      onPaired(session);
    } catch (cause) {
      if (cause instanceof ApiError) {
        setError(
          cause.code === "unauthorized"
            ? "That token wasn't accepted. Check KELP_AUTH_TOKEN and try again."
            : cause.message,
        );
      } else {
        setError("Pairing failed. Try again.");
      }
    } finally {
      setBusy(false);
      // Drop the token from memory immediately; it is never kept or logged.
      setToken("");
    }
  };

  return (
    <main className="screen pair-screen">
      <div className="pair-card">
        <div className="pair-logo" aria-hidden="true">
          <SproutMark size={40} />
        </div>
        <h1 className="wordmark-title">kelp</h1>
        <p className="pair-lede">
          Your private assistant. Pair this phone once with your server's auth token.
        </p>
        <form onSubmit={submit}>
          <label className="field">
            <span>Auth token</span>
            <input
              type="password"
              inputMode="text"
              autoComplete="off"
              autoCorrect="off"
              autoCapitalize="off"
              spellCheck={false}
              placeholder="KELP_AUTH_TOKEN"
              value={token}
              onChange={(event) => setToken(event.target.value)}
              disabled={busy}
              required
            />
          </label>
          <label className="field">
            <span>Device name</span>
            <input
              type="text"
              autoComplete="off"
              value={label}
              maxLength={40}
              onChange={(event) => setLabel(event.target.value)}
              disabled={busy}
            />
          </label>
          <button type="submit" className="btn btn-primary btn-block" disabled={busy || !token.trim()}>
            {busy ? "Pairing…" : "Pair this phone"}
          </button>
          {error && (
            <p className="form-error" role="alert">
              {error}
            </p>
          )}
        </form>
        <p className="pair-note">
          The token is sent once and never stored. The server gives this device its own
          session, which you can revoke any time from Settings.
        </p>
      </div>
    </main>
  );
}

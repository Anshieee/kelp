/*
 * Settings: this device, other signed-in devices, install hint, and a
 * server-glance card parsed from GET /api/metrics (Prometheus text format).
 */
import { useCallback, useEffect, useState } from "react";
import { ApiError, listDevices, revokeDevice, signOut } from "../api";
import { getSession, setSession } from "../state";
import StreamGlance from "../components/StreamGlance";

interface SettingsProps {
  onBack: () => void;
  onSignedOut: () => void;
}

export default function Settings({ onBack, onSignedOut }: SettingsProps) {
  const [devices, setDevices] = useState<Awaited<ReturnType<typeof listDevices>>["devices"] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [signingOut, setSigningOut] = useState(false);
  const session = getSession();

  const refresh = useCallback(async () => {
    try {
      const data = await listDevices();
      setDevices(data.devices);
      setError(null);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Couldn't load devices.");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const revoke = async (id: string) => {
    try {
      await revokeDevice(id);
      await refresh();
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Couldn't revoke that device.");
    }
  };

  const doSignOut = async () => {
    if (signingOut) return;
    setSigningOut(true);
    try {
      await signOut();
    } catch {
      // Even if the call fails, drop the local session; the cookie will
      // expire or the user can revoke the device from another one.
    }
    setSession(null);
    onSignedOut();
  };

  return (
    <main className="screen settings-screen">
      <header className="top-bar">
        <button type="button" className="icon-btn" aria-label="Back to threads" onClick={onBack}>
          <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
            <path d="M15.5 4.5 8 12l7.5 7.5" stroke="currentColor" strokeWidth="2" fill="none" strokeLinecap="round" />
          </svg>
        </button>
        <h1>Settings</h1>
      </header>

      <section className="settings-section">
        <h2>Install</h2>
        <InstallHint />
      </section>

      <section className="settings-section">
        <h2>Server glance</h2>
        <StreamGlance />
      </section>

      <section className="settings-section">
        <h2>Devices</h2>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        {devices === null ? (
          <p className="empty-note">Loading…</p>
        ) : (
          <ul className="device-list">
            {devices.map((device) => (
              <li key={device.id} className="device-item">
                <div className="device-info">
                  <span className="device-name">
                    <span
                      className={device.online ? "presence online" : "presence offline"}
                      aria-hidden="true"
                    />
                    {device.label}
                    {device.current && <span className="device-chip">this device</span>}
                  </span>
                  <span className="device-meta">
                    {device.online ? "online" : `last seen ${relativeTime(device.last_seen)}`}
                  </span>
                </div>
                {!device.current && (
                  <button
                    type="button"
                    className="btn btn-small btn-ghost danger-text"
                    onClick={() => void revoke(device.id)}
                  >
                    Revoke
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="settings-section">
        <h2>Session</h2>
        {session?.device.label && (
          <p className="settings-note">
            Signed in as “{session.device.label}”. Your auth token was used once at pairing and
            is not stored on this phone.
          </p>
        )}
        <button type="button" className="btn btn-danger btn-block" onClick={() => void doSignOut()} disabled={signingOut}>
          {signingOut ? "Signing out…" : "Sign out this device"}
        </button>
      </section>
    </main>
  );
}

function InstallHint() {
  const standalone =
    typeof window.matchMedia === "function" && window.matchMedia("(display-mode: standalone)").matches;
  if (standalone) {
    return <p className="settings-note">Kelp is installed and running as an app. You're all set.</p>;
  }
  const ios = /iP(hone|od|ad)/.test(navigator.userAgent);
  const android = /Android/.test(navigator.userAgent);
  if (ios) {
    return (
      <p className="settings-note">
        In Safari, tap <strong>Share</strong>, then <strong>Add to Home Screen</strong> to install
        Kelp as an app.
      </p>
    );
  }
  if (android) {
    return (
      <p className="settings-note">
        In Chrome, open the <strong>⋮ menu</strong> and choose <strong>Install app</strong> (or
        “Add to Home screen”).
      </p>
    );
  }
  return (
    <p className="settings-note">
      Use your browser's “Install app” / “Add to Home screen” option to put Kelp on your phone.
    </p>
  );
}

function relativeTime(iso: string | null): string {
  if (!iso) return "never";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "never";
  const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

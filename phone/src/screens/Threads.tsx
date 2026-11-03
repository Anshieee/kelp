/*
 * Threads list: recent conversations, a new-chat action, a pending-approvals
 * badge, and per-thread rename/delete. Paints instantly from the in-memory
 * cache, then refreshes from the server.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  createThread,
  deleteThread,
  listDevices,
  listThreads,
  pendingApprovals,
  renameThread,
} from "../api";
import { cacheThreads, getCachedThreads, type ThreadSummary } from "../state";

interface ThreadsProps {
  onOpenThread: (threadId: string) => void;
  onOpenSettings: () => void;
}

export default function Threads({ onOpenThread, onOpenSettings }: ThreadsProps) {
  const [threads, setThreads] = useState<ThreadSummary[]>(() => getCachedThreads()?.threads ?? []);
  const [loading, setLoading] = useState(threads.length === 0);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [pendingCount, setPendingCount] = useState(0);
  const [pendingThreadId, setPendingThreadId] = useState<string | null>(null);
  const [deviceLabel, setDeviceLabel] = useState<string | null>(null);
  const [menuThreadId, setMenuThreadId] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<{ id: string; title: string } | null>(null);
  const [deleting, setDeleting] = useState<ThreadSummary | null>(null);
  const loaded = useRef(false);

  const refresh = useCallback(async () => {
    try {
      const data = await listThreads(50);
      const mapped = data.threads.map((thread) => ({
        id: thread.id,
        title: thread.title,
        updated_at: thread.updated_at,
        last_message_preview: thread.last_message_preview ?? null,
        message_count: thread.message_count ?? null,
        active_turn_id: thread.active_turn_id ?? null,
      }));
      setThreads(mapped);
      cacheThreads(mapped);
      setError(null);
    } catch (cause) {
      if (!(cause instanceof ApiError)) setError("Couldn't load your threads.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (loaded.current) return;
    loaded.current = true;
    void refresh();
    void pendingApprovals()
      .then((data) => {
        setPendingCount(data.approvals.length);
        setPendingThreadId(data.approvals[0]?.thread_id ?? null);
      })
      .catch(() => setPendingCount(0));
    void listDevices()
      .then((data) => {
        const current = data.devices.find((device) => device.current);
        setDeviceLabel(current?.label ?? null);
      })
      .catch(() => setDeviceLabel(null));
  }, [refresh]);

  const newChat = async () => {
    if (creating) return;
    setCreating(true);
    setError(null);
    try {
      const data = await createThread();
      onOpenThread(data.thread.id);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Couldn't start a new chat.");
      setCreating(false);
    }
  };

  const confirmRename = async () => {
    if (!renaming) return;
    const title = renaming.title.trim();
    if (!title) return;
    try {
      await renameThread(renaming.id, title);
      setRenaming(null);
      await refresh();
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Couldn't rename that.");
    }
  };

  const confirmDelete = async () => {
    if (!deleting) return;
    try {
      await deleteThread(deleting.id);
      setDeleting(null);
      setMenuThreadId(null);
      await refresh();
    } catch (cause) {
      setDeleting(null);
      if (cause instanceof ApiError && cause.code === "turn_in_progress") {
        setError("Still answering in that thread. Wait or press Stop, then delete.");
      } else {
        setError(cause instanceof ApiError ? cause.message : "Couldn't delete that.");
      }
    }
  };

  return (
    <main className="screen threads-screen">
      <header className="top-bar">
        <div className="top-bar-title">
          <h1>Threads</h1>
          {deviceLabel && <span className="device-chip">{deviceLabel}</span>}
        </div>
        <button
          type="button"
          className="icon-btn"
          aria-label="Settings"
          onClick={onOpenSettings}
        >
          <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
            <path
              d="M12 15.5A3.5 3.5 0 1 0 12 8.5a3.5 3.5 0 0 0 0 7Zm7.4-2.6.1-.9-.1-.9 1.8-1.4-1.5-2.6-2.1.7a7 7 0 0 0-1.6-.9L15.6 4h-3l-.4 2.3a7 7 0 0 0-1.6.9l-2.1-.7L7 9.1l1.8 1.4-.1.9.1.9L7 13.7l1.5 2.6 2.1-.7c.5.4 1 .7 1.6.9l.4 2.3h3l.4-2.3c.6-.2 1.1-.5 1.6-.9l2.1.7 1.5-2.6-1.8-1.4Z"
              fill="currentColor"
            />
          </svg>
        </button>
      </header>

      {pendingCount > 0 && (
        <a
          className="approval-badge"
          href="#/"
          onClick={(event) => {
            event.preventDefault();
            if (pendingThreadId) onOpenThread(pendingThreadId);
          }}
        >
          <span className="approval-badge-dot" aria-hidden="true" />
          {pendingCount} {pendingCount === 1 ? "approval" : "approvals"} waiting on you
        </a>
      )}

      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}

      {loading ? (
        <p className="empty-note">Loading…</p>
      ) : threads.length === 0 ? (
        <div className="empty-state">
          <p>
            No conversations yet. Ask about a document, the web, or your repo — anything that
            changes something waits for your approval.
          </p>
        </div>
      ) : (
        <ul className="thread-list">
          {threads.map((thread) => (
            <li key={thread.id} className="thread-item">
              {renaming?.id === thread.id ? (
                <div className="thread-rename">
                  <input
                    autoFocus
                    value={renaming.title}
                    maxLength={80}
                    onChange={(event) => setRenaming({ id: thread.id, title: event.target.value })}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") void confirmRename();
                      if (event.key === "Escape") setRenaming(null);
                    }}
                    aria-label="Thread title"
                  />
                  <button type="button" className="btn btn-small" onClick={() => void confirmRename()}>
                    Save
                  </button>
                  <button type="button" className="btn btn-small btn-ghost" onClick={() => setRenaming(null)}>
                    Cancel
                  </button>
                </div>
              ) : (
                <>
                  <button
                    type="button"
                    className="thread-row"
                    onClick={() => onOpenThread(thread.id)}
                  >
                    <span className="thread-title">
                      {thread.title}
                      {thread.active_turn_id && <span className="working-dot" aria-label="Working" />}
                    </span>
                    {thread.last_message_preview && (
                      <span className="thread-preview">{thread.last_message_preview}</span>
                    )}
                    <span className="thread-meta">{relativeTime(thread.updated_at)}</span>
                  </button>
                  <button
                    type="button"
                    className="icon-btn thread-menu-btn"
                    aria-label={`Actions for ${thread.title}`}
                    aria-expanded={menuThreadId === thread.id}
                    onClick={() => setMenuThreadId(menuThreadId === thread.id ? null : thread.id)}
                  >
                    ⋯
                  </button>
                  {menuThreadId === thread.id && (
                    <div className="thread-menu" role="menu">
                      <button
                        type="button"
                        role="menuitem"
                        onClick={() => {
                          setRenaming({ id: thread.id, title: thread.title });
                          setMenuThreadId(null);
                        }}
                      >
                        Rename
                      </button>
                      <button
                        type="button"
                        role="menuitem"
                        className="danger"
                        onClick={() => {
                          setDeleting(thread);
                          setMenuThreadId(null);
                        }}
                      >
                        Delete
                      </button>
                    </div>
                  )}
                </>
              )}
            </li>
          ))}
        </ul>
      )}

      <div className="fab-slot">
        <button type="button" className="btn btn-primary fab" onClick={() => void newChat()} disabled={creating}>
          {creating ? "Starting…" : "New chat"}
        </button>
      </div>

      {deleting && (
        <div
          className="modal-backdrop"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setDeleting(null);
          }}
        >
          <div className="modal" role="dialog" aria-modal="true" aria-labelledby="delete-title">
            <h2 id="delete-title">Delete “{deleting.title}”?</h2>
            <p>This permanently removes the conversation and everything in it.</p>
            <div className="modal-actions">
              <button type="button" className="btn btn-ghost" onClick={() => setDeleting(null)}>
                Cancel
              </button>
              <button type="button" className="btn btn-danger" onClick={() => void confirmDelete()}>
                Delete
              </button>
            </div>
          </div>
        </div>
      )}
    </main>
  );
}

function relativeTime(iso: string): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (seconds < 60) return "now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.round(hours / 24);
  return `${days}d`;
}

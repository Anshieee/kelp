/*
 * Thread list data + presentation, shared by the Threads screen and the
 * conversation drawer. Behavior is unchanged from the original Threads
 * screen: snapshot from the in-memory cache, refresh from the server,
 * create/rename/delete, and the pending-approvals badge.
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
import { cacheThreads, getCachedThreads, getSession, type ThreadSummary } from "../state";

export interface ThreadList {
  threads: ThreadSummary[];
  loading: boolean;
  error: string | null;
  creating: boolean;
  pendingCount: number;
  pendingThreadId: string | null;
  deviceLabel: string | null;
  menuThreadId: string | null;
  renaming: { id: string; title: string } | null;
  deleting: ThreadSummary | null;
  newChat: () => void;
  confirmRename: () => void;
  confirmDelete: () => void;
  setMenuThreadId: (id: string | null) => void;
  setRenaming: (renaming: { id: string; title: string } | null) => void;
  setDeleting: (thread: ThreadSummary | null) => void;
}

export function useThreadsList(onOpenThread: (threadId: string) => void): ThreadList {
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

  const newChat = useCallback(() => {
    if (creating) return;
    setCreating(true);
    setError(null);
    void createThread()
      .then((data) => {
        onOpenThread(data.thread.id);
      })
      .catch((cause: unknown) => {
        setError(cause instanceof ApiError ? cause.message : "Couldn't start a new chat.");
        setCreating(false);
      });
  }, [creating, onOpenThread]);

  const confirmRename = useCallback(async () => {
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
  }, [renaming, refresh]);

  const confirmDelete = useCallback(async () => {
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
  }, [deleting, refresh]);

  return {
    threads,
    loading,
    error,
    creating,
    pendingCount,
    pendingThreadId,
    deviceLabel,
    menuThreadId,
    renaming,
    deleting,
    newChat,
    confirmRename,
    confirmDelete,
    setMenuThreadId,
    setRenaming,
    setDeleting,
  };
}

/** Approvals badge + New chat + thread list with rename/delete. No data fetching. */
export function ThreadListView({
  list,
  onOpenThread,
}: {
  list: ThreadList;
  onOpenThread: (threadId: string) => void;
}) {
  return (
    <>
      <button type="button" className="new-chat" onClick={list.newChat} disabled={list.creating}>
        <span aria-hidden="true">+</span> {list.creating ? "Starting…" : "New chat"}
      </button>

      {list.pendingCount > 0 && (
        <a
          className="approval-badge"
          href="#/"
          onClick={(event) => {
            event.preventDefault();
            if (list.pendingThreadId) onOpenThread(list.pendingThreadId);
          }}
        >
          <span className="approval-badge-dot" aria-hidden="true" />
          {list.pendingCount} {list.pendingCount === 1 ? "approval" : "approvals"} waiting on you
        </a>
      )}

      {list.error && (
        <p className="form-error" role="alert">
          {list.error}
        </p>
      )}

      {list.loading ? (
        <p className="empty-note">Loading…</p>
      ) : list.threads.length === 0 ? (
        <div className="empty-state">
          <p>
            No conversations yet. Ask about a document, the web, or your repo — anything that
            changes something waits for your approval.
          </p>
        </div>
      ) : (
        <ul className="thread-list">
          {list.threads.map((thread) => (
            <li key={thread.id} className="thread-item">
              {list.renaming?.id === thread.id ? (
                <div className="thread-rename">
                  <input
                    autoFocus
                    value={list.renaming.title}
                    maxLength={80}
                    onChange={(event) => list.setRenaming({ id: thread.id, title: event.target.value })}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") void list.confirmRename();
                      if (event.key === "Escape") list.setRenaming(null);
                    }}
                    aria-label="Thread title"
                  />
                  <button type="button" className="btn btn-small" onClick={() => void list.confirmRename()}>
                    Save
                  </button>
                  <button type="button" className="btn btn-small btn-ghost" onClick={() => list.setRenaming(null)}>
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
                    aria-expanded={list.menuThreadId === thread.id}
                    onClick={() =>
                      list.setMenuThreadId(list.menuThreadId === thread.id ? null : thread.id)
                    }
                  >
                    ⋯
                  </button>
                  {list.menuThreadId === thread.id && (
                    <div className="thread-menu" role="menu">
                      <button
                        type="button"
                        role="menuitem"
                        onClick={() => {
                          list.setRenaming({ id: thread.id, title: thread.title });
                          list.setMenuThreadId(null);
                        }}
                      >
                        Rename
                      </button>
                      <button
                        type="button"
                        role="menuitem"
                        className="danger"
                        onClick={() => {
                          list.setDeleting(thread);
                          list.setMenuThreadId(null);
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

      {list.deleting && (
        <div
          className="modal-backdrop"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) list.setDeleting(null);
          }}
        >
          <div className="modal" role="dialog" aria-modal="true" aria-labelledby="delete-title">
            <h2 id="delete-title">Delete “{list.deleting.title}”?</h2>
            <p>This permanently removes the conversation and everything in it.</p>
            <div className="modal-actions">
              <button type="button" className="btn btn-ghost" onClick={() => list.setDeleting(null)}>
                Cancel
              </button>
              <button type="button" className="btn btn-danger" onClick={() => void list.confirmDelete()}>
                Delete
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

/** Account chip for the drawer bottom (netlify sidebar pattern). */
export function AccountChip({ label }: { label: string | null }) {
  const session = getSession();
  const name = label ?? session?.device.label ?? "This device";
  return (
    <div className="account-row">
      <span className="account-avatar" aria-hidden="true">
        {name.slice(0, 1).toUpperCase() || "K"}
      </span>
      <span>
        {name}
        <small>Private · approval required</small>
      </span>
    </div>
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

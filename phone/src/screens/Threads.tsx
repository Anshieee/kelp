/*
 * Threads list: recent conversations in a slide-over drawer (netlify/mobile
 * sidebar pattern), plus a compact home panel. Data behavior is unchanged —
 * see ThreadList for the cache/refresh/create/rename/delete logic.
 */
import { useState } from "react";
import ConversationDrawer from "../components/ConversationDrawer";
import ThemeToggle from "../components/ThemeToggle";
import { AccountChip, ThreadListView, useThreadsList } from "../components/ThreadList";

interface ThreadsProps {
  onOpenThread: (threadId: string) => void;
  onOpenSettings: () => void;
}

export default function Threads({ onOpenThread, onOpenSettings }: ThreadsProps) {
  const list = useThreadsList(onOpenThread);
  const [drawerOpen, setDrawerOpen] = useState(false);

  return (
    <main className="screen threads-screen">
      <header className="top-bar">
        <button
          type="button"
          className="icon-btn"
          aria-label="Open conversations"
          aria-expanded={drawerOpen}
          aria-controls="conv-drawer"
          onClick={() => setDrawerOpen(true)}
        >
          <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
            <path d="M4 7h16M4 12h16M4 17h16" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
          </svg>
        </button>
        <div className="top-bar-title">
          <span className="kelp-avatar small" aria-hidden="true">
            <i />
            <i />
          </span>
          <h1>Kelp</h1>
          {list.deviceLabel && <span className="device-chip">{list.deviceLabel}</span>}
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

      <ConversationDrawer open={drawerOpen} onClose={() => setDrawerOpen(false)}>
        <div className="drawer-scroll">
          <ThreadListView list={list} onOpenThread={onOpenThread} />
        </div>
        <div className="drawer-bottom">
          <ThemeToggle />
          <AccountChip label={list.deviceLabel} />
        </div>
      </ConversationDrawer>

      <div className="threads-home">
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
        <div className="empty-state">
          <span className="kelp-avatar hero-avatar" aria-hidden="true">
            <i />
            <i />
          </span>
          <p>
            {list.loading
              ? "Loading…"
              : list.threads.length === 0
                ? "No conversations yet. Ask about a document, the web, or your repo — anything that changes something waits for your approval."
                : "Pick up a conversation to keep going, or start something new."}
          </p>
          {!list.loading && (
            <div className="threads-home-actions">
              <button
                type="button"
                className="btn btn-primary"
                onClick={list.newChat}
                disabled={list.creating}
              >
                {list.creating ? "Starting…" : "New chat"}
              </button>
              {list.threads.length > 0 && (
                <button type="button" className="btn" onClick={() => setDrawerOpen(true)}>
                  Open conversations
                </button>
              )}
            </div>
          )}
        </div>
      </div>
    </main>
  );
}

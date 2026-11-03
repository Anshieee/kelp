/*
 * Chat screen: history snapshot + live SSE tail, composer, approvals.
 *
 * Snapshot first (GET /api/threads/{id}), then open the stream with
 * after=last_seq — that ordering is the whole non-duplication rule (§10.1).
 * The stream stays open while the thread view is open and closes on navigate.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  decideApproval,
  getThread,
  pendingApprovals,
  retryTurn,
  sendTurn,
  stopTurn,
} from "../api";
import { initialChatState, reduceChat, type ChatState } from "../chatState";
import { ThreadStream, type StreamState } from "../stream";
import ApprovalCard from "../components/ApprovalCard";
import Composer from "../components/Composer";
import MessageList from "../components/MessageList";

interface ChatProps {
  threadId: string;
  onBack: () => void;
}

export default function Chat({ threadId, onBack }: ChatProps) {
  const [state, setState] = useState<ChatState | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [streamState, setStreamState] = useState<StreamState>("connecting");
  const [actionError, setActionError] = useState<string | null>(null);
  const streamRef = useRef<ThreadStream | null>(null);
  const stateRef = useRef<ChatState | null>(null);
  stateRef.current = state;

  useEffect(() => {
    let cancelled = false;
    let stream: ThreadStream | null = null;

    const open = async () => {
      try {
        const detail = await getThread(threadId);
        if (cancelled) return;
        setState(
          initialChatState(
            detail.messages,
            detail.last_seq,
            detail.thread.active_turn_id ?? null,
            detail.thread.title,
          ),
        );
        setLoadError(null);
        stream = new ThreadStream(threadId, {
          onEvent: (event) => {
            const current = stateRef.current;
            if (current) setState(reduceChat(current, event));
          },
          onState: setStreamState,
          onGiveUp: () => setStreamState("reconnecting"),
        });
        streamRef.current = stream;
        stream.open(detail.last_seq);
        // Pick up approvals that predate this visit (e.g. from another device).
        void pendingApprovals(threadId)
          .then((data) => {
            if (cancelled) return;
            setState((current) => {
              if (!current) return current;
              const approvals = new Map(current.approvals);
              for (const approval of data.approvals) {
                if (!approvals.has(approval.id)) {
                  approvals.set(approval.id, {
                    id: approval.id,
                    tool: approval.tool,
                    title: approval.title ?? null,
                    rationale: approval.rationale ?? null,
                    summary: approval.summary ?? null,
                    effects: approval.effects ?? [],
                    expires_at: approval.expires_at,
                    status: approval.status,
                  });
                }
              }
              return { ...current, approvals };
            });
          })
          .catch(() => {});
      } catch (cause) {
        if (cancelled) return;
        setLoadError(
          cause instanceof ApiError
            ? cause.code === "unknown_thread"
              ? "That conversation is gone."
              : cause.message
            : "Couldn't load this conversation.",
        );
      }
    };

    void open();
    return () => {
      cancelled = true;
      stream?.close();
      streamRef.current = null;
    };
  }, [threadId]);

  const send = useCallback(
    async (text: string) => {
      setActionError(null);
      try {
        await sendTurn(threadId, text, crypto.randomUUID());
      } catch (cause) {
        if (cause instanceof ApiError && cause.code === "turn_in_progress") {
          setActionError("Still answering. Wait or press Stop.");
        } else if (cause instanceof ApiError) {
          setActionError(cause.message);
        } else {
          setActionError("Couldn't send. Try again.");
        }
      }
    },
    [threadId],
  );

  const stop = useCallback(async () => {
    const turnId = stateRef.current?.activeTurnId;
    if (!turnId) return;
    try {
      await stopTurn(turnId);
    } catch (cause) {
      // 409 turn_not_running is an idempotent no-op for the UI.
      if (!(cause instanceof ApiError) || (cause.code !== "turn_not_running" && cause.code !== "unknown_turn")) {
        setActionError(cause instanceof ApiError ? cause.message : "Couldn't stop the turn.");
      }
    }
  }, []);

  const retry = useCallback(async () => {
    const failed = stateRef.current?.messages.find(
      (message) => message.role === "assistant" && (message.status === "error" || message.status === "interrupted"),
    );
    if (!failed?.turn_id) return;
    setActionError(null);
    try {
      await retryTurn(failed.turn_id);
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : "Couldn't retry.");
    }
  }, []);

  const decide = useCallback(async (approvalId: string, decision: "approve" | "deny") => {
    try {
      await decideApproval(approvalId, decision);
      // The stream's approval_resolved event removes the card; remove
      // optimistically in case the event is slow.
      setState((current) => {
        if (!current) return current;
        const approvals = new Map(current.approvals);
        approvals.delete(approvalId);
        return { ...current, approvals };
      });
    } catch (cause) {
      if (cause instanceof ApiError) {
        if (cause.code === "expired") {
          setState((current) => {
            if (!current) return current;
            const approvals = new Map(current.approvals);
            approvals.delete(approvalId);
            return { ...current, approvals };
          });
          setActionError("That approval expired. Nothing ran.");
        } else {
          setActionError(cause.message);
        }
      }
    }
  }, []);

  const pendingApprovalsList = useMemo(
    () => (state ? [...state.approvals.values()].filter((approval) => approval.status === "pending") : []),
    [state],
  );

  if (loadError) {
    return (
      <main className="screen chat-screen">
        <ChatHeader title="Conversation" onBack={onBack} />
        <div className="empty-state">
          <p>{loadError}</p>
          <button type="button" className="btn" onClick={onBack}>
            Back to threads
          </button>
        </div>
      </main>
    );
  }

  if (!state) {
    return (
      <main className="screen chat-screen">
        <ChatHeader title="Loading…" onBack={onBack} />
        <p className="empty-note">Loading…</p>
      </main>
    );
  }

  return (
    <main className="screen chat-screen">
      <ChatHeader title={state.threadTitle ?? "Conversation"} onBack={onBack} />
      {streamState !== "live" && (
        <div className="stream-banner" role="status">
          {streamState === "reconnecting" ? "Reconnecting…" : "Connecting…"}
        </div>
      )}
      <MessageList
        messages={state.messages}
        activeTurnId={state.activeTurnId}
        turnError={state.turnError}
        onRetry={() => void retry()}
      />
      {actionError && (
        <p className="form-error composer-error" role="alert">
          {actionError}
        </p>
      )}
      {pendingApprovalsList.length > 0 && (
        <section className="approvals-tray" aria-label="Approvals waiting on you">
          {pendingApprovalsList.map((approval) => (
            <ApprovalCard key={approval.id} approval={approval} onDecide={decide} />
          ))}
        </section>
      )}
      <Composer
        disabled={state.activeTurnId !== null}
        onSend={send}
        onStop={() => void stop()}
      />
    </main>
  );
}

function ChatHeader({ title, onBack }: { title: string; onBack: () => void }) {
  return (
    <header className="top-bar chat-top-bar">
      <button type="button" className="icon-btn" aria-label="Back to threads" onClick={onBack}>
        <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
          <path d="M15.5 4.5 8 12l7.5 7.5" stroke="currentColor" strokeWidth="2" fill="none" strokeLinecap="round" />
        </svg>
      </button>
      <h1 className="chat-title">{title}</h1>
    </header>
  );
}

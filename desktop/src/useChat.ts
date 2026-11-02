import { useCallback, useEffect, useRef, useState } from "react";
import {
  bridge,
  createThread,
  decideApproval,
  getThread,
  listThreads,
  messageText,
  retryTurn,
  sendTurn,
  stopTurn,
  type ApiFailure,
  type ChatEvent,
  type Message,
  type StreamState,
  type Thread,
} from "./bridge";
import { applyEvent } from "./chatEvents";
import { isTauri } from "./tauri";

export type Phase = "loading" | "signed-out" | "ready" | "error";

let counter = 0;
/** Tauri's webview is a secure context, but keep a fallback for a plain tab. */
export function clientTurnId(): string {
  const uuid = globalThis.crypto?.randomUUID?.();
  if (uuid) return `ct-${uuid}`;
  counter += 1;
  return `ct-${Date.now().toString(36)}-${counter}`;
}

export function useChat() {
  const [phase, setPhase] = useState<Phase>("loading");
  const [failure, setFailure] = useState<ApiFailure | null>(null);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [streamState, setStreamState] = useState<string>("closed");
  const [busy, setBusy] = useState(false);
  const [turnId, setTurnId] = useState<string | null>(null);
  const [turnStatus, setTurnStatus] = useState<string | null>(null);

  const activeRef = useRef<string | null>(null);
  const lastSeq = useRef(0);

  const fail = useCallback((error: unknown) => {
    const api = error as ApiFailure;
    if (api?.status === 401) {
      setPhase("signed-out");
      return;
    }
    setFailure(
      api?.message
        ? api
        : { status: 0, code: "unknown", message: "Something went wrong.", retryable: true },
    );
    // A transport failure means the server is unreachable, not that the UI is
    // still loading: show the error instead of a spinner forever.
    setPhase((current) => (current === "ready" ? current : "error"));
  }, []);

  const refreshThreads = useCallback(async (): Promise<boolean> => {
    try {
      setThreads(await listThreads());
      return true;
    } catch (error) {
      fail(error);
      return false;
    }
  }, [fail]);

  const openThread = useCallback(
    async (threadId: string) => {
      if (activeRef.current) void bridge.closeStream(activeRef.current);
      activeRef.current = threadId;
      setActiveId(threadId);
      setMessages([]);
      setBusy(false);
      setTurnId(null);
      setTurnStatus(null);
      lastSeq.current = 0;

      try {
        const snapshot = await getThread(threadId);
        setMessages(snapshot.messages ?? []);
        lastSeq.current = snapshot.last_seq ?? 0;
        await bridge.openStream(threadId, lastSeq.current);
      } catch (error) {
        fail(error);
      }
    },
    [fail],
  );

  const startSession = useCallback(async () => {
    // `fail` has already chosen signed-out or error; only a successful load
    // may move the UI to ready.
    const ok = await refreshThreads();
    if (ok) setPhase("ready");
  }, [refreshThreads]);

  useEffect(() => {
    void (async () => {
      if (!isTauri()) {
        setPhase("error");
        setFailure({
          status: 0,
          code: "no_bridge",
          message: "The desktop shell is not running.",
          retryable: false,
        });
        return;
      }
      await startSession();
    })();
  }, [startSession]);

  // One subscription for the whole session; the active thread decides what is
  // applied, so switching threads never needs a re-subscribe.
  useEffect(() => {
    if (!isTauri()) return;
    let off: (() => void) | undefined;

    const apply = (payload: ChatEvent) => {
      if (payload.threadId !== activeRef.current) return;

      const seq = payload.id ? Number(payload.id) : 0;
      if (seq && seq <= lastSeq.current) return;
      if (seq) lastSeq.current = seq;

      // Blocks (text, tool calls, approvals, artifacts, sources) are projected
      // purely; turn and thread state are this hook's job. A no-op event
      // returns the same array so React skips the render.
      setMessages((current) => applyEvent(current, payload));

      const type = payload.event ?? String(payload.data?.type ?? "");
      const data = payload.data ?? {};

      if (type === "turn_started") {
        setBusy(true);
        setTurnStatus(null);
        setTurnId(String(data.turn_id ?? "") || null);
        return;
      }

      if (type === "turn_finished") {
        setBusy(false);
        setTurnStatus(String(data.message_status ?? data.reason ?? "") || null);
        void refreshThreads();
        return;
      }

      if (type === "thread_updated") {
        void refreshThreads();
        return;
      }

      if (type === "error") {
        setFailure({
          status: 0,
          code: String(data.code ?? "stream_error"),
          message: String(data.message ?? "The turn did not finish."),
          retryable: true,
        });
      }
    };

    void (async () => {
      const { listen } = await import("@tauri-apps/api/event");
      const stopEvent = await listen<ChatEvent>("chat-event", (event) => apply(event.payload));
      const stopState = await listen<StreamState>("chat-stream-state", (event) => {
        setStreamState(event.payload.state);
        if (event.payload.state === "error") {
          setFailure({
            status: event.payload.status ?? 0,
            code: "stream_error",
            message: event.payload.message ?? "The live stream dropped.",
            retryable: true,
          });
        }
      });
      off = () => {
        stopEvent();
        stopState();
      };
    })();

    return () => off?.();
  }, [refreshThreads]);

  const signIn = useCallback(
    async (bearer: string) => {
      setFailure(null);
      try {
        await bridge.signIn(bearer, "Kelp desktop");
        await startSession();
      } catch (error) {
        fail(error);
      }
    },
    [fail, startSession],
  );

  const signOut = useCallback(async () => {
    if (activeRef.current) void bridge.closeStream(activeRef.current);
    activeRef.current = null;
    setActiveId(null);
    setMessages([]);
    setThreads([]);
    try {
      await bridge.signOut();
    } catch {
      /* signing out locally is enough for the UI */
    }
    setPhase("signed-out");
  }, []);

  const newChat = useCallback(async () => {
    try {
      const { thread } = await createThread();
      await refreshThreads();
      await openThread(thread.id);
    } catch (error) {
      fail(error);
    }
  }, [fail, openThread, refreshThreads]);

  const send = useCallback(
    async (text: string) => {
      const threadId = activeRef.current;
      const trimmed = text.trim();
      if (!threadId || !trimmed) return;
      try {
        await sendTurn(threadId, trimmed, clientTurnId());
      } catch (error) {
        fail(error);
      }
    },
    [fail],
  );

  const stop = useCallback(async () => {
    if (!turnId) return;
    try {
      await stopTurn(turnId);
    } catch (error) {
      fail(error);
    }
  }, [fail, turnId]);

  const retry = useCallback(async () => {
    if (!turnId) return;
    try {
      const { turn_id } = await retryTurn(turnId);
      setTurnId(turn_id);
      setBusy(true);
      setTurnStatus(null);
    } catch (error) {
      fail(error);
    }
  }, [fail, turnId]);

  const decide = useCallback(
    async (approvalId: string, decision: "approve" | "deny", note?: string) => {
      try {
        await decideApproval(approvalId, decision, note);
      } catch (error) {
        fail(error);
      }
    },
    [fail],
  );

  return {
    phase,
    failure,
    threads,
    activeId,
    activeThread: threads.find((thread) => thread.id === activeId) ?? null,
    messages,
    streamState,
    busy,
    turnId,
    turnStatus,
    lastMessageText: (message: Message) => messageText(message),
    startSession,
    signIn,
    signOut,
    newChat,
    openThread,
    send,
    stop,
    retry,
    decide,
    refreshThreads,
  };
}

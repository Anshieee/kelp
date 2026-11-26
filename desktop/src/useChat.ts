import { useCallback, useEffect, useRef, useState } from "react";
import {
  bridge,
  createThread,
  decideApproval,
  getThread,
  listThreads,
  messageText,
  NO_BRIDGE,
  parseBackendMode,
  retryTurn,
  sendTurn,
  stopTurn,
  type ApiFailure,
  type BackendMode,
  type ChatEvent,
  type Message,
  type StreamState,
  type Thread,
} from "./bridge";
import { DEBUG_CARD_SESSION, applyEvent, dismissCardMessage, injectDebugCard, isToolBlock, withSyntheticCards } from "./chatEvents";
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
  // Which backend Rust reports. Null until the explicit lookup answers, and
  // null again if that answer is unusable — never defaulted to hosted.
  const [mode, setMode] = useState<BackendMode | null>(null);
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
  // Mirror of `mode` for callbacks that must not read a stale render. Every
  // chat action checks it before issuing IPC, so a programmatic caller cannot
  // run ahead of the explicit lookup the UI depends on.
  const modeRef = useRef<BackendMode | null>(null);
  // Fabricated computer cards, per thread: scaffolding for the debug trigger
  // until the backend emits `surface: "computer"`. They never reach chat.db,
  // and they are re-injected on return because the transcript is the wrong
  // place for them to survive. Cleared on sign-out and on reload.
  const syntheticRef = useRef<Map<string, Message[]>>(new Map());

  /**
   * Ask Rust which backend this window talks to, before any chat call. The
   * answer drives auth and the Computer viewer; an unknown, missing, or
   * rejected answer is a hard error, never a guess at hosted.
   */
  const lookupMode = useCallback(async (): Promise<BackendMode | null> => {
    // The backend cannot change under a live window. Once Rust has confirmed
    // it, reuse that answer instead of re-asking; a failed lookup (null) still
    // retries on the next explicit Retry.
    if (modeRef.current) return modeRef.current;
    try {
      const resolved = parseBackendMode(await bridge.backendMode());
      modeRef.current = resolved;
      setMode(resolved);
      if (!resolved) {
        setFailure({
          status: 0,
          code: "unknown_backend_mode",
          message: "The desktop bridge reported an unknown backend mode.",
          retryable: true,
        });
        setPhase("error");
      }
      return resolved;
    } catch (error) {
      const api = error as Partial<ApiFailure> | undefined;
      const bridgeMissing = api?.code === NO_BRIDGE.code;
      modeRef.current = null;
      setMode(null);
      // Keep the two failures with a stable meaning. Any other rejected
      // lookup collapses to the fixed no-mode error: echoing a low-level
      // message here could surface a sidecar URL or credential detail.
      setFailure(
        bridgeMissing
          ? { ...NO_BRIDGE }
          : {
              status: 0,
              code: "unknown_backend_mode",
              message: "The desktop bridge did not report a usable backend mode.",
              retryable: true,
            },
      );
      setPhase("error");
      return null;
    }
  }, []);

  const fail = useCallback((error: unknown) => {
    const api = error as ApiFailure;
    // A 401 means "signed out" only on the hosted server, and only after the
    // mode lookup confirmed it. Local and unknown modes never prompt for a
    // bearer, so their 401s surface as an ordinary failure instead.
    if (api?.status === 401 && modeRef.current === "hosted") {
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
    // Retry after a failed/unknown lookup re-asks Rust first: no chat request
    // may run before the mode is known.
    if (!modeRef.current && !(await lookupMode())) return false;
    try {
      setThreads(await listThreads());
      // A refresh that worked retires the failure that the Retry button would
      // have retried; otherwise one flaky startup call leaves a permanent
      // "Something went wrong" banner over a perfectly working session. It
      // also finishes a stalled startup, but it never skips a hosted sign-in:
      // signed-out stays signed-out until the explicit sign-in path restarts
      // the session.
      setFailure(null);
      setPhase((current) => (current === "loading" || current === "error" ? "ready" : current));
      return true;
    } catch (error) {
      fail(error);
      return false;
    }
  }, [fail, lookupMode]);

  const openThread = useCallback(
    async (threadId: string) => {
      // No speculative chat IPC before the mode lookup has answered.
      if (!modeRef.current) return;
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
        setMessages(withSyntheticCards([...(snapshot.messages ?? [])], syntheticRef.current.get(threadId) ?? []));
        setFailure(null);
        lastSeq.current = snapshot.last_seq ?? 0;
        await bridge.openStream(threadId, lastSeq.current);
      } catch (error) {
        fail(error);
      }
    },
    [fail],
  );

  const startSession = useCallback(async (): Promise<boolean> => {
    // Explicit lookup before any chat call (reused once confirmed); `fail` has
    // already chosen signed-out or error, and only a successful load may move
    // the UI to ready.
    if (!(await lookupMode())) return false;
    const ok = await refreshThreads();
    if (ok) setPhase("ready");
    return ok;
  }, [lookupMode, refreshThreads]);

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
    async (bearer: string): Promise<boolean> => {
      // The hosted sign-in endpoint does not exist for a local sidecar, and an
      // unknown mode cannot prove it does. This is the UI half of the guard;
      // Rust refuses the command as well.
      if (modeRef.current !== "hosted") {
        // Local gets the fixed refusal. An unknown mode keeps the lookup
        // failure on screen instead of replacing the root cause with an auth
        // message that implies the backend was fine.
        if (modeRef.current === "local") {
          setFailure({
            status: 0,
            code: "auth_unavailable",
            message: "Sign-in is only available in hosted mode.",
            retryable: false,
          });
        }
        return false;
      }
      setFailure(null);
      try {
        await bridge.signIn(bearer, "Kelp desktop");
        await startSession();
        return true;
      } catch (error) {
        fail(error);
        return false;
      }
    },
    [fail, startSession],
  );

  const signOut = useCallback(async (): Promise<boolean> => {
    // Only the hosted backend has a sign-out endpoint. Bail before touching
    // the active chat or its stream: a direct hook call in Local (or an
    // unknown mode) must not tear down state the UI still shows.
    if (modeRef.current !== "hosted") return false;
    if (activeRef.current) void bridge.closeStream(activeRef.current);
    activeRef.current = null;
    syntheticRef.current.clear();
    setActiveId(null);
    setMessages([]);
    setThreads([]);
    try {
      await bridge.signOut();
    } catch {
      /* signing out locally is enough for the UI */
    }
    setPhase("signed-out");
    return true;
  }, []);

  const newChat = useCallback(async () => {
    // Creating a thread mutates server state, so it may not run on a guess.
    if (!modeRef.current) return;
    try {
      const { thread } = await createThread();
      await refreshThreads();
      await openThread(thread.id);
    } catch (error) {
      fail(error);
    }
  }, [fail, openThread, refreshThreads]);

  /**
   * Debug scaffolding: fabricate a finished computer card in the open thread.
   * The block never reaches the server, so the transcript cannot later claim
   * the agent touched a machine it never touched. Firing it again refreshes the
   * one debug card instead of stacking a tool row per press.
   */
  const injectComputerCard = useCallback(() => {
    // Debug cards exist only on the hosted backend; a Local sidecar has no
    // Computer viewer for them to point at.
    if (modeRef.current !== "hosted") return false;
    const threadId = activeRef.current;
    if (!threadId) return false;
    injectDebugCard(syntheticRef.current, threadId);
    const remembered = syntheticRef.current.get(threadId) ?? [];
    const rememberedIds = new Set(remembered.map((message) => message.id));
    setMessages((current) => [
      ...current.filter(
        (message) =>
          !rememberedIds.has(message.id) &&
          !message.blocks.some(
            (block) => isToolBlock(block) && block.session?.id === DEBUG_CARD_SESSION,
          ),
      ),
      ...remembered,
    ]);
    return true;
  }, []);

  /** Drop a fabricated card from both the view and the remembered list. */
  const dismissComputerCard = useCallback((callId: string) => {
    const threadId = activeRef.current;
    if (!threadId) return;
    const synthetic = syntheticRef.current.get(threadId) ?? [];
    setMessages((current) => dismissCardMessage(current, synthetic, callId));
  }, []);

  const send = useCallback(
    async (text: string) => {
      if (!modeRef.current) return;
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
    if (!modeRef.current || !turnId) return;
    try {
      await stopTurn(turnId);
    } catch (error) {
      fail(error);
    }
  }, [fail, turnId]);

  const retry = useCallback(async () => {
    if (!modeRef.current || !turnId) return;
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
      // Approvals are a hosted workflow; the Local sidecar denies them itself,
      // so deciding one from a stale card must not reach the bridge.
      if (modeRef.current !== "hosted") return;
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
    mode,
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
    injectComputerCard,
    dismissComputerCard,
  };
}

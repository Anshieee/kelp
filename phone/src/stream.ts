/*
 * SSE tail for one thread's event stream (CHAT_SPEC §4, §10.2).
 *
 * Uses native EventSource (GET needs no CSRF; the browser sends its own
 * cookies). The server frames every event with `id: <seq>`, so EventSource's
 * automatic reconnect replays from the right position; we additionally pass
 * `?after=<seq>` on the first open and on manual reconnects, and we drop any
 * event with `seq <= lastSeq` so replay can never duplicate.
 *
 * The parsing / seq-tracking / reconnect-decision logic is pure and exported
 * for unit tests; the class only wires it to EventSource.
 */

export interface StreamEvent {
  seq: number;
  type: string;
  [key: string]: unknown;
}

export type StreamState = "connecting" | "live" | "reconnecting";

/**
 * Every event type the server frames on the thread stream that this client
 * acts on. The server names each frame (`event: <type>`), so a listener must
 * exist per name: the generic `onmessage` handler fires ONLY for unnamed
 * frames (or `event: message`), and without these registrations the app
 * would connect successfully yet never render a single live update. Keep
 * this list in step with the reducer in chatState.ts and the `kind` values
 * in kernel/events.py plus the store-level events the stream carries.
 *
 * Deliberately NOT listened (verified against the server on 2026-11-22):
 * - `usage_updated`, `sources_ready`, `artifact_ready`, `schedule_trigger`
 *   are framed but the v1 reducer ignores them (chatState.ts default case);
 *   registering listeners would change nothing observable.
 * - `cancelled` is never a frame at all — it is a turn *reason* surfaced via
 *   `turn_finished`/message status (see `statusForReason` in chatState.ts).
 */
const SERVER_EVENT_TYPES = [
  "message_created",
  "turn_started",
  "token_delta",
  "tool_call_started",
  "tool_call_finished",
  "approval_requested",
  "approval_resolved",
  "thread_updated",
  "error",
  "turn_finished",
] as const;

/**
 * Incrementally parse an SSE byte stream into events.
 *
 * Feed it accumulated text; it returns every complete event found and the
 * trailing partial frame to keep for the next chunk. Comment lines
 * (`: keep-alive`) and frames without a parseable JSON `data:` payload are
 * skipped. Multi-line `data:` fields are joined with newlines per the SSE spec.
 */
export function parseSseBuffer(buffer: string): { events: StreamEvent[]; rest: string } {
  const events: StreamEvent[] = [];
  // Frames are separated by a blank line; tolerate \r\n.
  const normalized = buffer.replace(/\r\n/g, "\n");
  const frames = normalized.split("\n\n");
  const rest = frames.pop() ?? "";
  for (const frame of frames) {
    const event = parseSseFrame(frame);
    if (event) events.push(event);
  }
  return { events, rest };
}

function parseSseFrame(frame: string): StreamEvent | null {
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith(":")) continue; // comment / heartbeat
    if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
  }
  if (dataLines.length === 0) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(dataLines.join("\n"));
  } catch {
    return null;
  }
  if (!isStreamEvent(parsed)) return null;
  return parsed;
}

function isStreamEvent(value: unknown): value is StreamEvent {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Record<string, unknown>;
  return typeof record.seq === "number" && typeof record.type === "string";
}

/** True when an event was already applied (replay overlap on reconnect). */
export function isDuplicate(event: StreamEvent, lastSeq: number | null): boolean {
  return lastSeq !== null && event.seq <= lastSeq;
}

/** The `after=` cursor to use next: the highest seq seen so far. */
export function nextAfter(lastSeq: number | null, event: StreamEvent): number {
  return lastSeq === null ? event.seq : Math.max(lastSeq, event.seq);
}

/** Reconnect backoff: 1s, 2s, 4s, ... capped at 15s. */
export function reconnectDelayMs(attempt: number): number {
  return Math.min(1000 * 2 ** Math.max(0, attempt), 15_000);
}

export interface ThreadStreamCallbacks {
  onEvent: (event: StreamEvent) => void;
  onState?: (state: StreamState) => void;
  /** Called after repeated failures that never opened, e.g. auth expiry. */
  onGiveUp?: () => void;
}

const MAX_INITIAL_FAILURES = 4;

export class ThreadStream {
  private eventSource: EventSource | null = null;
  private lastSeq: number | null = null;
  private closed = false;
  private failedOpens = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(
    private readonly threadId: string,
    private readonly callbacks: ThreadStreamCallbacks,
  ) {}

  /** Open the stream, replaying everything after `after` (the snapshot's last_seq). */
  open(after: number | null): void {
    this.lastSeq = after;
    this.connect(after, 0);
  }

  close(): void {
    this.closed = true;
    if (this.reconnectTimer !== null) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.eventSource?.close();
    this.eventSource = null;
  }

  private connect(after: number | null, attempt: number): void {
    if (this.closed) return;
    this.callbacks.onState?.(attempt === 0 ? "connecting" : "reconnecting");
    const url =
      `/api/threads/${encodeURIComponent(this.threadId)}/stream` +
      (after !== null ? `?after=${after}` : "");
    const source = new EventSource(url);
    this.eventSource = source;

    source.onopen = () => {
      this.failedOpens = 0;
      this.callbacks.onState?.("live");
    };

    // NOTE: `onmessage` alone is NOT enough. The server names every frame
    // (`event: token_delta`, ...), and named events never reach `onmessage`.
    // Each known type gets an explicit listener below; `onmessage` stays as
    // the fallback for unnamed frames.
    const handleData = (data: string) => {
      const { events } = parseSseBuffer(`data: ${data}\n\n`);
      for (const event of events) {
        if (isDuplicate(event, this.lastSeq)) continue;
        this.lastSeq = nextAfter(this.lastSeq, event);
        this.callbacks.onEvent(event);
      }
    };
    for (const type of SERVER_EVENT_TYPES) {
      source.addEventListener(type, (message: Event) => {
        handleData((message as MessageEvent<string>).data ?? "");
      });
    }
    source.onmessage = (message: MessageEvent<string>) => {
      handleData(message.data);
    };

    source.onerror = (event: Event) => {
      if (this.closed) return;
      // A server-sent `event: error` frame (a failed turn, a refused tool
      // call) ALSO dispatches here, because it shares the name with the DOM
      // error event — but it arrives carrying data, on a perfectly healthy
      // connection. Only a dataless error means the transport itself failed.
      // Duck-typed (`in`), not instanceof: MessageEvent identity does not
      // survive realm boundaries, and the unit fakes are plain objects.
      if (event !== null && typeof event === "object" && "data" in event) return;
      // While CONNECTING the browser retries on its own (it also replays with
      // Last-Event-ID). Once it gives up (CLOSED), we reconnect ourselves with
      // an explicit after= cursor and backoff.
      if (source.readyState === EventSource.CLOSED) {
        this.eventSource = null;
        this.failedOpens += 1;
        if (this.failedOpens > MAX_INITIAL_FAILURES) {
          this.callbacks.onGiveUp?.();
          return;
        }
        const delay = reconnectDelayMs(attempt);
        this.reconnectTimer = setTimeout(() => this.connect(this.lastSeq, attempt + 1), delay);
      } else {
        this.callbacks.onState?.("reconnecting");
      }
    };
  }
}

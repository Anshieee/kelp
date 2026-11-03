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

    source.onmessage = (message: MessageEvent<string>) => {
      const { events } = parseSseBuffer(`data: ${message.data}\n\n`);
      for (const event of events) {
        if (isDuplicate(event, this.lastSeq)) continue;
        this.lastSeq = nextAfter(this.lastSeq, event);
        this.callbacks.onEvent(event);
      }
    };

    source.onerror = () => {
      if (this.closed) return;
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

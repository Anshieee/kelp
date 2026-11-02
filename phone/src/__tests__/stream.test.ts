import { afterEach, describe, expect, it, vi } from "vitest";
import {
  isDuplicate,
  nextAfter,
  parseSseBuffer,
  reconnectDelayMs,
  ThreadStream,
  type StreamEvent,
} from "../stream";

function ev(seq: number, type: string, extra: Record<string, unknown> = {}): StreamEvent {
  return { seq, type, ...extra };
}

describe("SSE parsing", () => {
  it("parses complete frames and keeps the trailing partial frame", () => {
    const chunk =
      'data: {"seq":1,"type":"message_created"}\n\n' + 'data: {"seq":2,"type":"token_delta","tex';
    const { events, rest } = parseSseBuffer(chunk);
    expect(events).toEqual([ev(1, "message_created")]);
    expect(rest).toBe('data: {"seq":2,"type":"token_delta","tex');
  });

  it("completes an event split across chunks", () => {
    const first = parseSseBuffer('data: {"seq":2,"type":"token_delta","tex');
    expect(first.events).toEqual([]);
    const second = parseSseBuffer(first.rest + 't":"The current"}\n\n');
    expect(second.events).toEqual([ev(2, "token_delta", { text: "The current" })]);
    expect(second.rest).toBe("");
  });

  it("ignores heartbeat comments and frames without data", () => {
    const chunk = ": keep-alive\n\n\n\n" + 'data: {"seq":3,"type":"turn_started"}\n\n';
    const { events } = parseSseBuffer(chunk);
    expect(events).toEqual([ev(3, "turn_started")]);
  });

  it("joins multi-line data fields and skips invalid JSON", () => {
    const chunk =
      'data: {"seq":4,\n' + 'data: "type":"usage_updated"}\n\n' + "data: not-json{{\n\n";
    const { events } = parseSseBuffer(chunk);
    expect(events).toEqual([ev(4, "usage_updated")]);
  });

  it("tolerates CRLF line endings", () => {
    const chunk = 'data: {"seq":5,"type":"error"}\r\n\r\n';
    const { events } = parseSseBuffer(chunk);
    expect(events).toEqual([ev(5, "error")]);
  });

  it("rejects payloads without a numeric seq or string type", () => {
    const chunk =
      'data: {"type":"no_seq"}\n\n' + 'data: {"seq":"7","type":"bad"}\n\n' + 'data: {"seq":8,"type":"ok"}\n\n';
    const { events } = parseSseBuffer(chunk);
    expect(events).toEqual([ev(8, "ok")]);
  });
});

describe("seq tracking", () => {
  it("drops duplicates at or below the last applied seq", () => {
    expect(isDuplicate(ev(5, "token_delta"), 5)).toBe(true);
    expect(isDuplicate(ev(4, "token_delta"), 5)).toBe(true);
    expect(isDuplicate(ev(6, "token_delta"), 5)).toBe(false);
    expect(isDuplicate(ev(1, "token_delta"), null)).toBe(false);
  });

  it("advances the cursor to the max seq seen", () => {
    let last: number | null = null;
    last = nextAfter(last, ev(3, "a"));
    last = nextAfter(last, ev(9, "b"));
    last = nextAfter(last, ev(7, "c"));
    expect(last).toBe(9);
  });
});

describe("reconnect policy", () => {
  it("backs off exponentially and caps at 15s", () => {
    expect(reconnectDelayMs(0)).toBe(1000);
    expect(reconnectDelayMs(1)).toBe(2000);
    expect(reconnectDelayMs(2)).toBe(4000);
    expect(reconnectDelayMs(5)).toBe(15000);
    expect(reconnectDelayMs(50)).toBe(15000);
  });

  it("reconnects with after= set to the last seq seen, then gives up", () => {
    vi.useFakeTimers();
    const seen: Array<number | null> = [];
    const fakeSources: Array<{
      readyState: number;
      onopen: (() => void) | null;
      onmessage: ((event: { data: string }) => void) | null;
      onerror: (() => void) | null;
      close: () => void;
      fail: () => void;
    }> = [];
    let factoryCalls = 0;

    class FakeEventSource {
      static CLOSED = 2;
      readyState = 0;
      onopen: (() => void) | null = null;
      onmessage: ((event: { data: string }) => void) | null = null;
      onerror: (() => void) | null = null;
      constructor(public url: string) {
        factoryCalls += 1;
        seen.push(
          url.includes("after=") ? Number(new URLSearchParams(url.split("?")[1]).get("after")) : null,
        );
        fakeSources.push(this);
      }
      close() {}
      fail() {
        this.readyState = FakeEventSource.CLOSED;
        this.onerror?.();
      }
    }

    vi.stubGlobal("EventSource", FakeEventSource);
    try {
      const stream = new ThreadStream("ses_x", { onEvent: () => {} });
      stream.open(10);
      expect(seen[0]).toBe(10);
      // One event applied, then repeated hard failures.
      fakeSources[0].onmessage?.({ data: JSON.stringify(ev(11, "token_delta")) });
      for (let i = 0; i < 6; i++) {
        fakeSources[fakeSources.length - 1].fail();
        vi.advanceTimersByTime(16_000);
      }
      // 1 initial + 4 reconnect attempts, then give-up: no further connects.
      expect(factoryCalls).toBe(5);
      expect(seen[1]).toBe(11);
      expect(seen[seen.length - 1]).toBe(11);
      stream.close();
    } finally {
      vi.unstubAllGlobals();
      vi.useRealTimers();
    }
  });

  it("never applies a replayed event twice across a manual reconnect", () => {
    vi.useFakeTimers();
    const applied: StreamEvent[] = [];
    const sources: Array<{
      onmessage: ((event: { data: string }) => void) | null;
      onerror: (() => void) | null;
      fail: () => void;
    }> = [];

    class FakeEventSource {
      static CLOSED = 2;
      readyState = 0;
      onopen: (() => void) | null = null;
      onmessage: ((event: { data: string }) => void) | null = null;
      onerror: (() => void) | null = null;
      constructor() {
        sources.push(this);
      }
      close() {}
      fail() {
        this.readyState = FakeEventSource.CLOSED;
        this.onerror?.();
      }
    }

    vi.stubGlobal("EventSource", FakeEventSource);
    try {
      const stream = new ThreadStream("ses_y", { onEvent: (event) => applied.push(event) });
      stream.open(null);
      // Server replays seq 1-2, then the connection drops and replays 2-3.
      sources[0].onmessage?.({ data: JSON.stringify(ev(1, "token_delta")) });
      sources[0].onmessage?.({ data: JSON.stringify(ev(2, "token_delta")) });
      sources[0].fail();
      vi.advanceTimersByTime(16_000);
      sources[1].onmessage?.({ data: JSON.stringify(ev(2, "token_delta")) });
      sources[1].onmessage?.({ data: JSON.stringify(ev(3, "token_delta")) });
      expect(applied.map((event) => event.seq)).toEqual([1, 2, 3]);
      stream.close();
    } finally {
      vi.unstubAllGlobals();
      vi.useRealTimers();
    }
  });
});

afterEach(() => {
  vi.useRealTimers();
});

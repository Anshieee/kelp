import { describe, expect, it } from "vitest";
import { initialChatState, reduceChat, type ChatState } from "../chatState";
import type { Message } from "../api";
import type { StreamEvent } from "../stream";

function ev(seq: number, type: string, extra: Record<string, unknown> = {}): StreamEvent {
  return { seq, type, ...extra };
}

function userMessage(): Message {
  return {
    id: "msg_u1",
    thread_id: "ses_1",
    role: "user",
    turn_id: "turn_1",
    status: "complete",
    created_at: "2026-11-03T00:00:00Z",
    blocks: [{ type: "text", text: "hello" }],
  };
}

function assistantMessage(): Message {
  return {
    id: "msg_a1",
    thread_id: "ses_1",
    role: "assistant",
    turn_id: "turn_1",
    status: "streaming",
    created_at: "2026-11-03T00:00:01Z",
    blocks: [],
  };
}

function stateWith(messages: Message[]): ChatState {
  return initialChatState(messages, 0, null, "Test");
}

describe("chat state reducer", () => {
  it("appends token deltas to the last text block of the message", () => {
    let state = stateWith([userMessage(), assistantMessage()]);
    state = reduceChat(state, ev(1, "message_created", { message: assistantMessage() }));
    state = reduceChat(state, ev(2, "token_delta", { message_id: "msg_a1", text: "The current" }));
    state = reduceChat(state, ev(3, "token_delta", { message_id: "msg_a1", text: " price is" }));
    const assistant = state.messages.find((message) => message.id === "msg_a1");
    expect(assistant?.blocks).toEqual([{ type: "text", text: "The current price is" }]);
    expect(state.lastSeq).toBe(3);
  });

  it("closes the text block on tool_call_started and opens a new one after", () => {
    let state = stateWith([assistantMessage()]);
    state = reduceChat(state, ev(1, "token_delta", { message_id: "msg_a1", text: "Let me look. " }));
    state = reduceChat(
      state,
      ev(2, "tool_call_started", {
        message_id: "msg_a1",
        call_id: "call_1",
        tool: "web.search",
        label: "Web search",
        summary: "GPU pricing",
        effects: ["network"],
      }),
    );
    state = reduceChat(state, ev(3, "token_delta", { message_id: "msg_a1", text: "Found it." }));
    const assistant = state.messages[0];
    expect(assistant.blocks.map((block) => block.type)).toEqual(["text", "tool_call", "text"]);
    expect(assistant.blocks[1]).toMatchObject({ call_id: "call_1", status: "running" });
    expect(assistant.blocks[2]).toEqual({ type: "text", text: "Found it." });
  });

  it("marks a tool call done on tool_call_finished", () => {
    let state = stateWith([assistantMessage()]);
    state = reduceChat(
      state,
      ev(1, "tool_call_started", { message_id: "msg_a1", call_id: "call_1", tool: "web.search" }),
    );
    state = reduceChat(
      state,
      ev(2, "tool_call_finished", { call_id: "call_1", ok: true, duration_ms: 412 }),
    );
    const block = state.messages[0].blocks[0];
    expect(block).toMatchObject({ status: "done", duration_ms: 412 });
  });

  it("adds and removes pending approvals", () => {
    let state = stateWith([]);
    state = reduceChat(
      state,
      ev(1, "approval_requested", {
        approval: {
          id: "apr_1",
          tool: "github.write",
          title: "Create a pull request",
          rationale: "The assistant wants to change something.",
          effects: ["write"],
          expires_at: "2026-11-03T00:10:00Z",
          status: "pending",
        },
      }),
    );
    expect(state.approvals.get("apr_1")?.tool).toBe("github.write");
    state = reduceChat(state, ev(2, "approval_resolved", { approval_id: "apr_1" }));
    expect(state.approvals.size).toBe(0);
  });

  it("sets the assistant message status from turn_finished and clears the active turn", () => {
    let state = stateWith([assistantMessage()]);
    state = reduceChat(state, ev(1, "turn_started", { turn_id: "turn_1" }));
    expect(state.activeTurnId).toBe("turn_1");
    state = reduceChat(state, ev(2, "turn_finished", { turn_id: "turn_1", reason: "stop", message_status: "complete" }));
    expect(state.messages[0].status).toBe("complete");
    expect(state.activeTurnId).toBeNull();
  });

  it("maps turn_finished reasons to statuses", () => {
    let state = stateWith([assistantMessage()]);
    state = reduceChat(state, ev(1, "turn_finished", { turn_id: "turn_1", reason: "cancelled" }));
    expect(state.messages[0].status).toBe("stopped");
    state = stateWith([assistantMessage()]);
    state = reduceChat(state, ev(1, "turn_finished", { turn_id: "turn_1", reason: "interrupted" }));
    expect(state.messages[0].status).toBe("interrupted");
  });

  it("records turn errors and ignores unknown event types without losing seq", () => {
    let state = stateWith([]);
    state = reduceChat(state, ev(1, "error", { code: "provider_timeout", message: "Too long.", retryable: true }));
    expect(state.turnError?.code).toBe("provider_timeout");
    state = reduceChat(state, ev(2, "usage_updated", { input_tokens: 10 }));
    expect(state.lastSeq).toBe(2);
    expect(state.turnError?.code).toBe("provider_timeout");
  });

  it("ignores a duplicate message_created", () => {
    let state = stateWith([userMessage()]);
    state = reduceChat(state, ev(1, "message_created", { message: userMessage() }));
    expect(state.messages.length).toBe(1);
  });
});

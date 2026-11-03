/*
 * Client-side projection of a thread: the history snapshot from
 * GET /api/threads/{id} plus live stream events applied on top.
 *
 * Rules come from CHAT_SPEC §3.3 / §4.4:
 *  - `token_delta` appends to the LAST text block of its message.
 *  - `tool_call_started` closes the current text block and opens a tool block;
 *    the next `token_delta` opens a new text block after the card.
 *  - Everything else opens or updates a block/card by id; replayed events with
 *    `seq <= lastSeq` never reach this reducer (the stream layer drops them).
 *  - Model text is data, never markup: it is rendered as plain text only.
 */
import type { Message, MessageBlock, ToolCallBlock } from "./api";
import type { StreamEvent } from "./stream";

export interface ChatState {
  messages: Message[];
  lastSeq: number | null;
  /** Turn id while a turn is running (drives the composer's disabled state). */
  activeTurnId: string | null;
  /** Latest turn-level error, shown as a banner with a retry affordance. */
  turnError: { code: string; message: string; retryable: boolean; turnId: string | null } | null;
  /** Pending approvals for this thread, keyed by approval id. */
  approvals: Map<string, Approval>;
  threadTitle: string | null;
}

export interface Approval {
  id: string;
  tool: string;
  title: string | null;
  rationale: string | null;
  summary: string | null;
  effects: string[];
  expires_at: string;
  status: string;
}

export function initialChatState(
  messages: Message[],
  lastSeq: number,
  activeTurnId: string | null,
  threadTitle: string | null,
): ChatState {
  return {
    messages,
    lastSeq,
    activeTurnId,
    turnError: null,
    approvals: new Map(),
    threadTitle,
  };
}

export function reduceChat(state: ChatState, event: StreamEvent): ChatState {
  const lastSeq = state.lastSeq === null ? event.seq : Math.max(state.lastSeq, event.seq);
  switch (event.type) {
    case "message_created":
      return reduceMessageCreated(state, event, lastSeq);
    case "turn_started":
      return { ...state, lastSeq, activeTurnId: str(event.turn_id) ?? state.activeTurnId };
    case "token_delta":
      return reduceTokenDelta(state, event, lastSeq);
    case "tool_call_started":
      return reduceToolCallStarted(state, event, lastSeq);
    case "tool_call_finished":
      return reduceToolCallFinished(state, event, lastSeq);
    case "approval_requested":
      return reduceApprovalRequested(state, event, lastSeq);
    case "approval_resolved":
      return reduceApprovalResolved(state, event, lastSeq);
    case "error":
      return {
        ...state,
        lastSeq,
        turnError: {
          code: str(event.code) ?? "unknown_error",
          message: str(event.message) ?? "Something went wrong.",
          retryable: event.retryable === true,
          turnId: str(event.turn_id),
        },
      };
    case "turn_finished": {
      const reason = str(event.reason);
      const turnId = str(event.turn_id);
      const messages = state.messages.map((message) => {
        if (message.turn_id !== turnId || message.role !== "assistant") return message;
        const status = str(event.message_status) ?? statusForReason(reason);
        return { ...message, status };
      });
      return {
        ...state,
        lastSeq,
        messages,
        activeTurnId: state.activeTurnId === turnId ? null : state.activeTurnId,
      };
    }
    case "thread_updated":
      return { ...state, lastSeq, threadTitle: str(event.title) ?? state.threadTitle };
    default:
      // usage_updated, sources_ready, artifact_ready, schedule_trigger,
      // heartbeats and future event types are ignored in v1.
      return { ...state, lastSeq };
  }
}

function reduceMessageCreated(state: ChatState, event: StreamEvent, lastSeq: number): ChatState {
  const message = event.message as Message | undefined;
  if (!message?.id) return { ...state, lastSeq };
  if (state.messages.some((existing) => existing.id === message.id)) return { ...state, lastSeq };
  return { ...state, lastSeq, messages: [...state.messages, message] };
}

function reduceTokenDelta(state: ChatState, event: StreamEvent, lastSeq: number): ChatState {
  const messageId = str(event.message_id);
  const text = str(event.text);
  if (!messageId || !text) return { ...state, lastSeq };
  const messages = state.messages.map((message) => {
    if (message.id !== messageId) return message;
    const blocks = appendToLastTextBlock(message.blocks, text);
    return { ...message, blocks, status: "streaming" };
  });
  return { ...state, lastSeq, messages };
}

function appendToLastTextBlock(blocks: MessageBlock[], text: string): MessageBlock[] {
  const last = blocks[blocks.length - 1];
  if (last?.type === "text") {
    const updated: TextBlockLike = { type: "text", text: last.text + text };
    return [...blocks.slice(0, -1), updated];
  }
  return [...blocks, { type: "text", text }];
}

interface TextBlockLike {
  type: "text";
  text: string;
}

function reduceToolCallStarted(state: ChatState, event: StreamEvent, lastSeq: number): ChatState {
  const messageId = str(event.message_id);
  const callId = str(event.call_id);
  if (!messageId || !callId) return { ...state, lastSeq };
  const block: ToolCallBlock = {
    type: "tool_call",
    call_id: callId,
    tool: str(event.tool) ?? callId,
    label: str(event.label) ?? str(event.tool) ?? callId,
    summary: str(event.summary),
    status: "running",
    effects: stringArray(event.effects),
  };
  const messages = state.messages.map((message) => {
    if (message.id !== messageId) return message;
    if (message.blocks.some((existing) => existing.type === "tool_call" && (existing as ToolCallBlock).call_id === callId)) {
      return message;
    }
    return { ...message, blocks: [...message.blocks, block] };
  });
  return { ...state, lastSeq, messages };
}

function reduceToolCallFinished(state: ChatState, event: StreamEvent, lastSeq: number): ChatState {
  const callId = str(event.call_id);
  if (!callId) return { ...state, lastSeq };
  const status = event.ok === true ? "done" : event.ok === false ? "failed" : "done";
  const messages = state.messages.map((message) => ({
    ...message,
    blocks: message.blocks.map((block) => {
      if (block.type !== "tool_call" || (block as ToolCallBlock).call_id !== callId) return block;
      const tool = block as ToolCallBlock;
      const updated: ToolCallBlock = {
        ...tool,
        status,
        duration_ms: typeof event.duration_ms === "number" ? event.duration_ms : tool.duration_ms,
      };
      return updated;
    }),
  }));
  return { ...state, lastSeq, messages };
}

function reduceApprovalRequested(state: ChatState, event: StreamEvent, lastSeq: number): ChatState {
  const approval = event.approval as Partial<Approval> | undefined;
  if (!approval?.id) return { ...state, lastSeq };
  const approvals = new Map(state.approvals);
  approvals.set(approval.id, {
    id: approval.id,
    tool: approval.tool ?? "tool",
    title: approval.title ?? null,
    rationale: approval.rationale ?? null,
    summary: approval.summary ?? null,
    effects: Array.isArray(approval.effects) ? approval.effects.map(String) : [],
    expires_at: approval.expires_at ?? "",
    status: approval.status ?? "pending",
  });
  return { ...state, lastSeq, approvals };
}

function reduceApprovalResolved(state: ChatState, event: StreamEvent, lastSeq: number): ChatState {
  const approvalId = str(event.approval_id);
  if (!approvalId) return { ...state, lastSeq };
  const approvals = new Map(state.approvals);
  approvals.delete(approvalId);
  return { ...state, lastSeq, approvals };
}

function statusForReason(reason: string | null): string {
  switch (reason) {
    case "error":
      return "error";
    case "cancelled":
      return "stopped";
    case "interrupted":
      return "interrupted";
    default:
      return "complete";
  }
}

function str(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

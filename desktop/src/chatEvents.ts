// Pure projection of the server's SSE stream onto the message transcript.
//
// The server is the source of truth (see chat/service.py): it appends a block
// for every tool call, approval, and artifact and flushes the message *before*
// it emits the matching event. This module mirrors that shaping so the
// transcript updates frame by frame without re-fetching the snapshot (a
// refetch would race the streaming text).

import type { Approval, Block, ChatEvent, Message, Source } from "./bridge";

interface ToolDetail {
  arguments?: Record<string, unknown>;
  result_summary?: string | null;
  result?: unknown;
  error?: unknown;
}

/** The computer session a tool call drives, as the backend reports it. */
export interface ComputerSession {
  id: string;
  url: string | null;
}

export interface ToolBlock {
  type: "tool_call";
  call_id: string;
  tool: string;
  label: string;
  summary: string;
  status: string;
  duration_ms: number | null;
  effects: string[];
  approval_id: string | null;
  detail: ToolDetail;
  /** `"computer"` when this call drives a screen, else null. The UI decides
   *  from this field, never from the tool's name, so the backend may name the
   *  tool anything. */
  surface: string | null;
  session: ComputerSession | null;
}

export interface ApprovalBlock {
  type: "approval";
  approval: Approval;
}

export interface ArtifactBlock {
  type: "artifact";
  artifact: Record<string, unknown>;
}

export function isToolBlock(block: Block): block is Block & ToolBlock {
  return block.type === "tool_call";
}

export function isApprovalBlock(block: Block): block is Block & ApprovalBlock {
  return block.type === "approval" && typeof block.approval === "object";
}

export function isArtifactBlock(block: Block): block is Block & ArtifactBlock {
  return block.type === "artifact" && typeof block.artifact === "object";
}

/** A tool's live status the server reports on ``tool_call_finished``. */
const TOOL_STATUS_AFTER: Record<string, string> = {
  approved: "running",
  denied: "blocked",
  expired: "expired",
};

const str = (value: unknown, fallback = ""): string =>
  typeof value === "string" ? value : fallback;

const num = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) ? value : null;

const strings = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];

/** A session id is the only part that matters; a missing URL is normal. */
const computerSession = (value: unknown): ComputerSession | null => {
  if (!value || typeof value !== "object") return null;
  const data = value as Record<string, unknown>;
  const id = str(data.id);
  if (!id) return null;
  return { id, url: str(data.url) || null };
};

/**
 * Which tool blocks own a computer card: the first call for each session in the
 * transcript. Later calls against the same session render only their tool row,
 * so one screen never produces several live mirrors — and the card's click
 * target stays put.
 */
export function computerCardAnchors(messages: Message[]): Set<string> {
  const sessions = new Set<string>();
  const anchors = new Set<string>();
  for (const message of messages) {
    for (const block of message.blocks) {
      if (!isToolBlock(block) || block.surface !== "computer" || !block.session) continue;
      if (sessions.has(block.session.id)) continue;
      sessions.add(block.session.id);
      anchors.add(block.call_id);
    }
  }
  return anchors;
}

/**
 * Debug-card scaffolding for the computer card, as pure list operations so the
 * lifecycle is unit-testable without React: inject one ephemeral card per
 * thread (refresh in place on re-fire), dismiss by call id, re-inject the
 * remembered cards when returning to a thread, and clear on sign-out.
 */
export const DEBUG_CARD_SESSION = "debug-session";

let debugCounter = 0;

export function debugCardMessage(now: number = Date.now()): Message {
  const callId = `debug_${now.toString(36)}_${(debugCounter += 1)}`;
  return {
    id: `debug_msg_${callId}`,
    role: "assistant",
    status: "complete",
    blocks: [
      {
        type: "tool_call",
        call_id: callId,
        tool: "computer.session",
        label: "Computer",
        summary: "Debug card — no tool actually ran.",
        status: "done",
        duration_ms: 4200,
        effects: [],
        approval_id: null,
        detail: {},
        surface: "computer",
        session: { id: DEBUG_CARD_SESSION, url: null },
      },
    ],
  };
}

const withoutDebugCards = (messages: Message[]): Message[] =>
  messages.filter(
    (candidate) =>
      !candidate.blocks.some(
        (block) => isToolBlock(block) && block.session?.id === DEBUG_CARD_SESSION,
      ),
  );

/** Add the debug card to a thread's synthetic list, refreshing it in place. */
export function injectDebugCard(synthetic: Map<string, Message[]>, threadId: string): Message {
  const message = debugCardMessage();
  synthetic.set(threadId, [...withoutDebugCards(synthetic.get(threadId) ?? []), message]);
  return message;
}

/** Drop a card by call id from both the view list and the remembered list. */
export function dismissCardMessage(messages: Message[], synthetic: Message[], callId: string): Message[] {
  const without = (list: Message[]) =>
    list.filter(
      (message) =>
        !message.blocks.some((block) => isToolBlock(block) && block.call_id === callId),
    );
  const next = without(messages);
  synthetic.splice(0, synthetic.length, ...without(synthetic));
  return next;
}

/** The view list for an opened thread: snapshot plus remembered synthetics. */
export function withSyntheticCards(snapshot: Message[], synthetic: Message[]): Message[] {
  return [...snapshot, ...synthetic];
}

function patchMessage(
  messages: Message[],
  messageId: string,
  update: (message: Message) => Message,
): Message[] {
  const index = messages.findIndex((message) => message.id === messageId);
  if (index === -1) return messages;
  const next = messages.slice();
  next[index] = update(messages[index]);
  return next;
}

function upsertMessage(messages: Message[], message: Message): Message[] {
  const index = messages.findIndex((item) => item.id === message.id);
  if (index === -1) return [...messages, message];
  const next = messages.slice();
  next[index] = message;
  return next;
}

/** Append ``text`` to the message, opening a new text block after a tool call. */
function appendText(messages: Message[], messageId: string, text: string): Message[] {
  if (!messageId || !text) return messages;
  if (!messages.some((message) => message.id === messageId)) {
    return [
      ...messages,
      { id: messageId, role: "assistant", status: "streaming", blocks: [{ type: "text", text }] },
    ];
  }
  return patchMessage(messages, messageId, (message) => {
    const blocks = message.blocks.slice();
    const last = blocks[blocks.length - 1];
    if (last && last.type === "text") {
      blocks[blocks.length - 1] = { ...last, text: str(last.text) + text };
    } else {
      // A fresh text block: the first token, or one opening after a tool call.
      blocks.push({ type: "text", text });
    }
    return { ...message, blocks };
  });
}

function upsertToolBlock(
  messages: Message[],
  data: Record<string, unknown>,
  phase: "started" | "finished",
): Message[] {
  const messageId = str(data.message_id);
  const callId = str(data.call_id);
  if (!messageId || !callId) return messages;

  return patchMessage(messages, messageId, (message) => {
    const blocks = message.blocks.slice();
    const index = blocks.findIndex((block) => isToolBlock(block) && block.call_id === callId);
    const existing = index === -1 ? undefined : (blocks[index] as Block & ToolBlock);

    if (phase === "started") {
      const block: Block & ToolBlock = {
        type: "tool_call",
        call_id: callId,
        tool: str(data.tool),
        label: str(data.label, str(data.tool)),
        summary: str(data.summary),
        status: "running",
        duration_ms: null,
        effects: strings(data.effects),
        approval_id: existing?.approval_id ?? null,
        detail: {
          arguments: (data.arguments as Record<string, unknown>) ?? {},
          result_summary: null,
          result: null,
          error: null,
        },
        surface: str(data.surface) || existing?.surface || null,
        session: computerSession(data.session) ?? existing?.session ?? null,
      };
      if (index === -1) blocks.push(block);
      else {
        blocks[index] = {
          ...(existing ?? {}),
          ...block,
          approval_id: existing?.approval_id ?? null,
        } as Block & ToolBlock;
      }
      return { ...message, blocks };
    }

    const status = str(data.status, data.ok === false ? "failed" : "done");
    const detail = (data.detail as ToolDetail) ?? existing?.detail ?? {};
    if (index === -1) {
      blocks.push({
        type: "tool_call",
        call_id: callId,
        tool: str(data.tool),
        label: str(data.tool),
        summary: str(data.result_summary),
        status,
        duration_ms: num(data.duration_ms),
        effects: [],
        approval_id: null,
        detail,
        surface: str(data.surface) || null,
        session: computerSession(data.session),
      });
    } else {
      blocks[index] = {
        ...(existing ?? {}),
        status,
        duration_ms: num(data.duration_ms),
        summary: str(data.result_summary, existing?.summary ?? ""),
        detail,
        surface: str(data.surface) || existing?.surface || null,
        session: computerSession(data.session) ?? existing?.session ?? null,
      } as Block & ToolBlock;
    }
    return { ...message, blocks };
  });
}

function onApprovalRequested(
  messages: Message[],
  data: Record<string, unknown>,
): Message[] {
  const messageId = str(data.message_id);
  const approval = data.approval as Approval | undefined;
  if (!messageId || !approval?.id) return messages;

  return patchMessage(messages, messageId, (message) => {
    const blocks = message.blocks.map((block) => {
      if (isToolBlock(block) && block.call_id && block.call_id === approval.call_id) {
        return { ...block, status: "waiting_approval", approval_id: approval.id };
      }
      return block;
    });
    if (!blocks.some((block) => isApprovalBlock(block) && block.approval.id === approval.id)) {
      blocks.push({ type: "approval", approval });
    }
    return { ...message, blocks };
  });
}

function onApprovalResolved(
  messages: Message[],
  data: Record<string, unknown>,
): Message[] {
  const approvalId = str(data.approval_id);
  const status = str(data.status);
  if (!approvalId) return messages;
  const toolStatus = TOOL_STATUS_AFTER[status];

  return messages.map((message) => {
    let changed = false;
    const blocks = message.blocks.map((block) => {
      if (isApprovalBlock(block) && block.approval.id === approvalId) {
        changed = true;
        return { ...block, approval: { ...block.approval, status } };
      }
      if (toolStatus && isToolBlock(block) && block.approval_id === approvalId) {
        changed = true;
        return { ...block, status: toolStatus };
      }
      return block;
    });
    return changed ? { ...message, blocks } : message;
  });
}

function appendArtifact(messages: Message[], data: Record<string, unknown>): Message[] {
  const messageId = str(data.message_id);
  const artifact = data.artifact as Record<string, unknown> | undefined;
  if (!messageId || !artifact) return messages;

  return patchMessage(messages, messageId, (message) => {
    const handle = artifact.handle;
    if (message.blocks.some((block) => isArtifactBlock(block) && block.artifact.handle === handle)) {
      return message;
    }
    return { ...message, blocks: [...message.blocks, { type: "artifact", artifact }] };
  });
}

function setSources(messages: Message[], data: Record<string, unknown>): Message[] {
  const messageId = str(data.message_id);
  if (!messageId || !Array.isArray(data.sources)) return messages;
  const sources = data.sources as Source[];
  const total = num(data.sources_total) ?? sources.length;
  return patchMessage(messages, messageId, (message) => ({
    ...message,
    sources,
    sources_total: total,
  }));
}

/**
 * Fold one stream event into the transcript. Events that only touch turn or
 * thread state (``turn_started``, ``usage_updated``, …) are ignored here and
 * handled by ``useChat``; the same array is returned so React can skip a render.
 */
export function applyEvent(messages: Message[], payload: ChatEvent): Message[] {
  const data = payload.data ?? {};
  const type = payload.event ?? str(data.type);

  switch (type) {
    case "message_created": {
      const message = data.message as Message | undefined;
      return message ? upsertMessage(messages, message) : messages;
    }
    case "token_delta":
      return appendText(messages, str(data.message_id), str(data.text));
    case "tool_call_started":
      return upsertToolBlock(messages, data, "started");
    case "tool_call_finished":
      return upsertToolBlock(messages, data, "finished");
    case "approval_requested":
      return onApprovalRequested(messages, data);
    case "approval_resolved":
      return onApprovalResolved(messages, data);
    case "artifact_ready":
      return appendArtifact(messages, data);
    case "sources_ready":
      return setSources(messages, data);
    default:
      return messages;
  }
}

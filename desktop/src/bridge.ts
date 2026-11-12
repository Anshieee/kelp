// Thin wrapper over the Rust bridge. The webview never talks to the server
// directly — see docs/DESKTOP_API_BRIDGE_PLAN.md for why (no CORS, and the
// server's CSRF gate rejects a tauri:// origin).

import { isTauri } from "./tauri";

export const NO_BRIDGE: ApiFailure = {
  status: 0,
  code: "no_bridge",
  message: "The desktop shell is not running.",
  retryable: false,
};

export interface ApiFailure {
  status: number;
  code: string;
  message: string;
  retryable: boolean;
}

export interface Thread {
  id: string;
  title: string;
  title_source?: string;
  updated_at: string;
  created_at?: string;
  message_count?: number;
}

export interface Block {
  type: string;
  text?: string;
  [key: string]: unknown;
}

/** A citation chip. Stored on the message, not inside a block. */
export interface Source {
  kind?: string;
  tool?: string;
  title?: string;
  url?: string;
  status?: string;
  [key: string]: unknown;
}

export interface Message {
  id: string;
  role: "user" | "assistant";
  status?: string;
  turn_id?: string;
  superseded_by?: string | null;
  blocks: Block[];
  sources?: Source[];
  sources_total?: number;
}

/** One pending (or settled) human-approval card for a mutating tool call. */
export interface Approval {
  id: string;
  tool: string;
  skill?: string;
  title: string;
  rationale?: string;
  summary?: string;
  effects?: string[];
  scopes?: Record<string, string[]>;
  command?: string[] | null;
  status: "pending" | "approved" | "denied" | "expired" | "consumed" | string;
  thread_id?: string;
  turn_id?: string | null;
  call_id?: string | null;
  requested_at?: string;
  expires_at?: string;
  ttl_seconds?: number;
  single_use?: boolean;
  decided_at?: string | null;
  [key: string]: unknown;
}

export interface Snapshot {
  ok: boolean;
  thread: Thread;
  messages: Message[];
  last_seq: number;
  has_more: boolean;
}

export interface ChatEvent {
  threadId: string;
  id: string | null;
  event: string | null;
  data: Record<string, unknown>;
}

export interface StreamState {
  threadId: string;
  state: "open" | "closed" | "error";
  message?: string;
  status?: number;
}

async function call<T>(command: string, args?: Record<string, unknown>): Promise<T> {
  if (!isTauri()) throw NO_BRIDGE;
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<T>(command, args);
}

export const bridge = {
  serverUrl: () => call<string>("bridge_server_url"),

  signIn: (bearer: string, deviceLabel: string) =>
    call<{ ok: boolean }>("bridge_sign_in", { bearer, deviceLabel }),

  signOut: () => call<Record<string, unknown>>("bridge_sign_out"),

  api: <T>(method: string, path: string, body?: unknown) =>
    call<T>("bridge_api", { method, path, body }),

  openStream: (threadId: string, after: number) =>
    call<void>("bridge_open_stream", { threadId, after }),

  closeStream: (threadId: string) =>
    call<void>("bridge_close_stream", { threadId }),
};

/** Concatenate the text blocks of a message. Tool and attachment blocks are
 *  ignored until the UI learns to render them. */
export function messageText(message: Message): string {
  return message.blocks
    .filter((block) => block.type === "text")
    .map((block) => block.text ?? "")
    .join("");
}

export async function listThreads(limit = 50): Promise<Thread[]> {
  const data = await bridge.api<{ threads: Thread[] }>(
    "GET",
    `/api/threads?limit=${limit}`,
  );
  return data.threads ?? [];
}

export function createThread(title?: string): Promise<{ thread: Thread }> {
  return bridge.api("POST", "/api/threads", title ? { title } : {});
}

export function getThread(threadId: string): Promise<Snapshot> {
  return bridge.api("GET", `/api/threads/${threadId}`);
}

export function sendTurn(threadId: string, text: string, clientTurnId: string) {
  return bridge.api<{ turn_id: string }>(
    "POST",
    `/api/threads/${threadId}/turns`,
    { client_turn_id: clientTurnId, text },
  );
}

export function stopTurn(turnId: string) {
  return bridge.api("POST", `/api/turns/${turnId}/stop`);
}

export function retryTurn(turnId: string) {
  return bridge.api<{ turn_id: string }>("POST", `/api/turns/${turnId}/retry`);
}

/** One-tap decision. Server enforces single-use, TTL, and idempotent replay. */
export function decideApproval(
  approvalId: string,
  decision: "approve" | "deny",
  note?: string,
) {
  return bridge.api("POST", `/api/approvals/${approvalId}/decision`, {
    decision,
    ...(note ? { note } : {}),
  });
}

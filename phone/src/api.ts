/*
 * Typed fetch client for the Kelp chat API (docs/CHAT_SPEC.md §15).
 *
 * - Always same-origin, relative URLs only (`/api/...`); the app is served
 *   from the Kelp server itself, so there is no host to configure.
 * - The session cookie rides along via `credentials: "include"`.
 * - Every mutation carries `X-CSRF-Token` from the paired session.
 * - Any 401 clears the session and returns the app to the pairing screen.
 * - Errors are the spec's JSON envelope; they become `ApiError` with the
 *   server's human-facing message (never a stack trace or raw payload).
 */
import { getCsrfToken, setSession, type ThreadSummary } from "./state";

const BASE = "/api";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryable: boolean;

  constructor(status: number, code: string, message: string, retryable: boolean) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.retryable = retryable;
  }
}

type UnauthorizedListener = () => void;
let unauthorizedListener: UnauthorizedListener | null = null;

/** Register the callback fired whenever the API answers 401. */
export function onUnauthorized(listener: UnauthorizedListener | null): void {
  unauthorizedListener = listener;
}

export interface ApiEnvelopeError {
  code?: string;
  message?: string;
  retryable?: boolean;
}

export interface PairResponse {
  ok?: boolean;
  session: {
    csrf_token: string;
    device: { id: string; label: string };
    expires_at: string | null;
  };
}

export interface ThreadRecord extends ThreadSummary {
  title_source?: string;
  created_at?: string;
}

export interface ThreadsResponse {
  ok?: boolean;
  threads: ThreadRecord[];
  next_cursor?: string | null;
}

export interface ThreadDetail {
  ok?: boolean;
  thread: ThreadRecord;
  messages: Message[];
  last_seq: number;
  has_more?: boolean;
}

export interface SendTurnResponse {
  ok?: boolean;
  turn_id: string;
  user_message_id?: string;
  assistant_message_id?: string;
}

export interface ApprovalsResponse {
  ok?: boolean;
  approvals: Approval[];
}

export interface DecisionResponse {
  ok?: boolean;
  approval: Approval;
  executed?: boolean;
}

export interface DevicesResponse {
  ok?: boolean;
  devices: DeviceRecord[];
}

export interface DeviceRecord {
  id: string;
  label: string;
  created_at: string;
  last_seen: string | null;
  expires_at?: string | null;
  online: boolean;
  current: boolean;
}

/* --- message / block shapes (CHAT_SPEC §3.2) --- */

export interface TextBlock {
  type: "text";
  text: string;
}

export interface ToolCallBlock {
  type: "tool_call";
  call_id: string;
  tool: string;
  label?: string;
  summary?: string | null;
  status: string;
  duration_ms?: number | null;
  effects?: string[];
}

export interface AttachmentBlock {
  type: "attachment";
  attachment: { handle: string; name: string; kind?: string; size_bytes?: number | null };
}

export interface ArtifactBlock {
  type: "artifact";
  artifact: {
    handle: string;
    name: string;
    kind: string;
    open_url?: string;
    download_url?: string;
    size_bytes?: number | null;
  };
}

export interface ScheduleTriggerBlock {
  type: "schedule_trigger";
  origin?: string;
  title?: string;
  scheduled_for?: string;
}

export type MessageBlock =
  | TextBlock
  | ToolCallBlock
  | AttachmentBlock
  | ArtifactBlock
  | ScheduleTriggerBlock
  | { type: string; [key: string]: unknown };

export interface SourceChip {
  id?: string;
  kind?: string;
  title?: string;
  url?: string;
  status?: string;
}

export interface Message {
  id: string;
  thread_id: string;
  role: "user" | "assistant";
  turn_id: string | null;
  status: string;
  created_at: string;
  updated_at?: string;
  blocks: MessageBlock[];
  sources?: SourceChip[];
}

export interface Approval {
  id: string;
  thread_id: string;
  turn_id?: string | null;
  tool: string;
  title?: string | null;
  rationale?: string | null;
  summary?: string | null;
  effects?: string[];
  command?: string[] | string | null;
  arguments?: unknown;
  status: string;
  requested_at?: string;
  expires_at: string;
}

interface RequestOptions {
  body?: unknown;
  /** Extra headers for the one bearer-authenticated call (pairing). */
  authorization?: string;
}

async function request<T>(method: string, path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.authorization) headers["Authorization"] = options.authorization;
  const csrf = getCsrfToken();
  if (method !== "GET" && csrf) headers["X-CSRF-Token"] = csrf;

  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      method,
      headers,
      credentials: "include",
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    });
  } catch {
    throw new ApiError(0, "network", "Can't reach the Kelp server right now.", true);
  }

  if (response.status === 401) {
    setSession(null);
    unauthorizedListener?.();
    throw new ApiError(401, "unauthorized", "This device's session ended. Pair again.", false);
  }

  if (!response.ok) {
    let code = "unknown_error";
    let message = "Something went wrong.";
    let retryable = false;
    try {
      const data = (await response.json()) as { error?: ApiEnvelopeError };
      if (data?.error) {
        if (typeof data.error.code === "string") code = data.error.code;
        if (typeof data.error.message === "string") message = data.error.message;
        retryable = data.error.retryable === true;
      }
    } catch {
      // Non-JSON error body; keep the generic copy.
    }
    throw new ApiError(response.status, code, message, retryable);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/* --- pairing (the only bearer-authenticated call) --- */

export async function pair(
  authToken: string,
  deviceLabel: string,
): Promise<SessionInfoLike> {
  const data = await request<PairResponse>("POST", "/auth/session", {
    authorization: `Bearer ${authToken}`,
    body: { device_label: deviceLabel },
  });
  const session = data?.session;
  if (!session?.csrf_token || !session?.device?.id) {
    throw new ApiError(500, "bad_pair_response", "The server's pairing response was incomplete.", false);
  }
  return {
    csrfToken: session.csrf_token,
    device: { id: session.device.id, label: session.device.label },
    expiresAt: session.expires_at ?? null,
  };
}

export interface SessionInfoLike {
  csrfToken: string;
  device: { id: string; label: string };
  expiresAt: string | null;
}

/* --- threads --- */

export function listThreads(limit = 50, cursor?: string): Promise<ThreadsResponse> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (cursor) params.set("cursor", cursor);
  return request<ThreadsResponse>("GET", `/threads?${params}`);
}

export function createThread(title?: string): Promise<{ ok?: boolean; thread: ThreadRecord }> {
  return request("POST", "/threads", { body: title ? { title } : {} });
}

export function getThread(id: string): Promise<ThreadDetail> {
  return request("GET", `/threads/${encodeURIComponent(id)}`);
}

export function renameThread(id: string, title: string): Promise<{ ok?: boolean; thread: ThreadRecord }> {
  return request("PATCH", `/threads/${encodeURIComponent(id)}`, { body: { title } });
}

export function deleteThread(id: string): Promise<{ ok?: boolean }> {
  return request("DELETE", `/threads/${encodeURIComponent(id)}`);
}

/* --- turns --- */

export function sendTurn(
  threadId: string,
  text: string,
  clientTurnId: string,
): Promise<SendTurnResponse> {
  return request("POST", `/threads/${encodeURIComponent(threadId)}/turns`, {
    body: { text, client_turn_id: clientTurnId },
  });
}

export function stopTurn(turnId: string): Promise<{ ok?: boolean }> {
  return request("POST", `/turns/${encodeURIComponent(turnId)}/stop`, { body: {} });
}

export function retryTurn(turnId: string): Promise<{ ok?: boolean; turn_id: string }> {
  return request("POST", `/turns/${encodeURIComponent(turnId)}/retry`, { body: {} });
}

/* --- approvals --- */

export function pendingApprovals(threadId?: string): Promise<ApprovalsResponse> {
  const params = new URLSearchParams({ status: "pending" });
  if (threadId) params.set("thread_id", threadId);
  return request("GET", `/approvals?${params}`);
}

export function decideApproval(
  approvalId: string,
  decision: "approve" | "deny",
): Promise<DecisionResponse> {
  return request("POST", `/approvals/${encodeURIComponent(approvalId)}/decision`, {
    body: { decision },
  });
}

/* --- metrics (Prometheus text format) --- */

export async function metricsText(): Promise<string> {
  const response = await fetch(`${BASE}/metrics`, { credentials: "include" });
  if (response.status === 401) {
    setSession(null);
    unauthorizedListener?.();
    throw new ApiError(401, "unauthorized", "This device's session ended. Pair again.", false);
  }
  if (!response.ok) {
    throw new ApiError(response.status, "metrics_unavailable", "Metrics are unavailable.", false);
  }
  return response.text();
}

/* --- devices / session --- */

export function listDevices(): Promise<DevicesResponse> {
  return request("GET", "/auth/devices");
}

export function revokeDevice(idShort: string): Promise<{ ok?: boolean }> {
  return request("DELETE", `/auth/devices/${encodeURIComponent(idShort)}`);
}

export function signOut(): Promise<{ ok?: boolean }> {
  return request("DELETE", "/auth/session");
}

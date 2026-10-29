# Dashboard chat surface (KEL-8 / P0-2)

- **Status:** spec only, 2026-10-29. **No UI code and no server code in this slice.**
- **Branch:** `kel-8` (from `main` at `daf3b14`, after KEL-6 + KEL-7 merged)
- **Builds on:** `docs/KERNEL.md` (loop, events, budgets, usage), `docs/OPENSHELL.md`
  (policy, approvals, audit), `docs/SKILL_SPEC.md` v1.0 (manifests, envelopes)
- **Design brief:** `docs/frontend-scope-simple.md` (screens 1-5 and 13; screen 4, the
  GitHub sweep, is specified here as `SweepCard` in §6.1)
- **Gate:** decision **Q15** — no UI is built until the Figma file is ready. This document
  is what Figma and the eventual frontend slice work from. It describes behaviour and
  contracts; it does not draw anything.

KEL-6 produces one ordered stream of typed events per user turn and never raises.
KEL-7 produces single-use, 10-minute approval objects and an audit trail. KEL-8 is the
surface that makes both usable from a browser: it persists threads, turns the kernel's
event stream into an append-only, resumable wire stream, derives renderable cards from
tool envelopes, and exposes approval decisions as ordinary HTTP calls.

---

## 1. Scope

In scope for KEL-8 (P0-2):

| # | Capability | Notes |
|---|---|---|
| 1 | Threaded chat | Message list per thread, streaming assistant turns, interleaved tool calls |
| 2 | Stop / retry | Cancel a running turn; re-run a failed turn without duplicating messages |
| 3 | Tool-call cards | One collapsed card per call: name, argument summary, status, duration; expandable |
| 4 | Inline approvals | KEL-7 approval objects rendered in the thread with approve / deny and a rationale; the turn blocks until answered |
| 5 | Artifact cards | PDF and CSV results as inline preview cards with open/download, plus the textual answer |
| 6 | Source chips | Chips under an answer, derived from tool output, not from model prose |
| 7 | GitHub sweep card | Wider card per `github.read` sweep (contiguous same-repo calls): repo, item count, short item list (`SweepCard`, §6.1) |
| 8 | Thread list, rename, reopen | Thread list, rename, reopen, reconnect mid-stream without duplicate messages |
| 9 | Error states | Provider, budget, tool, and approval failures as plain-language states with retry |

Out of scope (unchanged from the plan):

- Wallet / saved cards / pay modal (P1, Q18), voice (P1, Q10), WhatsApp mirror (P1, Q7),
  mobile app layout, memory viewer, activity-log screen, scheduler screen.
- Approval inbox *as a separate screen*. The chat surface renders the same approval
  objects inline; the inbox (screen 5 in the design brief) is a later view over the same
  endpoints, and its contract is already adequate here (§7.5).
- Prompt assembly, retrieval, and conversation compaction: the chat layer stores and
  replays message history, but how history becomes model context is a separate slice.
- Model-authored UI. Every card is derived server-side from a typed envelope (§6, §8, §9).

### 1.1 Honest dependencies

These are not built yet either, and KEL-8 acceptance depends on them:

| Dependency | State | Effect on KEL-8 |
|---|---|---|
| `docs.answer` handler | stub (`SkillNotImplementedError`) | No real PDF/CSV artifact until a handler exists. The demo path needs a fixture-backed handler or a recorded model fixture. |
| `web.search` handler | stub (`SkillNotImplementedError`) | No live results or web source chips. The demo path needs a fixture-backed handler or a recorded success envelope; a failed stub yields only the failed-search chip (§9.1). |
| `github.read` handler | stub (`SkillNotImplementedError`) | No real sweeps or GitHub source chips. Same fixture requirement: a recorded envelope must satisfy the manifest output schema and the §6.1 card shape. |
| Cancellation in `run_turn` | absent | "Stop" needs the kernel delta in §14.1 |
| Approval wait vs. wall-clock budget | approval wait counts against `max_wall_seconds` | A 10-minute approval can never finish inside a 120 s budget; see §14.2 |
| Durable chat store | absent (KEL-6 persists usage only) | Resume and non-duplication need §13 |
| Auth for the dashboard | not in this slice | Contract assumes a single owner; §12 states the minimum |

---

## 2. Vocabulary (use these words everywhere, including Figma layer names)

| Term | Meaning | Identifier |
|---|---|---|
| **Thread** / **session** | One conversation. The two words are synonyms; the product says "thread". | `ses_<32 hex>` |
| **Turn** | One user message plus everything the assistant does in response. | `turn_<32 hex>` |
| **Message** | A user or assistant item in a thread. | `msg_<32 hex>` |
| **Block** | A part of a message: text, tool call, artifact, approval (assistant), attachment (user). | index within message |
| **Attachment** | A file the person uploaded and attached to a user message; it becomes an artifact with the same handle. | `artifact:<8-128 chars>` |
| **Tool call** | One model-requested tool invocation and its envelope. | `call_<provider id>` |
| **Approval** | A KEL-7 approval object awaiting a human decision. | `apr_<32 hex>` |
| **Artifact** | A stored PDF or CSV with an opaque handle. | `artifact:<8-128 chars>` |
| **Source** | One citable result from a tool (web result, PDF page, CSV row). | `src_<index>` |
| **Event** | One append-only item in a thread's stream, addressed by `seq`. | `seq` (int, per thread, monotonic) |

An assistant message is a **sequence of blocks**. Text and tool calls interleave in the
order the kernel produced them, because the loop yields token deltas and tool calls from
the same stream. Cards are never floated or reordered to the end of the message.

---

## 3. Thread model

### 3.1 Thread record

```json
{
  "id": "ses_8f3c…",
  "title": "Nebius GPU pricing",
  "title_source": "auto",
  "created_at": "2026-10-29T09:12:03.114Z",
  "updated_at": "2026-10-29T09:14:51.902Z",
  "last_message_preview": "The current list price is…",
  "message_count": 6,
  "active_turn_id": null
}
```

- Titles are auto-generated from the first user message (deterministic truncation of the
  user text at 60 chars, not a model call) and become `title_source: "auto"`. Renaming
  sets `title_source: "user"` and stops auto-titling.
- `active_turn_id` is non-null only while a turn is running. The composer and Figma's
  "Working…" state key off it.

### 3.2 Message record (as served to the client)

A user message carries text and `attachment` blocks; an assistant message carries text,
tool-call, artifact, and approval blocks. Both use the same record and block ordering.

```json
{
  "id": "msg_5e02…",
  "thread_id": "ses_8f3c…",
  "role": "user",
  "turn_id": "turn_91c4…",
  "status": "complete",
  "created_at": "2026-10-29T09:12:03.980Z",
  "updated_at": "2026-10-29T09:12:03.980Z",
  "blocks": [
    {
      "type": "attachment",
      "attachment": {
        "handle": "artifact:q3report01",
        "name": "q3-report.pdf",
        "kind": "pdf",
        "mime": "application/pdf",
        "size_bytes": 1843200,
        "status": "attached",
        "open_url": "/api/artifacts/artifact:q3report01/content"
      }
    },
    { "type": "text", "text": "Summarise this and give me revenue by region." }
  ],
  "sources": []
}
```

Attachment blocks appear only on user messages, before the text they introduce, and only
after the upload is accepted (`attached`). The uploading, rejected (type/size), and failed
states are composer-chip states (§16.1) and are never persisted as message content. The
handles on the message must match the turn's `attachment_handles` (§5.1); a mismatch is
`400 invalid_message`.

```json
{
  "id": "msg_2b7a…",
  "thread_id": "ses_8f3c…",
  "role": "assistant",
  "turn_id": "turn_91c4…",
  "status": "streaming",
  "created_at": "2026-10-29T09:12:04.020Z",
  "updated_at": "2026-10-29T09:12:09.140Z",
  "blocks": [
    { "type": "text", "text": "I'll look that up." },
    {
      "type": "tool_call",
      "call_id": "call_a1",
      "tool": "web.search",
      "label": "Web search",
      "summary": "Nebius GPU pricing",
      "status": "done",
      "duration_ms": 412,
      "effects": ["read", "network"],
      "approval_id": null,
      "detail": {
        "arguments": { "query": "Nebius GPU pricing", "max_results": 5 },
        "result_summary": "5 results",
        "result": { "ok": true, "output": { "results": [] } },
        "error": null
      }
    },
    {
      "type": "artifact",
      "artifact": {
        "handle": "artifact:q3report01",
        "name": "q3-report.pdf",
        "kind": "pdf",
        "mime": "application/pdf",
        "size_bytes": 1843200,
        "pages": 12,
        "rows": null,
        "open_url": "/api/artifacts/artifact:q3report01/content",
        "preview_url": "/api/artifacts/artifact:q3report01/content#page=1&toolbar=0",
        "download_url": "/api/artifacts/artifact:q3report01/content?download=1"
      }
    }
  ],
  "sources": [
    {
      "id": "src_0",
      "kind": "web",
      "title": "GPU pricing — Nebius",
      "url": "https://nebius.com/prices",
      "snippet": "H200 from $2.90/hr…",
      "published_at": "2026-10-01",
      "status": "ok",
      "tool": "web.search"
    }
  ]
}
```

Statuses:

| Message status | Meaning | Figma state |
|---|---|---|
| `pending` | User message accepted, turn not started | Done (user bubble) |
| `streaming` | Text and/or cards still arriving | Working |
| `complete` | `turn_finished` with `reason: "stop" \| "length"` | Done |
| `error` | `turn_finished` with `reason: "error"`, or a provider/tool failure that ended the turn | Failed |
| `stopped` | Human pressed Stop | Done, with "Stopped" note |
| `interrupted` | Server died mid-turn and the turn was closed on restart | Failed, "That didn't work" + Resume |

A user message is `pending` from acceptance until its turn starts, then `complete`; the
terminal failure statuses describe the assistant message, not the user bubble.

`sources` is message-level, not a block: chips render under the whole answer, deduped by
`(kind, url)` or `(kind, handle, locator)` and ordered by first appearance (§9).

### 3.3 Text streaming rule

`token_delta` appends to the **last text block** of the message. `tool_call_started`
closes the current text block and opens a tool block; the next `token_delta` opens a new
text block after the card. This is the single rule the renderer needs; no client-side
reconstruction of provider deltas.

---

## 4. Stream contract

### 4.1 Transport decision

**Server-Sent Events, one stream per open thread.** This closes the open decision in the
KEL-6 issue ("SSE vs WebSocket"):

| Requirement | SSE | WebSocket |
|---|---|---|
| Server→client token deltas, approvals, artifacts | native | native |
| Client→server actions | already POST (send, stop, decide) | would need message framing |
| Reconnect + replay | built in (`Last-Event-ID`, `retry:`) | hand-rolled |
| Proxy/auth behaviour | plain HTTP, same cookies | upgrade handling, extra config |
| Multiple tabs | N independent readers, same log | N sockets, same fan-out |

Chosen: `POST` for every mutation, `GET` for one SSE stream. WebSocket is not needed
until bidirectional streaming exists (voice, P1).

The stream is **per thread, not per turn**, so an approval that outlives its turn, or one
created outside a turn (scheduler, exec gate), still arrives on the open thread.

### 4.2 Stream endpoints

```
GET /api/threads/{thread_id}/stream
    Accept: text/event-stream
    Last-Event-ID: <seq>          (or ?after=<seq> for EventSource implementations
                                   that cannot set headers)
```

- Replays every persisted event with `seq > after`, then tails live events.
- No `after`: replays from `seq = 0` (a fresh client normally sends `after=last_seq`
  from the history snapshot, §10.1).
- Frames:

```
id: 1042
event: token_delta
retry: 2000
data: {"seq":1042,"thread_id":"ses_8f3c…","turn_id":"turn_91c4…","message_id":"msg_2b7a…","type":"token_delta","text":"The current","step":1,"ts":"2026-10-29T09:12:09.140Z"}

: keep-alive
```

- `id` is the event `seq`. `event` equals `type`. `ts` is ISO-8601 UTC.
- Heartbeat comment every 15 s. The server closes an idle stream after 5 minutes; the
  client reconnects with the last id (browser does this automatically).
- Slow consumers are disconnected, never silently truncated: the log is the source of
  truth and replay from `seq` recovers everything.
- `Cache-Control: no-store`, `X-Accel-Buffering: no`, no compression.

### 4.3 Event catalog

Kernel event names are preserved one-for-one as `type`. The adapter only adds envelope
fields and the events the UI needs to place blocks.

| `type` | Emitted when | Data (beyond `seq`, `thread_id`, `turn_id`, `ts`) | Source |
|---|---|---|---|
| `message_created` | Once per message: user message accepted, then assistant message opened | `message` (full message record) | chat layer |
| `turn_started` | Turn begins | `turn_id`, `message_id`, `model`, `limits` (from `TurnStarted`) | KEL-6 |
| `token_delta` | Provider text | `message_id`, `text`, `step` | KEL-6 `TokenDelta` |
| `tool_call_started` | Model called a tool | `message_id`, `call_id`, `tool`, `label`, `summary`, `arguments` (redacted), `effects`, `step` | KEL-6 `ToolCallStarted` + manifest |
| `tool_call_finished` | Envelope returned | `message_id`, `call_id`, `ok`, `status`, `duration_ms`, `result_summary`, `error`, `step` | KEL-6 `ToolCallFinished` |
| `approval_requested` | Human decision needed | `approval` (normalized card, §7.2) | KEL-7 / KEL-5 hook |
| `approval_resolved` | Decision recorded | `approval_id`, `status`, `decided_at`, `actor`, `executed` | chat layer |
| `usage_updated` | Provider reported usage | `message_id`, `model`, `input_tokens`, `output_tokens`, `estimated_cost_usd`, `step` | KEL-6 `UsageUpdated` |
| `sources_ready` | Tool produced citable results | `message_id`, `sources` (full array, deduped) | chat layer, from envelope |
| `artifact_ready` | Tool produced a PDF/CSV card | `message_id`, `artifact` (card, §8.2) | chat layer, from envelope |
| `error` | Turn-level failure | `message_id`, `code`, `message`, `retryable`, `user_fixable`, `details`, `step` | KEL-6 `Error` |
| `turn_finished` | Turn terminal | `message_id`, `message_status`, `reason`, counts, `estimated_cost_usd`, `duration_ms`, `usage_persisted` | KEL-6 `TurnFinished` |
| `thread_updated` | Rename / auto-title | `title`, `title_source`, `updated_at` | chat layer |

`reason` values: `stop`, `length`, `budget`, `error`, plus `cancelled` and `interrupted`
(§14.1, §10.4). `message_status` is the mapping in §3.2.

`arguments` is included on `tool_call_started` for the human-facing card. It is redacted
by the same scrubber the skill runtime uses for error details and is capped at 4 KB;
the model-facing copy stays hash-only per `SKILL_SPEC.md` §8.3.

### 4.4 Ordering and idempotency

- `seq` is assigned by the chat layer at persist time, per thread, starting at 1. Kernel
  events have no sequence number and must not be given one.
- Every mutation is idempotent: replaying `seq <= last_applied_seq` is a no-op on the
  client; `POST` sends carry `client_turn_id` (§5.2).
- `token_delta` is the only event type that mutates an existing block. Everything else
  either opens a block or updates a card by `call_id` / `approval_id` / `handle`.

---

## 5. Chat behaviour

### 5.1 Sending a message

1. First send in a new conversation: `POST /api/threads` creates the thread (server id).
   Then `POST /api/threads/{id}/turns` with text and optional `attachment_handles`.
2. Server validates (non-empty, ≤ 8,000 characters after trim — code points, not bytes;
   ≤ 5 attachments), stores the user message, creates the assistant message, assigns
   `turn_id`, returns `202`.
3. The turn starts asynchronously; the client is already tailing the stream and receives
   `message_created` → `turn_started` → deltas.
4. On completion the composer re-enables. On failure it re-enables with a retry action.

One active turn per thread. A second send while a turn runs returns `409 turn_in_progress`
with `active_turn_id`. The composer is disabled while `active_turn_id` is set; the only
enabled action is Stop.

### 5.2 Idempotent sends

The client generates `client_turn_id` (UUIDv4) per send attempt and reuses it on retry
after a network failure.

- Same `client_turn_id` within 24 h → `200` with the existing turn and message ids; no
  second user message, no second model call.
- Same text sent again with a **new** `client_turn_id` is a new turn. Deduplication is by
  idempotency key only, never by text equality.

This is what makes "reconnect during a stream restores the partial turn without
duplicating messages" true even when the POST response was lost.

### 5.3 Stop

`POST /api/turns/{turn_id}/stop` → `202`.

- Kernel delta D1 (§14.1) makes the loop stop between provider chunks/steps.
- Terminates with `error.code = "turn_cancelled"`? No — no error. Persist
  `turn_finished.reason = "cancelled"`, message status `stopped`, usage record
  `outcome: "cancelled"`. Partial text stays exactly as streamed; cards stay.
- Stop on a finished turn → `409 turn_not_running` (idempotent no-op for the UI).
- Stop is not a retry. The user can send a new message afterwards.

### 5.4 Retry

`POST /api/turns/{turn_id}/retry` → `202` with a **new** `turn_id`.

- Allowed only for terminal turns with status `error`, `stopped`, or `interrupted`, and
  only for the latest turn in the thread (`409 not_latest_turn` otherwise).
- Reuses the stored user message text and attachments. The old assistant message keeps
  its status and gains `superseded_by: turn_id`; it is not deleted, so the thread shows
  what happened.
- Budgets reset: a retry is a new turn with a full budget, and a new usage record.

### 5.5 Budget and long-turn behaviour

`KERNEL.md` budgets are sent to the client in `turn_started.limits` and are not user-editable
in the prototype.

- Budget exhaustion arrives as `error` (`turn_budget_exceeded`, `details.limit`) followed
  by `turn_finished(reason="budget")`. Copy: "That answer got too long. Ask a narrower
  question, or continue from where it stopped." with **Continue** (sends a new turn) —
  not "Try again", which would repeat the same cost.
- After 8 s of a running turn, the client may show "still working" (design brief §3).
  After 45 s, show elapsed seconds. The server does not fake progress.

---

## 6. Tool-call cards

One card per call, collapsed by default, placed in the message at the point the call
happened. The card is a **view of a KEL-5 envelope**, not a new protocol.

| Field | Source | Notes |
|---|---|---|
| `tool` | `tool_call_started.name` | e.g. `web.search` |
| `label` | manifest `name` → display map, fallback = tool name | `web.search` → "Web search" |
| `summary` | derived from arguments | ≤ 120 chars, single line, no newlines, no secret values |
| `status` | lifecycle | `running` → `waiting_approval` → `done` / `failed` / `blocked` / `expired` |
| `duration_ms` | `tool_call_finished.duration_ms` | hidden while running |
| `effects` | manifest `permissions.effects` | shown as text in the expanded card, never as colour alone |
| `detail.arguments` | redacted arguments | pretty-printed, 4 KB cap, `{ "truncated": true }` beyond |
| `detail.result` | `tool_call_finished.result` | already bounded to 20,000 chars by the kernel; render `preview` with a "truncated" note when `result.truncated` is true |
| `detail.error` | envelope `error` | code kept for "See details"; plain `message` shown first |

Argument summary rules (server-side, deterministic, no model involvement):

1. If the tool has a display rule, use it — `web.search` → `query`; `docs.answer` →
   `question`; `github.read` → `repo` + `kind`; `schedule.create` → `title` + cron expr;
   `shell.exec` → the command joined with spaces.
2. Otherwise take the first 1-3 scalar values in manifest property order, `key=value`,
   comma-separated.
3. Truncate at 120 chars with `…`. Strip newlines. Never include a value of a key whose
   name looks secret-like (`*token*`, `*key*`, `*secret*`, `*password*`, `*credential*`).
4. Empty arguments → the label alone.

`shell.exec` is not a KEL-5 skill: it is the OpenShell gate's exec tool. Its card is created
from the gate approval, `effects` include `execute`, and its `command` is the argv list from
that approval (`OPENSHELL.md` §2) — never a shell string and never a manifest field. Every
`shell.exec` card is `waiting_approval` before it can be `running`.

Status mapping from the loop:

| Kernel signal | Card status | Figma state |
|---|---|---|
| `tool_call_started`, no approval needed | `running` | Working |
| `approval_requested` for this `call_id` | `waiting_approval` | Waiting on you |
| `tool_call_finished`, `ok: true` | `done` | Done |
| `tool_call_finished`, `ok: false` | `failed` | Failed |
| `error.code = "approval_denied"` | `blocked` | Failed, "You said no. Nothing ran." |
| approval expiry | `expired` | Failed, "It expired. Nothing ran." |

Expandable region content: arguments, result summary, error message + "See details"
(code, field, retryable), `call_id`, effects, and for approval-gated calls the link to the
approval card (`approval_id`). Collapsed content is exactly: label · summary · status ·
duration.

Cards are read-only. There is no "re-run this tool" in the prototype; retry is per turn
(§5.4). A settled card never animates.

### 6.1 `SweepCard` (`github.read`)

The design brief's GitHub sweep (screen 4) is a wider card, but it is still derived from
ordinary `github.read` tool calls. On the wire the block stays `type: "tool_call"` with
`tool: "github.read"`; `SweepCard` is a renderer variant keyed by that tool, not a new block
kind, event, or endpoint. Its expanded state is the `ToolCallCard` expanded region plus the
item summary below, so the two components cannot drift. A sweep is the contiguous run of
`github.read` calls in one message that share `repo` and `kind`: the first call anchors the
card, the rest contribute rows and keep their own `call_id` (order is unchanged, §3.3).

```json
{
  "type": "tool_call",
  "call_id": "call_b7",
  "tool": "github.read",
  "status": "done",
  "detail": {
    "arguments": { "kind": "issue", "repo": "Anshieee/nebius-nvidia" },
    "sweep": {
      "repo": "Anshieee/nebius-nvidia",
      "kind": "issue",
      "count": 12,
      "items": [
        { "number": 12, "title": "Wire the approval card", "state": "open",
          "url": "https://github.com/…" }
      ],
      "truncated": true
    }
  }
}
```

- `repo` and `kind` echo the validated arguments; the repo is the manifest allowlist entry.
- `count` is the number of items the sweep found: the number of `github.read` results in the
  run, or `repo.open_issues_count` when `kind: "repo"` reports one.
- `items` is capped at 5 on the card (`truncated: true` beyond); `count` remains the full
  total. The client never pages GitHub itself.
- States: `scanning` (`tool_call_started`, `running`), `found` (≥1 item), `empty`
  (`ok: true`, zero items), `failed` (envelope `ok: false`; §11 copy). `empty` is a normal
  result, not an error: "Nothing matched in {repo}." A failed sweep keeps the repo name and
  uses the owning turn's retry (§5.4), not a card-local button.
- Read-only by manifest (`SKILL_SPEC.md` §12.2): a sweep never offers comment/label/merge
  actions. Anything that would write is an approval card (§7).
- Item URLs follow the chip link rules (§9.2); item title and body render as text only.

---

## 7. Inline approvals

### 7.1 One card, two sources

Approvals reach the chat surface from two producers, and the UI renders one shape:

| Producer | Object fields | Shape difference |
|---|---|---|
| Skill runtime hook (`SKILL_SPEC.md` §8.3, `skills.context.ApprovalRequest`) | `skill`, `tool`, `argument_hash`, `effects`, `scopes`, `manifest_version`, `requested_at`, `expires_at`, `single_use` | no `command`, has `scopes` |
| OpenShell gate (`docs/OPENSHELL.md` §2, `openshell.approvals.ApprovalRequest`) | `id`, `tool`, `command`, `argument_hash`, `effects`, `blast_radius`, `requested_at`, `expires_at`, `actor`, `status`, `single_use`, `metadata` | has `command` + `blast_radius`, no `scopes` |

The chat layer assigns an `approval_id` (`apr_…`) to skill-runtime approvals too, so every
card has a stable id that the decision endpoint can address. `source` tells the renderer
which detail rows exist; the renderer never assumes `command` is present.

### 7.2 Wire shape (`ApprovalCard`)

```json
{
  "id": "apr_4d9e…",
  "source": "skill",
  "thread_id": "ses_8f3c…",
  "turn_id": "turn_91c4…",
  "call_id": "call_a2",
  "tool": "schedule.create",
  "skill": "schedule.create",
  "title": "Create a recurring task",
  "rationale": "This adds a job to your scheduler. It runs on the schedule below until you turn it off. Nothing has run yet.",
  "summary": "Weekday open-issue summary · 0 8 * * 1-5 · Europe/Berlin",
  "effects": ["write"],
  "blast_radius": ["fs:write:${WORKSPACE}/var/scheduler/jobs.json"],
  "scopes": { "paths": ["${WORKSPACE}/var/scheduler/**"], "hosts": [], "repos": [] },
  "command": null,
  "arguments": { "title": "Weekday open-issue summary", "schedule": { "kind": "cron", "expr": "0 8 * * 1-5" } },
  "arguments_digest": "sha256:9f1c…",
  "requested_at": "2026-10-29T09:13:02.001Z",
  "expires_at": "2026-10-29T09:23:02.001Z",
  "ttl_seconds": 600,
  "single_use": true,
  "status": "pending",
  "decided_at": null,
  "consumed_at": null,
  "cost": { "amount_usd": null, "known": false }
}
```

- `rationale` is generated server-side from `effects` + scope, never from model text.
  Templates: `write` → "The assistant wants to change something in your workspace.";
  `execute` → "The assistant wants to run a command on this machine. It runs once.";
  `network` beyond an allowlisted read → "The assistant wants to send data to {host}.";
  `write` + money (later) → "This will charge {amount}.". Always append: "Nothing has run
  yet. This approval expires in 10 minutes and works once."
- `arguments` are served to the owner (the UI is not the model; `SKILL_SPEC.md` §8.3 keeps
  only the *envelope* copy argument-free), scrubbed with the same redactor and capped at
  4 KB. `command` for exec approvals is the argv list, never a shell string.
- `cost` stays `{amount_usd: null, known: false}` in the prototype; the field exists so the
  card does not change shape when payments land (KEL-10).

### 7.3 Rendering rules

- The approval card is inline in the thread, at the point of the call, and also mirrored in
  the approvals inbox view over the same data.
- Statuses and copy:

| Status | Card | Buttons | Figma state |
|---|---|---|---|
| `pending` | "Waiting on you", expiry countdown ("expires in 9:41") | Approve, Deny | Waiting on you |
| `approved` | "Approved just now" | none | Working (until the call settles) |
| `consumed` | "Done. Used once." | none | Done |
| `denied` | "You said no. Nothing ran." | none | Failed (calm) |
| `expired` | "It expired. Nothing ran." + "Ask again" | Ask again | Failed |

- The warning accent is reserved for approvals, and a pending approval is the only card
  that may carry it. If several are pending (for example a multi-call turn), all are
  visible, all share the same accent, and all are actionable — no second emphasis colour,
  no modal.
- Countdown is cosmetic; `expires_at` is authoritative and the server rejects late
  decisions (§7.4). Never show a countdown that keeps running past `expires_at`.
- Approve does **not** navigate or open a modal in the prototype (no money yet). It shows
  the `approved` state and waits for the call to settle.

### 7.4 Decision contract

```
POST /api/approvals/{approval_id}/decision
{ "decision": "approve" | "deny", "note": "optional, ≤ 200 chars" }
```

| Outcome | HTTP | Body |
|---|---|---|
| Recorded | `200` | `{ "ok": true, "approval": {…status…}, "executed": false }` |
| Unknown id | `404` | `error.code = "unknown_approval"` |
| Already decided (same decision) | `200` | current approval, `idempotent: true` |
| Conflicting decision: approve after `denied`, deny after `approved`, or any decision on an approval that is not `pending` | `409` | `error.code = "not_approved"` |
| Expired | `409` | `error.code = "expired"` |
| Consumed | `409` | `error.code = "already_used"` |

- Approve-after-deny and deny-after-approve are conflicts, not state changes: the first
  decision is final for that approval object (`not_approved`, HTTP `409`), the stored status is
  unchanged, and another attempt needs a new approval object. The UI keeps the card's existing
  state and never offers the opposite button.
- For a skill-runtime approval, the decision resolves the awaiting hook: approve → the
  loader runs the handler exactly once and the call continues in the same turn; deny → the
  loader returns its `approval_denied` envelope, the card goes `blocked`, and the turn
  continues (the model sees the denial). `approval_resolved.executed` is `true` once the
  handler has run, so both sources report the field with the same meaning.
- For an OpenShell approval, approve then execution is a separate, audited step
  (`gate.execute`); `approval_resolved.executed` reports when the process actually ran.
  A decision never executes anything by itself in the API call's request scope.
- Denial and expiry leave no side effect (KEL-7 guarantee) and are audited by the gate.
- Replay is impossible: the store consumes once (`already_used`) and re-checks policy at
  execution time. The UI surfaces that as "Already used. Nothing ran twice."

### 7.5 Inbox compatibility

The design brief (`docs/frontend-scope-simple.md` §4) lists `POST /propose` and
`POST /approve` for screen 5. The real contract is narrower and safer:

| Brief call | Real call |
|---|---|
| `POST /propose` | none — approvals are created by the policy gate and skill hook, never by the client |
| `POST /approve` | `POST /api/approvals/{id}/decision` |
| `GET /activity` | not part of KEL-8 (activity-log slice) |

Figma should draw the inbox as a list of `ApprovalCard`s (`GET /api/approvals?status=pending`)
with the same four states as the inline card.

---

## 8. Artifact cards

### 8.1 Where cards come from

Only from a validated `docs.answer` success envelope (`skills/builtin/docs_answer`):

```json
{ "answer": "…",
  "artifact": { "handle": "artifact:q3report01", "kind": "pdf", "mime": "application/pdf",
                "pages": 12 },
  "citations": [ { "locator": "p.4", "text": "Revenue was $4.2M" } ] }
```

For CSV the `artifact` object is
`{ "handle": …, "kind": "csv", "mime": "text/csv", "rows": 1840 }` and `citations` carry
row locators (`r.42`).

The chat layer validates `handle` against `^artifact:[A-Za-z0-9_-]{8,128}$`, resolves it in
its own artifact registry, and emits `artifact_ready`. A handle the registry does not know
produces no card and an `error` of `artifact_not_found` — a model cannot invent a file
reference that becomes a clickable download.

### 8.2 Card shape

```json
{
  "handle": "artifact:q3report01",
  "name": "q3-report.pdf",
  "kind": "pdf",
  "mime": "application/pdf",
  "size_bytes": 1843200,
  "pages": 12,
  "rows": null,
  "columns": null,
  "created_at": "2026-10-29T09:10:00.000Z",
  "open_url": "/api/artifacts/artifact:q3report01/content",
  "preview_url": "/api/artifacts/artifact:q3report01/content#page=1&toolbar=0",
  "download_url": "/api/artifacts/artifact:q3report01/content?download=1",
  "table": null
}
```

CSV cards also carry a server-rendered preview so the client never parses the file:

```json
"table": {
  "columns": ["region", "revenue"],
  "rows": [["EMEA", "4210000"], ["APAC", "2980000"]],
  "preview_rows": 5,
  "total_rows": 1840,
  "truncated": true
}
```

- `preview_rows` is 5 in the collapsed card, up to 20 expanded (`GET /preview?rows=20`).
  Cell values are strings, server-truncated to 60 chars; the client must not compute
  sums or formats from them — the numeric answer belongs to the text answer.
- PDF preview is page 1 only, rendered by the browser from the `content` endpoint inside a
  sandboxed iframe (`#page=1&toolbar=0`; `sandbox` without `allow-same-origin` or navigation
  permissions, so the framed document can neither reach the app's DOM nor navigate the tab).
  `content` is the one route exempt from the app's frame-denying headers (§12): it is
  `SAMEORIGIN`-frameable so the preview works, and it cannot frame the app. No server-side
  rasteriser in the prototype; a thumbnail endpoint is a deferred enhancement, not a KEL-8
  dependency.
- "Open" opens `open_url` in a new tab (`rel="noopener"`); "Download" uses
  `Content-Disposition: attachment` and the stored original name, filename-sanitised.

### 8.3 Artifact endpoints

| Call | Returns | Notes |
|---|---|---|
| `POST /api/attachments` (multipart, field `file`) | `{ ok, artifact }` | ≤ 20 MB, PDF (`%PDF-` magic) or CSV (UTF-8 text, comma/semicolon header), scanned for NUL bytes; opaque id on disk, original name only in metadata |
| `GET /api/artifacts/{handle}` | card metadata | 404 for unknown/expired/deleted |
| `GET /api/artifacts/{handle}/content` | bytes | `Content-Type` from the allowlist only; `X-Content-Type-Options: nosniff`; `Content-Disposition: inline` or `attachment` with `?download=1`; supports `Range` for PDFs; `ETag` = content hash; the only route allowed in an iframe: `X-Frame-Options: SAMEORIGIN` + CSP `frame-ancestors 'self'`, never `DENY` (§12) |
| `GET /api/artifacts/{handle}/preview?rows=20` | `{ columns, rows, total_rows, truncated }` | CSV only; 415 for PDF |

Access control: an artifact is readable only within its owning thread's session and only
while the thread exists. The handle is opaque, but opacity is not authorisation.
`artifacts.thread_id` is `NULL` at upload and is set exactly once, under the same transaction,
when a turn attaches the handle to its user message; until then the upload is visible only to
the owner's composer, and a handle already bound to another thread behaves as unknown (`404`).

### 8.4 Failure states

| Case | Card | Copy |
|---|---|---|
| Handler `not_implemented` | no card, tool card failed | "This document tool isn't finished yet." |
| Handle unknown | no card, `artifact_not_found` | "That file isn't available any more." |
| Preview fetch fails | card with retry on the preview area only | "Couldn't load the preview." + Try again |
| File deleted | metadata 404 | chip/tool card only, card removed on reload |

The textual answer always renders, even when the card fails.

---

## 9. Source chips

### 9.1 Derivation

Sources come from tool envelopes, server-side:

| Tool | Mapping |
|---|---|
| `web.search` | each `output.results[]` → `{kind: "web", title, url, snippet, published_at}` |
| `docs.answer` | each `output.citations[]` → `{kind: "doc", title: artifact name, handle, locator, snippet: text}` |
| `github.read` | each item's `html_url`/`title` → `{kind: "web", title, url, snippet}` (read-only sweep) |

Rules:

- Maximum 8 chips per message, deduped by `(kind, url)` or `(kind, handle, locator)`,
  first appearance wins. Extra sources are counted, not dropped silently: one `+{n} more`
  chip (§16.2) in the same row, which expands in place — never a separate screen and never a
  second overflow label.
- Order = first mention. Do not sort by model preference or relevance score.
- A failed search (`ok: false`, `timeout`) yields one failed chip:
  `{kind:"web", status:"failed", title:"Web search failed"}` so the person can see that
  the answer lacked live sources. A `docs.answer` failure yields a failed doc chip with
  the artifact name when known.
- Chip labels: web = hostname, doc = file name + locator (`q3-report.pdf p.4`). Titles,
  not snippets, in the collapsed chip.

### 9.2 Rendering and safety

- `url` must be `http:`/`https:`. Render as `<a target="_blank" rel="noopener noreferrer
  nofollow">`; never render `javascript:`, `data:`, or relative URLs as links.
- Snippets render as text, never as HTML or Markdown.
- Chips are keyboard-focusable, with the full title as accessible name and the hostname as
  visible label.
- Hostname display is the raw registered domain; a punycode host is shown decoded but the
  link href is the punycoded form (no homograph link text vs. target).

### 9.3 Message-level event

`sources_ready` replaces the message's source array each time it is emitted (the server
owns dedupe/order). Clients must apply it wholesale, not append, or two tool calls will
duplicate chips.

---

## 10. Resume

Resume has two distinct meanings; both must work.

### 10.1 Open / reopen a thread

```
GET /api/threads  →  { ok, threads: [ … thread records … ], next_cursor }
GET /api/threads/{thread_id}?limit=50&before=<msg_id>
   →  { ok, thread, messages: [ …oldest→newest, each with its own sources… ],
        last_seq, has_more }
```

- The history snapshot is authoritative: complete messages, block order, card states,
  approval states, artifact metadata, final message statuses.
- On receipt the client renders, records `last_seq`, and opens the stream with
  `after=last_seq`. That ordering (snapshot → tail) is the whole non-duplication rule:
  anything with `seq <= last_seq` is already in the snapshot and is skipped.
- Pagination is by message, newest-first fetch, displayed oldest-first. `last_seq` is
  thread-wide, not per page.

### 10.2 Reconnect mid-stream

Browser `EventSource` retries automatically; the client must also handle a manual
"Reconnecting…" banner after 2 failed attempts and a "Live again" state.

- Reconnect uses `Last-Event-ID` = the last `seq` applied.
- Server replays from the persisted log; the client drops duplicates by `seq`.
- Partial assistant text is persisted write-behind (flush at 250 ms or 4 KB, whichever
  first, plus every block boundary), so a reload shows the text up to the last flush and
  then continues. A reload must never show a message twice or a lost paragraph.
- Streaming text is append-only. There is no server-side "replace this message" event
  except `sources_ready` (array replacement) and card updates by id.

### 10.3 Multi-tab

Two tabs on one thread both tail the stream. Because every state change is an event with a
`seq`, both converge. Decisions made in one tab appear as `approval_resolved` in the other;
the second tab's buttons disable on that event. Optimistic UI is allowed but must
reconcile to the event, not to the local click.

### 10.4 Server restart mid-turn

- On boot, any turn still marked `running` with no live task is closed: message status
  `interrupted`, a persisted `turn_finished` with `reason: "interrupted"` at the boot
  timestamp, usage record `outcome: "interrupted"` (or `usage_persisted: false` if the
  store is unavailable — the same rule as KEL-6).
- The thread remains usable. The interrupted message shows "That didn't work. The server
  restarted." with **Resume**, which is `POST /api/turns/{turn_id}/retry` (§5.4). Nothing
  is auto-retried: an interrupted turn may already have had side effects, and KEL-7
  approvals are single-use.
- If the turn had a pending approval at crash time, the approval is restored from the
  chat store with its original `expires_at`. It stays decidable until that instant, then
  expires. It is never silently auto-approved.

### 10.5 Thread lifecycle

- `PATCH /api/threads/{id}` `{ "title": "…" }` → 1-80 chars, trimmed, sets
  `title_source: "user"`.
- `DELETE /api/threads/{id}` → soft delete; the thread disappears from the list, artifacts
  and approvals become unreachable, and the stream connection is closed. Reconnect
  attempts then get `404`, and the client stops retrying instead of looping.
- Deleting a thread with a running turn stops it first (`reason: "cancelled"`).

---

## 11. Errors and copy

One error renderer for everything. Codes are internal; the person sees one sentence.

| `error.code` | Plain sentence | Retry affordance |
|---|---|---|
| `missing_api_key` | "The assistant isn't connected to its model yet." | none (admin fix) |
| `provider_auth` | "The model service rejected the assistant's key." | none |
| `provider_model_not_found` | "The configured model isn't available." | none |
| `provider_timeout` | "The model took too long to answer." | Try again |
| `provider_rate_limited` | "The model service is busy." | Try again (after `Retry-After` if given) |
| `provider_unavailable` | "The model service is unavailable right now." | Try again |
| `provider_rejected` | "The model service refused that request." | none |
| `provider_unreachable` | "Can't reach the model service." | Try again |
| `provider_error` | "The model service returned an error it didn't explain." | Try again |
| `turn_budget_exceeded` | "That answer got too long." | Continue |
| `invalid_arguments` | "The assistant asked for something the tool can't do." | none |
| `invalid_tool_arguments` | "The assistant's tool request couldn't be read." | Try again |
| `invalid_output` | "The tool sent back something unexpected." | none |
| `timeout` (tool) | "That step took too long." | Try again |
| `handler_error` | "That step failed." | Try again |
| `tool_error` | "A tool crashed while running." | Try again |
| `no_tool_executor` | "This tool isn't available in this build." | none |
| `missing_secret` | "A tool isn't configured yet." | none |
| `not_implemented` | "This tool isn't finished yet." | none |
| `approval_required` | "This step is waiting for your approval." | Open the approval card (never an error banner) |
| `approval_denied` | "You said no. Nothing ran." | none |
| `expired` | "That approval expired. Nothing ran." | Ask again |
| `already_used` | "That was already used. Nothing ran twice." | none |
| `artifact_not_found` | "That file isn't available any more." | none |
| `unknown_skill` | "The assistant tried a tool that doesn't exist." | none |
| `turn_in_progress` | "Still answering. Wait or press Stop." | none |
| `rate_limited` | "Too many requests. Wait a moment." | Try again |
| *(any unlisted code)* | "Something went wrong." | Try again |

`invalid_tool_output` (kernel `loop._execute`) is the same failure as `invalid_output`; both
render the one sentence. An unlisted code is never shown: the catch-all row is the whole
contract, so a new code cannot leak a stack trace or provider payload to the person.

Stop and interruption are turn `reason` values, not error codes: `cancelled` renders
"Stopped" (§5.3), `interrupted` renders "The server restarted while answering." with
Resume (§10.4).

Rules:

- Never show a code, stack trace, HTTP status, provider payload, or secret in the main
  copy. "See details" may show `code`, `field`, and `retryable` — nothing else.
- A `429 rate_limited` carries `Retry-After` in whole seconds; use it for the Try again
  affordance (§12.1). The limiter never reports its own failure as a `5xx`.
- Errors never clear the composer text or the partial answer.
- A tool failure is not a turn failure: one failed card plus the model's recovery. Only
  `turn_finished(reason="error")` turns the whole message red.

---

## 12. Security and privacy

KEL-8 adds a network surface to a private assistant; these are requirements, not polish.

| Area | Requirement |
|---|---|
| Auth | Single owner. Session cookie `HttpOnly; Secure; SameSite=Lax`, 30-day rolling; `POST`/`PATCH`/`DELETE` require a CSRF token (double-submit) or an `Origin`-checked same-site request. Unauthenticated API → `401` JSON, not a redirect, for `/api/*`. |
| Authorisation | Thread, approval, and artifact access is checked against the authenticated owner on **every** call. Guessing an id must not leak existence: `404`, not `403`. |
| Secrets | No API keys or Stripe values in any response; the provider's base URL is server config and is never echoed to the client. `NEBIUS_API_KEY` stays server-side. `arguments`, `result`, and audit text pass the existing scrubbers; error envelopes keep their no-echo rule. |
| SSE | Same-origin only; no `Access-Control-Allow-Origin: *`; no `EventSource` with credentials in a URL; stream disconnects on auth expiry. |
| XSS | Model text renders as text (no `dangerouslySetInnerHTML`, no Markdown HTML). Chip URLs restricted to http/https. Artifact content served with `nosniff` and a fixed content-type allowlist, and downloaded filenames sanitised. |
| Path traversal | Artifact resolution is registry-lookup only, never `join(user_input)`. Handles are looked up, then the stored path is used. Symlinks are rejected; the target must resolve inside `${WORKSPACE}/var/artifacts`. |
| CSRF / clickjacking | SameSite cookies + CSRF token. Frame denial is scoped to **app HTML routes only**: `X-Frame-Options: DENY` and CSP `frame-ancestors 'none'` there. `/api/*` responses are not framed and carry no frame headers, except `/api/artifacts/{handle}/content`, which is exempt from `DENY` and served with `X-Frame-Options: SAMEORIGIN` + CSP `frame-ancestors 'self'`, `nosniff`, and an allowlist `Content-Type`; it is only ever embedded by the app's sandboxed preview iframe (§8.2), so the preview works without making the app frameable. |
| DoS | Per-thread concurrency limit 1; body limits (message 8,000 characters after trim — the same character count as §5.1, not 8 KB; decision body 1 KB; upload 20 MB); one stream per tab, idle-closed at 5 min; per-class token buckets, a global turn cap, and a concurrent-stream cap per owner (§12.1). |
| Logs | Request logs contain ids, status, and duration — no message text, no arguments, no provider bodies. |
| Audit | Approval decisions and executions come from KEL-7's audit log; the chat layer does not duplicate it. |
| Retention | Messages and artifacts live under `var/` and are deletable per thread; nothing is uploaded to a third party beyond the tools' declared hosts. |

### 12.1 Rate limits and concurrency (KEL-17)

The limiter runs in front of dispatch, before routing or body work. Every
authenticated request costs one token from its route class, keyed by client
identity; a presented token is hashed and never retained as a key.

| Class | Routes | Default per minute / burst |
|---|---|---|
| `auth` | Only failed authentication attempts, keyed by peer address | 30 / 10 |
| `chat` | Thread create/rename/delete, turns, approvals | 120 / 30 |
| `upload` | `POST /attachments` | 60 / 20 |
| `read` | `GET` threads, approvals, artifacts | 600 / 200 |
| `admin` | `GET /metrics`, `GET /readyz` | 1200 / 300 |

- A rejected call is `429` with the ordinary error envelope
  (`error.code = "rate_limited"`, `retryable: true`) and a `Retry-After`
  header in whole seconds. It is never a `5xx`; an internal limiter fault
  fails open and the request proceeds.
- Failed authentication is charged to the independent `auth` bucket, so a
  burst of bad credentials cannot spend the owner's chat or read budget. A
  successful authentication clears that peer's failure debt, and a key that
  goes idle is discarded (default 300 s; `KELP_RATE_LIMIT_IDLE_SECONDS`), so
  the map is bounded and a quiet client starts from a full bucket.
- A global turn cap and a per-owner stream cap sit alongside the token
  buckets. Exceeding either returns `429 rate_limited` with
  `details.reason = "turn_capacity"` or `"stream_capacity"` and a short
  `Retry-After`; the stream is never opened and the turn is never queued.
  Streaming slots are returned on exhaustion, `close()`, or collection.
  A retry (`POST /api/turns/{id}/retry`) queues a fresh turn, so it takes a
  turn slot exactly like a send and returns it the same way: when the retried
  turn finalizes, or immediately if the call fails or is refused.
- The body cap is enforced by the HTTP adapter before dispatch. A
  `Content-Length` that is non-numeric, negative, duplicated, or above the
  route's cap is rejected without reading the body: `413 upload_too_large`
  for uploads, `400 invalid_request` elsewhere. A chunked body is read
  incrementally and aborted as soon as the next chunk would exceed that cap.
  A request that carries both `Content-Length` and `Transfer-Encoding` is
  rejected with `400 invalid_request` and the connection is closed, so the two
  framings can never disagree (RFC 7230 §3.3.3).
- Configuration is environment-only: `KELP_RATE_LIMIT_ENABLED`,
  `KELP_RATE_LIMIT_<CLASS>_RPM`, `KELP_RATE_LIMIT_<CLASS>_BURST`,
  `KELP_RATE_LIMIT_IDLE_SECONDS`, `KELP_MAX_CONCURRENT_TURNS`, and
  `KELP_MAX_STREAMS_PER_OWNER`. A zero RPM or burst denies that class; a zero
  turn or stream cap disables that cap.
- `GET /api/metrics` exposes `kelp_limiter_in_use`,
  `kelp_limiter_capacity`, and `kelp_limiter_saturation` with
  `class="turns"` and `class="streams"`. Stream capacity is per owner, so its
  `in_use` sample is the busiest owner's usage and its saturation the hottest
  owner's ratio.

This layer does not change `TurnBudget`: the kernel's per-turn step, token,
tool-call, and wall-clock limits remain the only turn limits.

---

## 13. Persistence requirements

The chat layer needs durable state that KEL-6 does not provide (it stores usage only).
Suggested: SQLite at `var/chat/chat.db`, `0600`, WAL, one writer.

```
threads      (id PK, title, title_source, created_at, updated_at, deleted_at)
messages     (id PK, thread_id, turn_id, role, status, created_at, updated_at, superseded_by)
blocks       (message_id, block_index PK, kind, payload_json)         -- read model, derived from events
events       (thread_id, seq PK, type, payload_json, ts)              -- append-only wire log
turns        (id PK, thread_id, user_message_id, assistant_message_id, state, reason,
              started_at, finished_at, budget_json, usage_json, client_turn_id UNIQUE)
approvals    (id PK, thread_id, turn_id, call_id, source, status, payload_json,
              requested_at, expires_at, decided_at, consumed_at)
artifacts    (handle PK, thread_id NULL until attached, name, kind, mime, size_bytes, sha256,
              storage_path, created_at, deleted_at)
```

Invariants:

- `events.seq` is gap-free per thread and assigned under the same transaction that writes
  the state change, so replay and snapshot can never disagree.
- Appends are `fsync`-ed at turn boundaries and every 250 ms during streaming; a crash may
  lose the last ≤ 250 ms of text but must never lose a user message, a decision, or a
  finished turn.
- `client_turn_id` is unique per thread, giving the idempotent send.
- `artifacts.thread_id` starts `NULL` and is claimed by exactly one attaching turn; attachment
  handles are immutable and never re-pointed to another thread.
- Deleting a thread cascades to messages, events, approvals, and artifacts.
- The wire log is retained for the life of the thread (prototype); compaction is a later
  slice.
- No card is stored as HTML. Only typed payloads are stored; rendering is derived.

Planned (not created in this slice) module layout:

```
chat/__init__.py        exports
chat/store.py           threads, messages, events, turns, approvals, artifacts
chat/service.py         send/stop/retry, turn orchestration around AgentLoop
chat/stream.py          SSE framing, replay, heartbeat, fan-out
chat/cards.py           envelope → tool card / artifact card / sources / approval card
chat/api.py             HTTP routes (framework chosen in the impl slice)
tests/test_chat_store.py, tests/test_chat_stream.py, tests/test_chat_cards.py,
tests/test_chat_api.py
```

---

## 14. Deltas this spec requires from KEL-6 / KEL-7

The spec is written against the merged KEL-6/KEL-7 code and cannot be implemented without
these small, additive changes. They are listed so the implementation slice budgets for
them and does not quietly work around them.

### 14.1 D1 — Cancellation (KEL-6)

`AgentLoop.run_turn()` has no stop path. Needed:

- a cooperative cancel signal checked between provider chunks and between steps;
- `TurnFinished.reason` accepts `"cancelled"` (today's doc comment lists
  `stop | length | budget | error`);
- `UsageRecord.outcome` records `"cancelled"`;
- no exception escapes, no partial tool call is left `running` — an in-flight tool call
  finishes or its card is marked `failed`/`blocked` with `error.code = "turn_cancelled"`.

### 14.2 D2 — Approval wait vs. wall clock (KEL-6 + KEL-8)

`run_turn` measures elapsed time with `time.monotonic()` (the injectable `time_fn`, which
defaults to `time.monotonic`) and checks `max_wall_seconds` between steps. No wall-clock
`time()` read is involved, so an NTP or manual clock change can neither shorten nor extend a
turn. A tool call that awaits a human for up to 600 s therefore still blows the 120 s budget.
Needed:

- either the loop subtracts time spent awaiting approval **on that same monotonic clock**
  (accumulate `waited_ms` and expose it on `ToolCallFinished`, so the turn's `duration_ms`
  stays honest), or the executor reports the wait and the loop pauses the budget clock;
- the approval wait must not be counted against the skill's `timeout_ms` either — already
  true in the loader (`SKILL_SPEC.md` §8.3), so only the turn budget needs the fix;
- when the approval expires and the hook returns `False`, the envelope is
  `approval_denied`; the chat layer distinguishes "expired" from "denied" by comparing
  `expires_at` to the local decision time, and by the `approval_resolved` event it wrote.

### 14.3 D3 — Nothing else changes

- Tool cards, sources, artifact cards, and the event envelope are all derived in the chat
  layer from existing fields (`ToolCallStarted.arguments`, `ToolCallFinished.result`,
  `UsageUpdated`, `Error`). No kernel changes.
- Approval objects already carry `expires_at`, `single_use`, `argument_hash`, and
  `to_dict()`; the chat layer adds ids and rationale only.
- Persisting `seq` is the chat layer's job; kernel events stay sequence-free.
- `docs.answer` must stop being a stub for artifact acceptance. Until then, the demo path
  needs a fixture-backed handler or a recorded provider fixture (the same deferred item
  `KERNEL.md` §4 names).

---

## 15. API contract (complete list)

Base path `/api`. All responses `application/json` unless noted. Standard envelopes:

```json
{ "ok": true, … }
{ "ok": false, "error": { "code": "…", "message": "…", "retryable": false,
  "user_fixable": true, "field": "/…", "details": { … } } }
```

The error object is `SKILL_SPEC.md` §10.2's shape, reused so there is one error renderer
on the client.

### 15.1 Threads

| Method | Path | Body / query | Success | Notes |
|---|---|---|---|---|
| `GET` | `/threads` | `?limit=50&cursor=` | `{ threads, next_cursor }` | Thread list, newest activity first |
| `POST` | `/threads` | `{ title? }` | `201 { thread }` | First send creates the thread, then posts the turn; no body → title from the first user message |
| `GET` | `/threads/{id}` | `?limit=50&before=` | `{ thread, messages, last_seq, has_more }` | History snapshot (§10.1) |
| `PATCH` | `/threads/{id}` | `{ title }` | `{ thread }` | Rename |
| `DELETE` | `/threads/{id}` | — | `{ ok: true }` | Soft delete; stops a running turn |
| `GET` | `/threads/{id}/stream` | `Last-Event-ID` or `?after=` | `text/event-stream` | §4.2 |

One id space, one name on the wire: `thread_id`. The kernel calls the same value
`session_id` (KEL-6 events and usage records); the chat adapter maps it and never exposes
both. `thread_id` and `session_id` are never different values.

### 15.2 Turns

| Method | Path | Body | Success | Errors |
|---|---|---|---|---|
| `POST` | `/threads/{id}/turns` | `{ client_turn_id, text, attachment_handles? }` | `202 { turn_id, user_message_id, assistant_message_id, stream_started: true }` or `200` on idempotent replay | `400 invalid_message`, `409 turn_in_progress`, `404 unknown_thread`, `429 rate_limited` |
| `POST` | `/turns/{turn_id}/stop` | `{}` | `202 { turn_id, status: "stopping" }` | `409 turn_not_running`, `404 unknown_turn` |
| `POST` | `/turns/{turn_id}/retry` | `{}` | `202 { turn_id, retry_of }` | `409 not_latest_turn`, `409 turn_not_retryable`, `404 unknown_turn` |

`POST /threads/{id}/turns` response echoes the accepted text so the client can reconcile a
lost response; the authoritative copy is the `message_created` event.

### 15.3 Approvals

| Method | Path | Body / query | Success | Errors |
|---|---|---|---|---|
| `GET` | `/approvals` | `?status=pending&thread_id=&limit=` | `{ approvals: [ApprovalCard] }` | — |
| `GET` | `/approvals/{id}` | — | `{ approval }` | `404 unknown_approval` |
| `POST` | `/approvals/{id}/decision` | `{ decision, note? }` | `{ approval, executed }` | `404`, `409 not_approved` / `expired` / `already_used` |

### 15.4 Artifacts and uploads

| Method | Path | Body / query | Success | Notes |
|---|---|---|---|---|
| `POST` | `/attachments` | multipart `file` | `201 { artifact }` | PDF/CSV, ≤ 20 MB |
| `GET` | `/artifacts/{handle}` | — | `{ artifact }` | Metadata only |
| `GET` | `/artifacts/{handle}/preview` | `?rows=20` | `{ columns, rows, total_rows, truncated }` | CSV only |
| `GET` | `/artifacts/{handle}/content` | `?download=1` | bytes | `Range` supported |

### 15.5 What Figma needs from the contract

Draw only these states; nothing else in the API changes what is on screen.

| Screen | Call | States to draw |
|---|---|---|
| Thread | `GET /threads/{id}` + stream | loading, empty, streaming, complete, error, stopped, interrupted |
| Composer | `POST /threads/{id}/turns` | idle, typing, sending, disabled (turn active), rate-limited, send-failed |
| Tool card | stream only | running, waiting_approval, done, failed, blocked, expired; collapsed + expanded |
| Sweep card | stream only | scanning, found, empty, failed |
| Attachment chip | `POST /attachments` | uploading, attached, rejected (type/size), failed; removable before send |
| Approval card | `GET /approvals/{id}` + decision | pending, approved, consumed, denied, expired; expiring soon |
| Artifact card | `GET /artifacts/{handle}` | PDF: preview loaded / failed; CSV: rows / empty / failed |
| Source chips | stream only | 1, 3, many (wraps), failed, +N overflow |
| Thread list | `GET /threads` | empty, populated, active, renaming, delete-confirm |
| Error banner | any | failed with Try again / Continue / Resend |

### 15.6 Health and readiness

Two process probes for a supervisor (systemd, Kubernetes, a load balancer). They are
not part of the UI contract and are never called by the client.

| Method | Path | Auth | Success | Failure |
|---|---|---|---|---|
| `GET` | `/api/healthz` | none | `200` liveness envelope | — |
| `GET` | `/api/readyz` | owner token | `200` all checks pass | `503` when any check fails |
| `GET` | `/api/health` | owner token | `200 { "ok": true }` | legacy alias, unchanged |

All three live under the `/api` prefix, like every other route in this spec.
`/api/healthz` is the only unauthenticated route. It touches no state and never reports
component detail: a `200` means the HTTP loop is serving. `/api/readyz` runs six checks
and returns `503` when any of them fails; there is no non-critical check, so every check
is critical to readiness. The body still enumerates every check so an operator can see
which component is degraded.

```json
{
  "ok": true,
  "status": "ok",
  "version": "0.1.0",
  "commit": "abc1234",
  "uptime_seconds": 412.73,
  "checks": {
    "store":     { "ok": true },
    "artifacts": { "ok": true },
    "audit":     { "ok": true },
    "usage":     { "ok": true },
    "provider":  { "ok": true, "cached": false, "age_seconds": 0.0 },
    "registry":  { "ok": true }
  }
}
```

`status` is `ok` or `degraded`.

| Check | What it proves |
|---|---|
| `store` | `BEGIN IMMEDIATE` succeeds and rolls back: the database is writable and unlocked |
| `artifacts` | a throwaway file can be created and removed under the artifact root |
| `audit` | the audit file opens for append (`O_APPEND`); nothing is written |
| `usage` | the usage JSONL opens for append; nothing is written |
| `provider` | the upstream answers `GET /models` with a 2xx, or a provider-supplied probe passes |
| `registry` | the loaded skill registry can enumerate its names |

A check that is not configured for the deployment (`audit` on a bare `ChatAPI`, or a
provider with no upstream) reports `{ "ok": true, "detail": "not_configured" }` so a
test double or embedded use does not turn every scrape into a false alarm.

Failure detail is a stable code only — `unavailable`, `timeout`, or `not_configured`.
Bodies never contain tokens, API keys, filesystem paths, or exception text. `version`
and `commit` come from `KELP_VERSION` and `KELP_GIT_COMMIT` (falling back to the
installed package version and `"unknown"`); a source checkout is not touched at request
time.

The provider probe is cached for at most 30 s (`cached` and `age_seconds` say whether
the current answer is reused), so a one-second scrape loop cannot hammer the upstream.
`age_seconds` is measured from when the probe answered, not from when it started.
Steady-state budget: p95 `/api/healthz` < 50 ms, p95 `/api/readyz` < 250 ms with a
warm cache.

---

## 16. What Figma must deliver

Per Q15, no implementation starts until these exist. This section is the checklist.

### 16.1 Components and states

| Component | States | Notes |
|---|---|---|
| `Thread` | empty (first run), populated, streaming, long (scrolled), narrow 390 px | Assistant text column max 760 px on desktop |
| `UserMessage` | normal, sending, failed (Resend) | Right-aligned per design brief |
| `AttachmentChip` | uploading (progress), attached, rejected (type or size), failed upload | Name + size; removable before send; same chip in the composer tray and the user bubble |
| `AssistantMessage` | streaming, complete, stopped, failed, interrupted, superseded | Failed keeps partial text + cards |
| `TextBlock` | streaming (caret), complete | Caret stops on `turn_finished` |
| `ToolCallCard` | collapsed/expanded × running, waiting_approval, done, failed, blocked, expired | Collapsed = label · summary · status · duration |
| `SweepCard` | scanning, found, empty, failed | Wider card per `github.read` sweep: repo, count, up to 5 items, "+N"; expanded form of the same `ToolCallCard` (§6.1) |
| `ApprovalCard` | pending, approved, consumed, denied, expired, expiring-soon | Approve/Deny visible, never in a menu; one warning colour |
| `ArtifactCardPDF` | loading preview, page-1 preview, preview failed, deleted | Open + Download |
| `ArtifactCardCSV` | collapsed (5 rows), expanded (20 rows / scroll), empty table, failed | Column headers, monospace numerals, "showing 5 of 1,840 rows" |
| `SourceChip` | ok, failed | Hostname (web) / file + locator (doc) |
| `SourceChipRow` | 1, 3, many (wrap), +N | Wraps to a second line, never scrolls horizontally |
| `Composer` | idle, typing, sending, disabled, stop-visible, error | Stop replaces Send while a turn runs |
| `TurnStatus` | working, still-working (8 s+), reconnecting, live-again, stopped | "Still working" appears at 8 s |
| `ThreadListItem` | default, active, hover, renaming, deleting | Title truncation with ellipsis |
| `EmptyThread` | first run | Copy: "Ask about a document, the web, or your repo. Nothing leaves this machine unless a tool needs it — and anything that changes something waits for your approval." |
| `ErrorBanner` | provider failed, budget, tool failed, rate-limited | One sentence + one action |

### 16.2 Copy (use verbatim; the client ships these strings)

- Working: "Working…" · "Reading your file…" · "Checking GitHub…" · "Searching the web…"
- Waiting: "Waiting on you" · "Needs your approval"
- Failed: "That didn't work" · "Try again" · "See details"
- Stopped: "Stopped"
- Expired approval: "It expired. Nothing ran." + "Ask again"
- Denied approval: "You said no. Nothing ran."
- Interrupted: "The server restarted while answering." + "Resume"
- Budget: "That answer got too long." + "Continue"
- Approval rationale suffix: "Nothing has run yet. This approval expires in 10 minutes and works once."
- Artifact CSV footer: "Showing {n} of {total} rows"
- Sources overflow: "+{n} more"

### 16.3 Layout constraints

- Desktop first; one narrow (390 px) version of the thread and the approval card only.
- One warning colour total (design brief §5). Status must not rely on colour alone:
  every card carries a text status.
- Tool cards and approval cards share the same left edge and corner radius; a pending
  approval is the only card with a visible border accent.
- Long answers scroll the thread, not the page; the composer is sticky at the bottom.
- Focus order for a pending approval: card → Approve → Deny → details. Announce state
  changes with `aria-live="polite"` (throttled to ~1/s for streaming text).

---

## 17. Testing plan for the implementation slice

No tests are added in this docs-only slice; `python -m pytest` stays at 153 passing.
When KEL-8 is implemented, the contract above is testable offline:

| Area | Test |
|---|---|
| Stream | replay from `seq`, no duplicates on reconnect, heartbeat, idle close, auth expiry closes stream |
| Idempotent send | same `client_turn_id` → one user message, one model call |
| Stop | cancel mid-stream → `reason: "cancelled"`, partial text preserved, no `running` cards |
| Restart | boot closes a fake `running` turn as `interrupted`; approval survives with original expiry |
| Cards | envelope → tool card for 0/1/3 calls; result `truncated` renders as truncated; argument summary redaction |
| Approvals | approve/deny/expire/consume/conflict status codes; skill hook path runs once; denial leaves no side effect |
| Artifacts | handle validation, unknown handle → no card, path traversal attempt, content-type allowlist, CSV preview caps, 20 MB limit |
| Sources | dedupe, cap 8, failed search chip, `+N` overflow |
| Errors | every row of §11 maps to copy and the right affordance |
| Security | cross-thread artifact/approval access → 404, CSRF rejection, no secrets in responses |

The P0-2 acceptance criteria map as follows: artifact card → §8 + artifact tests; inline
approval blocking the stream → §7.3-7.4 + approval tests; reconnect without duplicates →
§10.2-10.3 + stream tests; Playwright smoke is the frontend slice's job after Figma.

---

## 18. Open questions (do not block Figma)

| # | Question | Default if unanswered |
|---|---|---|
| 1 | Server framework for the API (FastAPI vs. stdlib `http.server` + SSE) | `httpx` is already a dependency; pick in the impl slice. Contract is framework-neutral |
| 2 | PDF preview without a rasteriser on machines that block iframe PDFs | Download-only fallback with a page-1 metadata row |
| 3 | Whether "Continue" after a budget stop should summarise the partial answer | No; plain continuation message |
| 4 | Artifact retention beyond the thread | Thread lifetime for the prototype |
| 5 | Auto-title by model instead of truncation | Truncation for the prototype (`KERNEL.md` §4 keeps prompt work out of this slice) |
| 6 | Approval inbox as its own screen | Reuse the same card; no new endpoints |

---

## 19. References

| Doc | Why |
|---|---|
| `docs/KERNEL.md` | event names, budgets, usage record, provider failure mapping |
| `docs/OPENSHELL.md` | approval lifecycle, blast radius, audit redaction |
| `docs/SKILL_SPEC.md` | envelopes, approval request fields, effects/scopes, error codes |
| `docs/frontend-scope-simple.md` | screens 1-5 and 13, the four states, copy tone |
| `docs/adr/0001-docs-answer-single-skill.md` | why PDF and CSV share `docs.answer` |
| `kernel/events.py`, `openshell/approvals.py`, `skills/context.py` | the objects this spec normalises |

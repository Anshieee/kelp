# Privacy review — what leaves the machine, and what does not (KEL-22 / M3-E)

- **Status:** written review, 2026-10-30; sharing cross-references added 2026-10-30
- **Branch:** `kel-22`; `kel-26` added the ADR 0002 material; the share rows below describe the
  shipped KEL-28 surface (ADR 0002 §14.1)
- **Scope:** the server process in `chat/`, `kernel/`, `skills/`, `openshell/`, and `payments/`
- **Companions:** the retention rule ([`docs/DEPLOY.md`](DEPLOY.md) §8.1, `chat/retention.py`),
  the policy gate ([`docs/OPENSHELL.md`](OPENSHELL.md)), the chat spec
  ([`docs/CHAT_SPEC.md`](CHAT_SPEC.md) §12), the sharing decision
  ([ADR 0002](adr/0002-sharing-and-identity.md)), and the control-layer
  contract ([`docs/SERVICE_CONTRACT.md`](SERVICE_CONTRACT.md),
  [ADR 0003](adr/0003-service-control-layer.md))
- **Enforced by:** `openshell/egress_ledger.py` (the ledger) and
  `tests/test_privacy.py` (recipient, ledger, and exec-boundary checks)

This is the written answer to the M3 question "who sees what". It has three parts:
**who receives data**, **where that data is stored on this machine and how it is
removed**, and **what the product does not promise**. The fourth part is the
exec-boundary decision: the approval card for running a command no longer promises
a run this build cannot perform.

Every claim below is checkable from the repository. The tests in
`tests/test_privacy.py` fail when this document and the code drift apart in either
direction: a recipient the code can reach that this document does not name, or a
recipient named here that the code no longer knows.

---

## 1. The short version

| Question | Answer |
|---|---|
| Where does the conversation go? | The model provider named by `KELP_PROVIDER_BASE_URL` (OpenRouter on the documented path) — every turn, in full |
| Does anything else receive conversation text? | Not by default: no analytics, no telemetry, no crash reporter, no CDN, no third-party log sink |
| Do skills reach the network? | Only through one chokepoint, and only to hosts a manifest declares. `github.write` can create a branch, commit, and pull request in `Anshieee/kelp`, but its handler cannot run before owner approval; other network skills are not implemented |
| Do payments reach the network? | Only Razorpay, and only when both `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET` are set: hosted Checkout (`/api/wallet/checkout`) and saved-card wallet (`/api/wallet`) routes are mounted, and each payment call goes to `api.razorpay.com` and writes an `egress` row (recipient `razorpay`). With no keys those routes answer `503 wallet_not_configured` and nothing is sent. Stripe is never constructed by `build_app` |
| Where are keys? | Only in the server process environment, or (provider key only) in the private tenant secrets file named by `KELP_TENANT_SECRETS_FILE`. `TAVILY_API_KEY` and `GITHUB_TOKEN` are read from `os.environ` only. Each is sent only to its own recipient and scrubbed from every log line and audit row |
| What records exist? | Chat content until the owner deletes the thread; audit and usage records on a stated clock; a content-free egress ledger inside the audit trail; a device row per signed-in browser with a short label and timestamps, no credential |
| Who can read a shared thread? | Whoever holds a share URL the owner created (`GET /api/shared/{token}`): no account, one frozen snapshot of one thread, until the link is revoked or expires (7 days by default, 30 at most). A share read is inbound, so it adds no recipient and no egress row; the store keeps a per-link daily view count and the audit one `share_viewed` record per link per day, with no viewer identity (§2.3) |
| Can I audit what left? | Yes: one `egress` line per outbound call — recipient, purpose, byte counts, outcome, no content |

The provider key is read from `KELP_PROVIDER_API_KEY` (from the tenant secrets
file instead, when `KELP_TENANT_SECRETS_FILE` is set); `NEBIUS_API_KEY` is
accepted only as a legacy alias when the canonical name is blank, and its value
is scrubbed from logs and audit lines exactly like the canonical one. The
endpoint itself comes from `KELP_PROVIDER_BASE_URL` (or its legacy alias), so no
vendor host is implicit.

---

## 2. Every outbound recipient

The recipient ids are the ones `openshell/egress_ledger.py` writes
(`RECIPIENTS`). Nothing in the server process reaches the network without going
through one of these rows.

| Recipient id | Host | What is sent | When | Credential | Wired today? |
|---|---|---|---|---|---|
| `openrouter` | `openrouter.ai` | The full model request: system prompt, every message in the thread context (user text, assistant text, tool results), the tool schemas, model name, and sampling settings. Document text and quoted excerpts appear inside those messages once a document tool has read them | Every assistant turn | `KELP_PROVIDER_API_KEY` (bearer header) | **Yes**, when the base URL is `https://openrouter.ai/api/v1` — the only content-bearing egress |
| `tavily` | `api.tavily.com` | The search query string, plus the API key header | On a `web.search` call | `TAVILY_API_KEY` | **No** — handler raises `not_implemented` |
| `github` | `api.github.com` | Repository `Anshieee/kelp`, branch/commit metadata, file content, and pull-request title/body; bounded to the manifest's repo/host scope | Only after owner approval of a `github.write` request | `GITHUB_TOKEN` (bearer header) | **Yes**, after approval; the egress ledger records each request without content |
| `stripe` | `api.stripe.com` | Customer id, payment-method id, amount, currency, description, idempotency key. Card numbers are typed into Stripe's own form and never reach this process | On a saved-card charge | `STRIPE_SECRET_KEY` | **No** — `HttpStripeGateway` is never constructed by `build_app` |
| `razorpay` | `api.razorpay.com` | Order amount, currency, receipt (the local order id), a description of at most 200 characters with card numbers and keys masked, and an idempotency key; card ids, customer ids, and payment ids in the URL path or body for saved-card charges and for settlement re-reads. The key id and secret travel only in the `Authorization` header. Card numbers are typed on Razorpay's hosted page and never reach this process | On a hosted Checkout order, a saved-card charge, a card list or attach, and a settlement re-read | Key id + key secret (`RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`) | **Yes**, when both keys are set — `build_app` mounts the routes and `HttpRazorpayTransport` is built on the first payment call. Each call writes one `egress` row (`purpose = "payments.razorpay.<operation>"`, byte counts, outcome, status; no ids, amounts, or credentials). With no keys the routes answer `503 wallet_not_configured` and nothing is sent |
| `unlisted` | whatever else `KELP_PROVIDER_BASE_URL` points at | Exactly what `openrouter` would receive: the whole transcript | Every turn, when the base URL is another host | `KELP_PROVIDER_API_KEY` | Yes, for any endpoint that is not `openrouter.ai` |

Two consequences worth stating plainly:

1. **The model provider sees the conversation.** That is the product: nothing on
   this machine can answer without it. Attachments are read locally, but the text
   the tools quote back travels inside the next model request. If a document must
   not leave the machine, it must not be attached.
2. **`unlisted` is a real recipient, not a detail.** Choosing a base URL other
   than `openrouter.ai` (a gateway, a proxy, or a regional mirror) makes that
   endpoint a recipient of the full transcript. The ledger records it under
   `unlisted` so the choice is visible in the audit trail instead of being
   attributed to a host this repository documents.

### 2.1 The provider health probe

Readiness (`GET /api/readyz`) sends one bounded `GET {base_url}/models` to the
model provider, cached for 30 seconds (`chat/api.py`). It carries the API key in
a header and **no user content**. It is still outbound traffic, so it writes one
ledger row with `purpose = "provider.health"`, `request_bytes = 0`. The row says
what the upstream did: `completed` with the status on a 2xx, or `provider_error`
with the status on a rejection — a failing probe is never filed as a success.

### 2.2 What is not a recipient

- No analytics, metrics exporter, crash reporter, or log shipper. Metrics are
  served locally at `/api/metrics`; nothing pushes them anywhere.
- No external fonts, scripts, or images.
- The evaluation harness (`evals/`) replays recorded turns with the network
  blocked (`evals/offline.py`); it is not a recipient.
- Local embeddings for document search run in-process. They are not an egress
  path.

### 2.3 A share link is a reader, not a recipient

The recipient table above is about outbound calls the server process makes. A share
link is the other direction: an inbound read of a local snapshot, served to whoever
holds the URL.

- **It adds no recipient.** A share read writes no egress row, opens no socket,
  starts no turn, calls no tool, and reads no artifact bytes. The list in §2 is
  unchanged by sharing.
- **It does add a reader.** The owner can hand out a URL
  (`POST /api/threads/{id}/share`, then `GET /api/shared/{token}`; ADR 0002,
  KEL-28) that shows one thread's snapshot to anyone who holds it, with no
  account, until it is revoked or expires. That person receives the owner's
  content. They are not tracked: the audit records the link, not the viewer, and
  aggregate daily view counts are the only usage record. No IP, no user agent, no
  cookie.
- **It is a copy.** Revocation stops the next request; it cannot recall a page
  already delivered to a browser. Sharing is a decision to hand someone a copy, and
  the UI says so.
- **It is live in this build.** The routes are mounted by `chat/api.py` and
  specified in [`docs/CHAT_SPEC.md`](CHAT_SPEC.md) §12.3. This subsection exists so
  that no reader has to guess whether the recipient list grew when sharing shipped.
  It did not.

---

## 3. Where the data lives on this machine

| Data | Location | Removed by | Retention row |
|---|---|---|---|
| Chat content — threads, messages, blocks, events, turns, approvals | `KELP_DB_PATH` (`chat.db`) | `ChatStore.purge_thread` — immediate hard delete, no `deleted_at` shadow | **Chat content** |
| Share snapshot and daily view counts | `KELP_DB_PATH` (`chat.db`), `shares` and `share_views` | `ChatStore.purge_thread` deletes both in the thread's transaction; revoked and expired rows are pruned by the share sweep (`ChatStore.share_sweep`, run by `RetentionMaintenance` at boot and daily), never on the read path. The snapshot holds no artifact bytes, so it has nothing to delete when an artifact is purged | **Chat content** (no separate row) |
| Owner device sessions — hashed session id, short label, timestamps, revocation | `KELP_DB_PATH` (`chat.db`), `sessions` table | `DELETE /api/auth/devices/{id_short}`, `DELETE /api/auth/session(s)`; expired and evicted rows pruned on use | Not in the retention table; bounded by the session TTL and `KELP_SESSION_MAX` |
| Uploaded files | `KELP_ARTIFACT_DIR` (default `workspace/var/artifacts`) | `ArtifactRegistry.purge` when the thread goes; `sweep_orphans` when parentless | **Uploaded files** |
| Derived document index | `workspace/var/index` | Removed eagerly with the artifact; orphan directories swept | **Derived index** |
| Audit records, including the egress ledger | `KELP_AUDIT_DIR/audit.jsonl` and its segments | `RetentionMaintenance.rotate` — 30 days, 40 segments | **Audit records** |
| Usage records — token counts and estimated cost per turn | `KELP_USAGE_DIR/usage.jsonl` and its segments | `RetentionMaintenance.rotate` — 90 days, 100 segments | **Usage records** |
| Spend window state | `KELP_USAGE_DIR/spend-state.json` | Rebuilt from the retained usage records; carries amounts, not text | **Usage records** |
| Request and error logs | Container stdout | Size-bounded by the Compose log driver (10 MiB × 3), no age limit | **Request/correlation logs** |
| Temporary files | `/tmp` (16 MiB, `noexec` tmpfs) | Container lifetime | **Temporary artifacts** |

The row names in the last column are the `class` values of
`chat.retention.RETENTION_ROWS`, mirrored in [`docs/DEPLOY.md`](DEPLOY.md) §8.1.
That table is the contract; this one says which of its rows hold what this review
talks about, so a reader can move between the two without guessing.

**Where a share snapshot sits.** A share link (§2.3) is a second copy of one
thread's content, and its per-day view counter (`share_views`) is a small aggregate
keyed by the link's token hash. Both belong to the **Chat content** row and inherit
that row's rule: they are removed in the same transaction that purges the thread,
and neither is retained after the thread is gone. So sharing adds **no** row to
`chat.retention.RETENTION_ROWS` or to `docs/DEPLOY.md` §8.1. Revoked and expired
links are pruned by the share sweep (boot and daily), never on the read path,
and a revoked row is kept until that prune rather than deleted at revoke. The
snapshot never contains artifact bytes — the projection drops storage paths,
hashes, and artifact handles — so a purged artifact leaves nothing behind inside a
share row.

If share rows are ever given their own retention window, that window must be
added to `RETENTION_ROWS`, `docs/DEPLOY.md` §8.1, and this table in the same
change. The test binding is one-directional, so that change still needs a reviewer:
`tests/test_privacy.py` asserts that every `RETENTION_ROWS` class name appears in
this file, and nothing parses the §8.1 rows.

Three properties matter for the privacy claim:

- **Delete means delete, and `deleted_at` is not a delete.** Purging a thread removes
  its rows in one transaction and unlinks its files. `threads` and `artifacts` carry a
  `deleted_at` column that the soft-delete paths (`ChatStore.soft_delete_thread`) use
  as a read filter; `purge_thread` is the privacy-relevant delete, it removes the rows
  outright, and nothing is left flagged. A live turn makes the purge answer `409
  turn_in_progress` instead of racing it, so a dead turn cannot make a thread
  undeletable and a running one cannot write into rows that no longer exist.
- **The logs are scrubbed, not trusted.** Every string written to the audit log
  passes a scrubber that replaces known secret values, `Bearer …` tokens,
  `sk_live_…` / `sk_test_…` keys, and `token=…` / `password: …` pairs before
  serialization. Command output is stored as a SHA-256 hash and a length, never as
  a body.
- **Sessions store no credential.** The `sessions` row holds the SHA-256 of the
  session id and a derived CSRF token is never written at all; neither value can
  be lifted from `chat.db` and replayed as a cookie. Device labels are owner-visible
  data in the list response, and no log or audit line carries them.

---

## 4. The egress ledger

`openshell/egress_ledger.py` writes one `egress` record per outbound call. Each
record carries **recipient, purpose, request bytes, response bytes, method, host,
and outcome** — and nothing else. Two optional fields are filled in only by the
call sites that know them: the **HTTP status** and the **turn id**. Skill egress
writes both; the model client writes the outcome (`provider_error` on a rejected
status) but no numeric status; a readiness probe belongs to no turn, so it has
neither. Absent is better than invented. None of these is a free-text field: a
host that is not a hostname is recorded as `unresolved` instead of being copied,
and there is no field that could hold a body, query, path, header value, file
name, or prompt, so a caller cannot put content in it even by mistake.

Three properties make it trustworthy as an account rather than a log of good
intentions:

1. **It is the same trail as the audit log.** Ledger rows go through
   `openshell.audit.AuditLog`, so they inherit the `0600` file mode, mandatory
   secret scrubbing, rotation, and the 30-day / 40-segment retention row above.
   There is no second file to forget and no second clock to drift.
2. **Recipients are a closed set, and so are destinations.** `EgressLedger.record`
   rejects an unknown recipient id, the host-to-recipient mapping is an exact
   match, so a look-alike domain cannot be reported under a trusted name, and a
   host field is validated against a hostname grammar — a URL with a path or a
   query becomes `unresolved` rather than being written into the trail.
3. **It fails open on accounting, never on traffic.** A ledger write error is
   swallowed; a missing ledger is a no-op. The ledger never blocks a request, the
   same rule the rest of the audit trail follows.

Coverage is by path, not by hope. Four call sites write rows in this build: the
model client (`purpose = "chat.completions"`), each skill's egress chokepoint
(`purpose =` the skill name), the readiness probe (`provider.health`), and the
Razorpay transport (`purpose = "payments.razorpay.<operation>"`, one of
`customer.create`, `card.fetch`, `card.list`, `order.create`, `payment.fetch`,
`payment.create`, `payment.capture`). `build_app` hands its egress ledger to both
the wallet and the hosted Checkout, so the lazily built `HttpRazorpayTransport`
writes into the same audit trail; the row's outcome is `completed`,
`provider_error` (HTTP 400 and up), `unreachable`, `timeout`, or `error` (an
unexpected client-side fault), and a call
refused before it reaches the network (a missing credential, a malformed id)
writes nothing because nothing left. The Stripe transport is the one place a
future change could add a recipient without a row: `HttpStripeGateway` is not
constructed by `build_app` today, the `stripe` id already exists so the day it is
wired the gap is visible, and §2's table must be updated in that same change.

Read it like any other audit record:

```python
from openshell import AuditLog, EgressLedger

ledger = EgressLedger(AuditLog("var/audit/audit.jsonl"))
for row in ledger.entries():  # or entries(turn_id="turn_01H…") for a skill call
    print(row["extra"]["recipient"], row["extra"]["purpose"],
          row["extra"]["request_bytes"], row["extra"]["response_bytes"])
```

In production the process builds one ledger in `chat.server.build_app` and shares
it with the model client; each skill's egress client builds one from the same
audit log. So all three content paths — model turns, skill egress, and the
provider readiness probe — land in one place.

**What the ledger does not do.** It records sizes, not destinations in detail: it
cannot tell you *which* document was attached, and it is not a data-loss-prevention
filter. It is an account of who received how much, which is what the privacy claim
needs.

---

## 5. Exec boundary: no command runs in this build

**Decision: remove the promise, not wire the gate.** An approval card with the
`execute` effect previously said "It runs once". Nothing in this build runs a
command: `chat.server.build_app` never constructs `openshell.ExecGate`, no skill
manifest may declare `execute` (the loader rejects it outright), and `shell.exec`
is not in the model-facing tool list. The card now says what is true instead.

| Fact | Where it is enforced |
|---|---|
| Approving an exec card records a decision and executes nothing | `ChatService._decide_exec` returns `executed: false`; no gate is wired |
| No manifest can request `execute` | `skills/loader.py` rejects the effect in v1.0 |
| `shell.exec` is never offered to the model | The tool list is derived from manifests (`chat/server.py::_tool_specs`); no manifest declares it |
| The card copy cannot promise a run that will not happen | `chat/cards.py::EXEC_AVAILABLE` is `False`; `approval_rationale` follows it |

The gate itself is not deleted: `openshell/exec_gate.py` is a reviewed, tested
capability (argv-only, allowlisted `argv[0]`, single-use approval, minimal
environment, bounded and scrubbed output, no `shell=True`). Wiring it is a
deliberate, separate change:

1. construct a `PolicyEngine` with `allow_exec=True` and an `argv[0]` allowlist,
   plus an `ExecGate` over the existing `ApprovalStore` and `AuditLog`, in
   `build_app`;
2. call `gate.execute()` on an approved `openshell`-sourced card and record the
   result;
3. add it to the tool list — and only then flip
   `chat/cards.py::EXEC_AVAILABLE` back to `True`.

`tests/test_privacy.py` enforces the invariant in both directions: the flag is
`False` and no gate is reachable from the application, or the flag is `True` and
the gate must be wired. The wiring check parses `build_app` rather than grepping
it, and a second test builds the app with `ExecGate.__init__` booby-trapped, so a
newly constructed gate fails at construction time. It cannot be flipped as a copy
change alone.

The approved `github.write` skill is a separate, narrowly-scoped network write:
its manifest allows only `api.github.com` and `Anshieee/kelp`, and the owner must
approve the exact request before the handler can run. Approval does not enable
local command execution: `chat.cards.EXEC_AVAILABLE` remains `False`, the server
does not construct `ExecGate`, and no local commands run.

**Why this side of the decision.** Wiring the gate would have added a
process-spawning capability to the same change that writes the privacy review,
with no model-facing caller to exercise it — new attack surface with no new
function. The honest card is smaller, reversible, and testable, and it removes a
false promise from a product whose pitch is that it does not overstate what it
does.

---

## 6. What this review does not promise

- **The provider is trusted with the transcript.** TLS protects the hop, not the
  recipient. The provider's own retention is outside this repository; this
  document does not claim anything about it.
- **Attachments leave via the model request.** Document tools read locally and
  send nothing, but the excerpts they return become part of the next model
  request.
- **The ledger is an account, not a filter.** It records what left; it does not
  prevent a send.
- **Request logs are size-bounded, not age-bounded.** The Compose log driver is
  configured per size; a day-based claim would need a change in both the Compose
  file and the runbook. The retention table says so rather than pretending.
- **Chat content has no age timer.** It is removed when the owner deletes the
  thread. The retention table states this instead of implying an expiry.
- **Razorpay is wired; Stripe is not.** With both Razorpay keys set the server
  calls `api.razorpay.com`, and every call is accounted for as a `razorpay` row
  (counts and fixed labels only). The row does not say what was bought or for how
  much; the amount, the description, and the ids are in the wallet's own
  database, not the audit trail. Setting only one of the two keys is a startup
  configuration error, not a half-working wallet. If `HttpStripeGateway` is ever
  constructed by `build_app`, `stripe` becomes a live recipient and §2's "Wired
  today?" column must change with it; `tests/test_privacy.py` fails if the
  constructor runs.
- **A shared thread is deliberately readable by a stranger.** Because the share
  surface is live, the owner can hand a URL to one thread and the holder can read it
  with no account. That is the feature, not a leak: the content is still the owner's,
  the model provider already has it, and the only promise is that the link dies on
  revocation or expiry. The page cannot be un-rendered once it has been seen.

---

## 7. Verification

`tests/test_privacy.py` checks the parts of this document that can be checked:

- every recipient id in `openshell.egress_ledger.RECIPIENTS` is named in this
  file, and every recipient named in §2 is a known id — both directions; the §2
  host column and the hosts in `RECIPIENTS` are the same set;
- every module under `chat/`, `kernel/`, `skills/`, `openshell/`, and
  `payments/` is scanned for a hardcoded `https://` endpoint, so a new module is
  covered without editing a list;
- every `class` value in `chat.retention.RETENTION_ROWS` appears in §3; the reverse
  direction — a row left in §3 after the code drops it — is not test-bound, and
  `docs/DEPLOY.md` §8.1 is mirrored by hand, so both are checked at review;
- a model turn writes an `egress` row with byte counts and no content, including
  through the real `ProviderClient` stream path;
- a skill egress call writes a `tavily` / `github` row, and a policy denial still
  writes a row with zero response bytes;
- the readiness probe records `completed` with its status on 2xx and
  `provider_error` with the status on a rejection;
- no ledger row contains transcript text, a query string, a file name, or a
  secret value; the ledger rejects an unknown recipient and a free-text purpose,
  and a host that carries text is stored as `unresolved`;
- `chat.cards.EXEC_AVAILABLE` and the wiring agree under AST inspection, and
  `build_app` is run with `ExecGate.__init__` booby-trapped, so a newly wired
  gate fails the test at construction time;
- an exec card makes no execution promise;
- the Razorpay transport writes exactly one content-free `razorpay` row per HTTP
  call (`completed`, `provider_error` with the status, `unreachable`, `timeout`),
  a failing ledger never breaks the call, and `build_app` hands the app's own
  egress ledger to both the wallet and hosted Checkout gateways while keeping the
  transport lazy; with no keys no transport is constructed and the routes answer
  `503 wallet_not_configured`;
- `build_app` never constructs `HttpStripeGateway` (booby-trapped constructor),
  and the §2 "Wired today?" cells for `razorpay` (**Yes**) and `stripe` (**No**)
  match.

The retention behaviour itself is covered by `tests/test_retention.py`; this
review references that table rather than restating it.

---

## 8. Cross-references

- Retention table and its clocks: [`docs/DEPLOY.md`](DEPLOY.md) §8.1
- Retention policy source: [`chat/retention.py`](../chat/retention.py)
- Policy gate and audit log: [`docs/OPENSHELL.md`](OPENSHELL.md)
- Chat surface, approvals, and the security section: [`docs/CHAT_SPEC.md`](CHAT_SPEC.md) §12
- Sharing and identity, including what a share link does and does not do:
  [`docs/adr/0002-sharing-and-identity.md`](adr/0002-sharing-and-identity.md)
- Control-layer contract (tenants, provider seam, run records, gate order, and
  admin read-model exclusions): [`docs/SERVICE_CONTRACT.md`](SERVICE_CONTRACT.md)
  and [`docs/adr/0003-service-control-layer.md`](adr/0003-service-control-layer.md)
- Skill contract and the `execute` prohibition: [`docs/SKILL_SPEC.md`](SKILL_SPEC.md)
- Egress ledger: [`openshell/egress_ledger.py`](../openshell/egress_ledger.py)
- Exec gate: [`openshell/exec_gate.py`](../openshell/exec_gate.py)
- Tests: [`tests/test_privacy.py`](../tests/test_privacy.py), [`tests/test_retention.py`](../tests/test_retention.py)

## Optional private credential channel

When `KELP_CREDENTIALS_ENABLED=1`, owner input sent to the separate credential
API is encrypted in `credentials.db` beside `chat.db`. It is never appended to
chat, tool results, SSE replay, shares or provider requests. A separately
authenticated trusted browser worker receives it once and enters it into the
explicit HTTPS site. Its bootstrap token (`KELP_BROWSER_WORKER_TOKEN`) can only
register a request. Registration issues a token signed with a separate server-only
`KELP_CREDENTIAL_SIGNING_KEY` and scoped to that one request's status read, claim
and finish. Claim and finish stop at the ten-minute request deadline; the status
read remains for the 24-hour retention window so the worker can see the owner's
release. Device identity is derived by the
server from the bootstrap credential, not accepted from the worker body. The
request ciphertext is bound to its ID, target origin, browser session, authenticated
device, form fingerprint, and field roles; a changed binding fails closed. These
handoff tokens and entered values are never copied into history, results, SSE,
logs, or audit records. Provider credentials remain in the chat server's protected
environment and existing provider wiring; they are not sent to the browser worker.
That destination site receives the entered values; the trusted server and worker
briefly hold plaintext. Optional saved values remain encrypted until explicit
reuse or deletion; verification codes cannot be saved. See
[`CREDENTIAL_HANDOFF.md`](CREDENTIAL_HANDOFF.md) for the complete trust boundary,
retention rules and limits. This optional worker is a separate process from the
chat server and must pause agent observations during private entry. The earlier
recipient table describes the chat server's provider/skill egress, not this
new owner-to-worker-to-website credential path.

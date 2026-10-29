# Privacy review — what leaves the machine, and what does not (KEL-22 / M3-E)

- **Status:** written review, 2026-10-30
- **Branch:** `kel-22`
- **Scope:** the server process in `chat/`, `kernel/`, `skills/`, `openshell/`, and `payments/`
- **Companions:** the retention rule ([`docs/DEPLOY.md`](DEPLOY.md) §8.1, `chat/retention.py`),
  the policy gate ([`docs/OPENSHELL.md`](OPENSHELL.md)), the chat spec
  ([`docs/CHAT_SPEC.md`](CHAT_SPEC.md) §12)
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
| Where does the conversation go? | Nebius Token Factory, as the model provider — every turn, in full |
| Does anything else receive conversation text? | Not by default: no analytics, no telemetry, no crash reporter, no CDN, no third-party log sink |
| Do skills reach the network? | Only through one chokepoint, and only to hosts a manifest declares. `web.search` (Tavily) and `github.read` (GitHub) are declared but their handlers are not implemented; today they send nothing |
| Do payments reach the network? | Not in this build. The Stripe and Razorpay clients exist but `build_app` constructs neither |
| Where are keys? | Only in the server process environment. `NEBIUS_API_KEY`, `TAVILY_API_KEY`, `GITHUB_TOKEN` are read from `os.environ`, sent only to their own recipient, and scrubbed from every log line |
| What records exist? | Chat content until the owner deletes the thread; audit and usage records on a stated clock; a content-free egress ledger inside the audit trail |
| Can I audit what left? | Yes: one `egress` line per outbound call — recipient, purpose, byte counts, outcome, no content |

---

## 2. Every outbound recipient

The recipient ids are the ones `openshell/egress_ledger.py` writes
(`RECIPIENTS`). Nothing in the server process reaches the network without going
through one of these rows.

| Recipient id | Host | What is sent | When | Credential | Wired today? |
|---|---|---|---|---|---|
| `tokenfactory` | `api.tokenfactory.nebius.com` | The full model request: system prompt, every message in the thread context (user text, assistant text, tool results), the tool schemas, model name, and sampling settings. Document text and quoted excerpts appear inside those messages once a document tool has read them | Every assistant turn | `NEBIUS_API_KEY` (bearer header) | **Yes** — the only content-bearing egress |
| `tavily` | `api.tavily.com` | The search query string, plus the API key header | On a `web.search` call | `TAVILY_API_KEY` | **No** — handler raises `not_implemented` |
| `github` | `api.github.com` | The repository `owner/name`, and the issue, pull-request, or file path in the URL; `GET` only | On a `github.read` call | `GITHUB_TOKEN` (bearer header) | **No** — handler raises `not_implemented` |
| `stripe` | `api.stripe.com` | Customer id, payment-method id, amount, currency, description, idempotency key. Card numbers are typed into Stripe's own form and never reach this process | On a saved-card charge | `STRIPE_SECRET_KEY` | **No** — `HttpStripeGateway` is never constructed by `build_app` |
| `razorpay` | `api.razorpay.com` | Order amount, currency, receipt, and the key id in the auth header; webhook settlement re-reads the order by id. Card numbers are typed on Razorpay's hosted page | On a hosted Checkout order | Key id + key secret | **No** — `HttpRazorpayTransport` is never constructed by `build_app` |
| `unlisted` | whatever `NEBIUS_TOKEN_FACTORY_BASE_URL` points at | Exactly what `tokenfactory` would receive: the whole transcript | Every turn, when an operator overrides the base URL | `NEBIUS_API_KEY` | Only if the operator sets the override |

Two consequences worth stating plainly:

1. **The model provider sees the conversation.** That is the product: nothing on
   this machine can answer without it. Attachments are read locally, but the text
   the tools quote back travels inside the next model request. If a document must
   not leave the machine, it must not be attached.
2. **`unlisted` is a real recipient, not a detail.** Pointing
   `NEBIUS_TOKEN_FACTORY_BASE_URL` at a gateway or a proxy makes that endpoint a
   recipient of the full transcript. The ledger records it under `unlisted` so the
   override is visible in the audit trail instead of being reported as Nebius.

### 2.1 The provider health probe

Readiness (`GET /api/readyz`) sends one bounded `GET {base_url}/models` to the
model provider, cached for 30 seconds (`chat/api.py`). It carries the API key in
a header and **no user content**. It is still outbound traffic, so it writes one
ledger row with `purpose = "provider.health"`, `request_bytes = 0`.

### 2.2 What is not a recipient

- No analytics, metrics exporter, crash reporter, or log shipper. Metrics are
  served locally at `/api/metrics`; nothing pushes them anywhere.
- No external fonts, scripts, or images. The tracker page in `netlify/` is static
  and links out only; it loads no third-party asset.
- The evaluation harness (`evals/`) and the demo fixtures replays recorded turns
  with the network blocked (`evals/offline.py`); they are not recipients.
- Local embeddings for document search run in-process. They are not an egress
  path.

---

## 3. Where the data lives on this machine

| Data | Location | Removed by | Retention row |
|---|---|---|---|
| Chat content — threads, messages, blocks, events, turns, approvals | `KELP_DB_PATH` (`chat.db`) | `ChatStore.purge_thread` — immediate hard delete, no `deleted_at` shadow | **Chat content** |
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

Two properties matter for the privacy claim:

- **Delete means delete.** Purging a thread removes its rows in one transaction
  and unlinks its files. A live turn makes the purge answer `409 turn_in_progress`
  instead of racing it, so a dead turn cannot make a thread undeletable and a
  running one cannot write into rows that no longer exist.
- **The logs are scrubbed, not trusted.** Every string written to the audit log
  passes a scrubber that replaces known secret values, `Bearer …` tokens,
  `sk_live_…` / `sk_test_…` keys, and `token=…` / `password: …` pairs before
  serialization. Command output is stored as a SHA-256 hash and a length, never as
  a body.

---

## 4. The egress ledger

`openshell/egress_ledger.py` writes one `egress` record per outbound call. Each
record carries **recipient, purpose, request bytes, response bytes, method, host,
outcome, HTTP status, and the turn id** — and nothing else. The API has no field
that could hold a body, query, path, header value, file name, or prompt, so a
caller cannot put content in it even by mistake.

Three properties make it trustworthy as an account rather than a log of good
intentions:

1. **It is the same trail as the audit log.** Ledger rows go through
   `openshell.audit.AuditLog`, so they inherit the `0600` file mode, mandatory
   secret scrubbing, rotation, and the 30-day / 40-segment retention row above.
   There is no second file to forget and no second clock to drift.
2. **Recipients are a closed set.** `EgressLedger.record` rejects an unknown
   recipient id, and the host-to-recipient mapping is an exact match, so a
   look-alike domain cannot be reported under a trusted name.
3. **It fails open on accounting, never on traffic.** A ledger write error is
   swallowed; a missing ledger is a no-op. The ledger never blocks a request, the
   same rule the rest of the audit trail follows.

Coverage is by path, not by hope. Three call sites write rows in this build: the
model client (`purpose = "chat.completions"`), each skill's egress chokepoint
(`purpose =` the skill name), and the readiness probe (`provider.health`). The
Stripe and Razorpay transports are the one place a future change could add a
recipient without a row: they are not constructed today, the `stripe` and
`razorpay` ids already exist so the day they are wired the gap is visible, and
§2's table must be updated in that same change.

Read it like any other audit record:

```python
from openshell import AuditLog, EgressLedger

ledger = EgressLedger(AuditLog("var/audit/audit.jsonl"))
for row in ledger.entries(turn_id="turn_01H…"):
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
the gate must be wired. It cannot be flipped as a copy change alone.

**Why this side of the decision.** Wiring the gate would have added a
process-spawning capability to the same change that writes the privacy review,
with no model-facing caller to exercise it — new attack surface with no new
function. The honest card is smaller, reversible, and testable, and it removes a
false promise from a product whose pitch is that it does not overstate what it
does.

---

## 6. What this review does not promise

- **The provider is trusted with the transcript.** TLS protects the hop, not the
  recipient. Token Factory's own retention is outside this repository; this
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
- **Payments are not wired.** When they are, `stripe` and `razorpay` become live
  recipients; the ledger ids already exist so that change shows up in the audit
  trail on day one, but §2 will need its "Wired today?" column updated with it.
- **The static tracker page** in `netlify/` links out to Linear and GitHub when a
  reader clicks. That is a browser navigation, not a server egress, and it loads
  no third-party asset.

---

## 7. Verification

`tests/test_privacy.py` checks the parts of this document that can be checked:

- every recipient id in `openshell.egress_ledger.RECIPIENTS` is named in this
  file, and every recipient named in §2 is a known id — both directions;
- the retention row names used in §3 are exactly the `class` values in
  `chat.retention.RETENTION_ROWS`;
- a model turn writes an `egress` row with byte counts and no content, including
  through the real `TokenFactoryClient` stream path;
- a skill egress call writes a `tavily` / `github` row, and a policy denial still
  writes a row with zero response bytes;
- no ledger row contains transcript text, a query string, a file name, or a
  secret value, and the ledger rejects an unknown recipient and a free-text
  purpose;
- `chat.cards.EXEC_AVAILABLE` and the wiring agree, and an exec card makes no
  execution promise.

The retention behaviour itself is covered by `tests/test_retention.py`; this
review references that table rather than restating it.

---

## 8. Cross-references

- Retention table and its clocks: [`docs/DEPLOY.md`](DEPLOY.md) §8.1
- Retention policy source: [`chat/retention.py`](../chat/retention.py)
- Policy gate and audit log: [`docs/OPENSHELL.md`](OPENSHELL.md)
- Chat surface, approvals, and the security section: [`docs/CHAT_SPEC.md`](CHAT_SPEC.md) §12
- Skill contract and the `execute` prohibition: [`docs/SKILL_SPEC.md`](SKILL_SPEC.md)
- Egress ledger: [`openshell/egress_ledger.py`](../openshell/egress_ledger.py)
- Exec gate: [`openshell/exec_gate.py`](../openshell/exec_gate.py)
- Tests: [`tests/test_privacy.py`](../tests/test_privacy.py), [`tests/test_retention.py`](../tests/test_retention.py)

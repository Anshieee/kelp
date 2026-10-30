# ADR 0002 — Sharing and identity: one owner, per-thread capability links

- **Status:** Accepted
- **Date:** 2026-10-30
- **Issue:** KEL-26 (M4-E). The decision requested by the issue text.
- **Branch:** `kel-26`
- **Related:** KEL-28 (the implementation slice this ADR specifies), KEL-24 (device
  sessions), KEL-23 / KEL-27 (scheduler, not yet merged), KEL-25 (plugins, not yet
  merged), KEL-17 (rate classes), KEL-18 (spend ceilings), KEL-19 (owner sign-in),
  KEL-21 (retention), KEL-20 (invoke-time policy), `docs/PRIVACY.md`,
  `docs/CHAT_SPEC.md` §12
- **Decision log:** no existing Q-decision changes, so this ADR adds no Q-number.
  It settles a question the decision log never covered, which is what an ADR is for.

## 1. Context

Everything in the shipped build assumes **exactly one principal**. This is not a
default that happens to be set; it is designed into five separate places, and each
one is load-bearing:

| Invariant | Where it lives | What it assumes |
|---|---|---|
| One credential family | `chat/api.py` (`SESSION_COOKIE`, `SessionStore`, `_authorize`) | The root bearer `KELP_AUTH_TOKEN` and every session minted from it are the same principal. |
| One limiter identity | `chat/api.py` (`OWNER_IDENTITY = "owner"`) | A second tab must not double the owner's allowance, so credentials are not the limiter key. |
| One spend budget | `kernel/usage.py` (`SpendGuard`, `SPEND_CAP_DAY_ENV`, `SPEND_CAP_MONTH_ENV`) | Every turn bills the same wallet; the day and month ceilings are machine-wide. |
| No owner column | `chat/store.py` (`_SCHEMA`) | `threads`, `messages`, `blocks`, `events`, `turns`, `approvals`, and `artifacts` carry no principal reference at all. |
| Approval means "the owner said yes" | `openshell/approvals.py` (`create(actor="owner")`, `approve(actor="owner")`), `chat/service.py` (`"actor": "owner"`) | The only party who can satisfy a gate is the owner, and the audit says so as a literal string. |

A sixth invariant is narrower than it looks: `ArtifactRegistry.get(handle,
thread_id=...)` in `chat/artifacts.py` scopes an artifact to a **thread**, not to a
principal, and a mismatched `thread_id` is deliberately indistinguishable from an
unknown handle. Artifact access control is therefore "knows the thread id", which is
only sound while knowing a thread id and being the owner are the same thing.

The audit trail already has the field this ADR needs: `openshell/audit.py`'s
`AuditLog.record(..., actor=...)` accepts a free-text actor, and
`skills/loader.py::invoke(..., actor=...)` threads it through skill calls. Nothing
constrains the vocabulary or adds an "on behalf of" today, because there has never
been anyone but the owner to name.

### 1.1 What is actually shipped, at 30 October 2026

M4-C is merged (`kel-24` at `a9814ff`): owner sessions are durable rows in
`chat.db`, listable and revocable per device. M4-A (`KEL-23`), M4-B (`KEL-27`), and
M4-D (`KEL-25`) are **not merged** — they are `Todo` in the tracker. Two
consequences for this ADR, stated plainly because the issue asked for consistency
with shipped behaviour:

1. The scheduled/system principal does **not** exist in code yet. Its rules below
   bind KEL-23 and KEL-27; they are not a description of something already built.
2. Nothing in the share surface can ship before this ADR is merged, because the
   slice (`KEL-28`) needs the token, scope, and revocation semantics decided here.

**Update, 30 October 2026 — after merge.** This ADR was written before `kel-23`
and `kel-25` joined `main`. Both are now merged (`13fc687` and `08f87e5`), so the
scheduler store and the plugin loader exist as code; the scheduled/system
principal is still not *running*, because the server does not start the ticker
yet (that is KEL-27). The rules in §4 and §5 for that principal therefore still
describe a binding contract rather than shipped behaviour, and the plugin
principal's constraints are now enforced by the digest pin described in
`docs/SKILL_SPEC.md` §15.1. Nothing else in this ADR changes.

## 2. The question

Sharing is the most expensive thing in M4 to undo. A wrong choice here is not a bad
screen; it is a data-ownership model that has to be migrated out of a database
while it holds someone's conversations. So the decision is taken in writing, before
any share code exists, and it answers one question:

> When someone other than the owner touches a thread, what are they, what may they
> do, who pays, who can stop them, and what does the record say?

## 3. Decision

**Admit exactly one principal — the owner — and give everyone else nothing but a
per-thread, revocable, read-only capability.**

- There is no workspace, no membership, no invitation, no role, and no second
  credential family in the shippable slice. "Named collaborator" is a principal
  this ADR **defines and does not admit**; every one of its capabilities is "none",
  so a request that presents one is simply an unauthenticated request.
- The only way a non-owner reads anything is a share link: a 256-bit random token
  in a URL, bound to exactly one thread and one snapshot boundary, stored only as a
  hash, expiring, revocable, and GET-only.
- A share link is a **capability, not an identity**. It authenticates nobody. It
  authorizes one read of one frozen snapshot and nothing else — no model call, no
  tool call, no artifact bytes, no approval, no write, no second thread.
- Every principal that can cause work — the owner, a scheduled run, a plugin —
  bills the owner, spends the owner's credentials, and is approved only by the
  owner. Delegation changes who *asked*, never who *pays* or who *approves*.

The rest of this document is the enforceable detail: the principal table with no
blank cells, the approval matrix, attribution, limits, revocation, migration, and
the threat model.

## 4. Principals

Five principals exist in the model. Two are implemented, one is a capability rather
than an identity, and two are named so that a future change has a target to hit
rather than a blank space to fill with a guess.

| Principal | Authenticates as | Reads | Writes | Credentials a delegated action uses | Spend billed to | Revocable by | Audit says |
|---|---|---|---|---|---|---|---|
| **Owner** (interactive) | Root bearer `KELP_AUTH_TOKEN`, or a session cookie minted from it (KEL-19/KEL-24) | Every thread, every artifact, every approval, the activity log, metrics with auth | Everything: turns, approvals, device revocation, share creation and revocation | Its own; the provider key is process environment, never the cookie | The owner | The owner: log out one device, all devices, or rotate the root token | `actor="owner"`, plus the credential class and the session's short id hash |
| **Owner device session** | One opaque session cookie; `sessions` row holds only the SHA-256 | Same as the owner — it *is* the owner | Same as the owner | Same as the owner | The owner | Any other owner session, or the owner from any device | `actor="owner"`, `credential="session"`, `id_short` of the hashed id |
| **Anonymous share viewer** | Possession of the token in `/s/{token}`. No account, no cookie, no header | One frozen snapshot of one thread: messages, blocks, and citation metadata. Never artifact bytes, never another thread, never an approval card | Nothing. Every non-GET is `405` before any lookup | None. A share read has no provider key and makes no outbound call | Nobody: a share read spends $0.00 | The owner, per link, or all links for the thread, or by deleting the thread | `actor="share"`, `token_hash`, `event="share_viewed"` aggregated per day — no viewer identity, no IP, no user agent |
| **Scheduled / system actor** (KEL-23, KEL-27) | No credential. It is an in-process turn started by the ticker for a schedule row the owner created | The target thread, exactly as the owner would | Starts and settles a turn; calls skills subject to the same policy gate. **Cannot approve anything** | The owner's provider key from the process environment; never a session | The owner. One budget, no side ledger (KEL-27) | The owner: pause or delete the schedule; a failure breaker pauses it automatically | `actor="schedule"`, `on_behalf_of="owner"`, `origin_ref={schedule_id, run_id}`; the process settling an expiry writes `actor="system"` (§6) |
| **Plugin handler** (KEL-25) | No credential of its own. It is third-party code invoked inside the owner's turn, loaded by path from a digest-pinned package | Only the arguments the policy gate allows for its declared scope | Only what its manifest declares, and only behind the same approval gate as a builtin | The skill's declared credential from the process environment, scoped to its declared host | The owner — it runs inside the owner's turn, on the owner's token budget | The owner: uninstall the package (a restart is required, KEL-25) | `actor="plugin"`, `extra.plugin=<name>`, `extra.plugin_digest=<prefix>` |
| **Named collaborator** (defined, **not admitted**) | Would be an invited credential in a second identity family. Today there is no column, no route, and no invite: any such credential is an unauthenticated request | None | None | n/a — it cannot act | Nobody — it cannot cause work | n/a — it cannot exist | `auth_failed` with a hashed identity, exactly like any bad credential |

Two rules fall out of that table and are worth stating as rules rather than cells:

1. **A device is not a principal.** KEL-24's whole point is that the owner's devices
   are peers: any of them can revoke any other, and an approval given on the phone
   satisfies a gate opened on the laptop. The principal is `owner`; the device is a
   credential with a short id.
2. **A plugin is not a principal either.** It has no identity of its own to leak,
   because it never holds a credential. It borrows the owner's turn, and the thing
   that constrains it is the manifest's declared effects and scopes plus the
   approval gate — not a permissions table. "Plugin identity" (a per-plugin
   credential or a per-plugin sub-cap) is an explicit non-decision, §13.

## 5. Approval authority

The rule is one sentence: **only an owner-authenticated credential, presented at the
moment of decision, can satisfy a gate, and it must arrive as a mutation with a
matching `Origin` and CSRF pair.**

| Requesting actor | Sees the approval card | Can satisfy the gate | What happens instead |
|---|---|---|---|
| Owner, interactive turn | Yes, in its own thread | **Yes** | The existing path: `POST /api/approvals/{id}/decision`, CSRF + `Origin` checked by `_authorize` |
| Owner device A, turn started on device B | Yes, on any device | **Yes** — same principal | Same path; the audit records which session's `id_short` decided |
| Scheduled / system actor | Yes, in its thread | **Never** | The run parks in `paused_needs_approval` with a reason code and stops advancing. It cannot self-approve, retry into an approval, or widen its own scope |
| Plugin handler | Yes — the card is the owner's | **Never** | The call parks exactly like a builtin tool that needs approval. A plugin cannot approve, and cannot synthesize an approval by returning a payload |
| Anonymous share viewer | **No.** Approval cards are excluded from the snapshot | **Never** | The share route is GET-only and has no decision endpoint; a non-GET is `405` before any lookup |
| Named collaborator | n/a | **Never**, even if collaborators are admitted later | Stated here so a future change cannot quietly grant it: **a collaborator may never approve a spend or a write** |

Three corollaries, because they are the cases that get implemented wrong:

- **A spend block is not an approval.** `SpendGuard` refuses *before* the provider
  call and records `spend_cap_blocked`. There is no card and no override: an
  approval cannot buy past a ceiling. This follows KEL-20's rule that a policy
  refusal is terminal with no approval path, and it is why "whose spend" matters
  more than "whose approval".
- **Expiry, not silence, settles a gate.** An unresolved approval expires on its TTL
  and the turn settles accordingly; a parked scheduled run does not hold a slot
  forever waiting for a human.
- **The decision is attributed to the decider, not the requester.** The request
  records `actor="schedule"`, `on_behalf_of="owner"`, and
  `origin_ref={schedule_id, run_id}`. The decision records `actor="owner"` with
  **no** `on_behalf_of` — the owner borrows nobody's authority — and no
  `origin_ref`; the two records are correlated by `turn_id` and the approval id.
  Both records exist, and only the request claims delegation.

## 6. Attribution

`AuditLog.record` already carries `actor` and an `extra` mapping. It has no
`on_behalf_of` or `origin_ref` parameter, and nothing writes those keys today.
They ride in `extra` rather than as new columns — `extra` is already persisted and
already carries structured keys such as the egress ledger's `recipient` and
`purpose` — so no schema change is required. The
vocabulary below becomes closed **at the two chokepoints that write attribution**,
`openshell/audit.py::AuditLog.record` and `skills/loader.py::invoke`, and the
issues named in the last column are the only ones that may introduce a value:

| Field | Meaning | Values | Written by |
|---|---|---|---|
| `actor` | Who caused this record | `owner`, `system`, `schedule`, `plugin`, `share` | `owner` today; `system` for expiry events (conversion in §15); `schedule` by KEL-23/KEL-27; `plugin` by KEL-25; `share` by KEL-28 |
| `on_behalf_of` | Whose authority was borrowed, when it was not the actor's own | `owner` (the only value in a single-owner build) | KEL-23/KEL-27 for `schedule`; KEL-25 for `plugin`. A `share` record borrows no authority and writes no value |
| `origin_ref` | The specific delegate, when `actor` is not `owner` | `{schedule_id, run_id}`, `{plugin, plugin_digest}`, or `{token_hash}` — never a raw token | KEL-23/KEL-27, KEL-25, and KEL-28, each matching its own `actor` |

Rules:

- **Never a free-text actor.** An unknown `actor` value is a bug, not a label. The
  five values above are the only ones any call site may write, and the rule is
  enforced at `AuditLog.record` and `skills/loader.py::invoke` — not retroactively
  on every historical record. `owner` is written by `chat/service.py`,
  `openshell/approvals.py`, and the auth paths. `system` is the value for the
  process settling an expiry or a recovery: `chat/recovery.py` already writes
  `actor="system"` into its `approval_expired` *event payload*, while the audit line
  it writes passes no `actor` at all. That unattributed audit record is the one
  conversion gap, named in §15. No call site writes `agent` today, so an `agent`
  value appearing later is a bug in the change that added it, and converting one is
  that change's job.
- **`system` is an actor, not a principal.** It names the process settling an
  expiry or a recovery, which cannot be asked to do anything. The principal count
  in §3 stays five.
- **An omitted actor is not a value.** `skills/loader.py::invoke` takes
  `actor=None` by default; such a record is unattributed, and every calling issue
  named above must pass one.
- **The delegation pair is closed too.** `on_behalf_of` has one value, and
  `origin_ref` has one shape per actor. Neither is free text: a string that is not
  one of the listed shapes is a bug, not an extra detail.
- **`credential` and `id_short` are not part of this vocabulary.** KEL-24 already
  writes them in `extra` on owner records to name the *credential* (root bearer or
  device session) and the session's hashed short id. They do not change `actor`,
  which stays `owner` for both.
- **A share view is attributed to the link, not the person.** The record is
  `actor="share"` plus `token_hash`. There is no viewer identity to log, and none is
  invented: no IP, no user agent, no cookie, no fingerprint. Aggregate daily counts
  live in their own table so per-request identity logging is never needed to answer
  "was this link used?".
- **A share read does not write an egress row.** The egress ledger records outbound
  calls; a share read is an inbound read of a local snapshot. Its accounting is the
  view counter and the access log, not the ledger.
- **No credential material, ever.** The existing scrubber and allow-list already
  enforce this for logs (`chat/logging.py::_ALLOWED_FIELDS` has `route` and `path`
  is absent), and the share route must not become the exception that adds one.

## 7. Limits: how M3-A rate classes and M3-C spend ceilings compose

The M3-A limiter has five classes today (`auth`, `chat`, `upload`, `read`, `admin`)
and keys **authorized** traffic on `OWNER_IDENTITY`, deliberately, so a second tab
cannot double the allowance; the `auth` class is the exception and keys on the
client address, because a rejected credential names no owner. KEL-28 adds a sixth
class, `share`. The M3-C ceiling is one day window and one month window computed
from `usage.jsonl`.

| Principal | Rate budget | Spend budget |
|---|---|---|
| Owner (all devices, root bearer) | One `owner` identity across every credential — **unchanged** | One day and one month ceiling. Every turn counts. |
| Anonymous share viewer | New `share` class, keyed on `sha256(token)` (prefix only), **plus** a global `share` ceiling, both charged before any lookup and failing closed | None. A share read makes no model call, so its marginal spend is exactly $0.00 and it does not move the owner's daily total |
| Scheduled / system actor | Consumes the owner's `chat` budget and the owner's turn/stream concurrency. Bounded by cron plus a per-schedule failure breaker, not by a per-schedule bucket | The owner's ceiling, before the first model call, in KEL-27's guard order |
| Plugin handler | The owner's `chat` budget and the turn's tool/time budget; no separate class | The owner's ceiling, because it runs inside the owner's turn |
| Named collaborator | Would need its own `principal:{id}` bucket | Would need a **sub-cap** under the owner's ceiling — otherwise one collaborator can consume the whole daily total. This is the concrete blocker on admitting collaborators (§12) |

Two decisions worth naming:

- **Share reads do not spend the owner's `read` budget.** If they did, a leaked
  link would be a denial-of-service against the owner's own dashboard — the exact
  inversion of the threat. The `share` class is separate and additionally capped
  globally, so many links cannot collectively exhaust the process.
- **Sharing cannot raise the bill.** Because a share is a frozen snapshot and never
  starts a turn, sharing is a zero-marginal-cost operation by construction. That
  property is what makes the free-tier story safe; any future "let the viewer ask a
  question" feature is a different ADR, a different principal, and a real spend
  decision.

## 8. Retention, revocation, and deletion rights

**Share token and snapshot.** `secrets.token_urlsafe(32)` — 256 bits. Stored as
`sha256(token)` hex in the primary key; the raw token exists in the creation
response exactly once and is never recoverable. Default TTL 7 days, hard maximum
30 days (`KELP_SHARE_MAX_TTL_SECONDS`); a request for longer is clamped, not
rejected silently. The token appears nowhere else: not in the URL of any other
route, not in a thread id, not in a log field, not in the audit trail.

**The snapshot is materialized at creation**, into the `shares` row, from the
thread's settled state: messages, blocks, and citation metadata projected to display
name, kind, mime, page/row counts, and locator. The projection **drops**
`storage_path`, `sha256`, and the artifact handle. That is what makes "no artifact
bytes" a structural property instead of a query the implementer must remember to
filter. Pending approval cards are excluded and counted ("2 cards not shown"), so a
half-finished turn cannot leak an argv payload, and a thread with a turn in progress
is refused at creation (`409 turn_in_progress`, the same rule KEL-21's purge uses).
Snapshot size is capped (`KELP_SHARE_MAX_SNAPSHOT_BYTES`, default 4 MiB); a larger
thread is refused with a typed error rather than silently truncated.

| Action | Immediately invalidates | Deliberately leaves behind |
|---|---|---|
| Revoke one link | `revoked_at` is set on the row; the next request is the identical `404`. The row is **not** deleted at revoke — it is pruned by the thread purge and by KEL-28's share sweep (boot + daily, the orphan-sweep pattern), never on the read path (a share GET mutates nothing) — so a concurrent read cannot lose the row mid-request. Nothing to purge from a cache, because the response is `no-store` | The `share_revoked` audit record (token hash, no token) under audit retention; the thread itself; any copy the viewer already holds |
| Revoke all links for a thread | `revoked_at` is set on every row for that thread, in one transaction; the rows are pruned later by the sweep, not deleted here | As above, per link |
| Link expires | The next request after `expires_at` is the identical `404`; expired and revoked rows are pruned by the share sweep, not by the request that observed them | The creation audit record, until its own 30-day rotation |
| Delete a thread (KEL-21 purge) | Its share rows **and its `share_views` counts** are deleted in the same transaction as its messages and blocks, and its files are unlinked. Links are `404` on the next request | Nothing of the thread; the audit record of creation/revocation |
| Revoke a device session (KEL-24) | That session's cookie and its live SSE stream | **Share links are untouched.** A link is not tied to a session, so signing a laptop out does not un-share a thread |
| Rotate the root token / `KELP_SESSION_SECRET` | Every session credential | **Share links are untouched**, by design: they do not derive from the session secret. The owner therefore needs an explicit "revoke all links" action, and it is not optional — it is the only kill switch for links |

The honesty clause that belongs next to that table: **a delivered page cannot be
un-delivered.** Revocation guarantees the next request fails; it cannot blank a page
already rendered in a viewer's browser, nor recall a saved copy, a screenshot, or a
print. Sharing is a decision to hand someone a copy. The product must say so in the
share panel copy, and this ADR refuses to pretend otherwise.

**Retention accounting.** Two tables, one clock. The snapshot is chat content — a
second copy of it — and `share_views` is a per-day counter keyed by `token_hash`.
Both are governed by the existing **Chat content** row: the thread purge deletes
`shares` and `share_views` in the same transaction as messages and blocks, and
neither is retained after the thread is gone. They therefore add **no** row to
`chat/retention.py::RETENTION_ROWS`, `docs/DEPLOY.md` §8.1, or `docs/PRIVACY.md`
§3, and no row is owed there. The snapshot contains no artifact bytes by
construction (the projection drops `storage_path`, `sha256`, and the handle), so a
purged artifact has nothing to delete inside a share row: the artifact follows the
**Uploaded files** row and the snapshot never held it.

If KEL-28 gives share rows their own clock instead — for example a 30-day window
on revoked rows — that row must be added to all three in the same change. The
test binding is one-directional, which is why the review still has to check the
other way: `tests/test_privacy.py` asserts that every `RETENTION_ROWS` class name
appears in `docs/PRIVACY.md`, and nothing parses `docs/DEPLOY.md` §8.1's rows. A
row added to the code and to `DEPLOY.md` but not to the review would pass the
test and still be wrong.

## 9. Migration path for owner-less tables

No table carries a principal column, `sessions` included: a `sessions` row records
the owner's *device* — hashed id, label, timestamps, revocation — not a principal,
and the string `'owner'` appears nowhere in the current schema. There is also no
schema version table: `ChatStore.__init__` runs `_SCHEMA` with `CREATE TABLE IF NOT EXISTS`
on every open, which is additive and idempotent but cannot alter an existing table.

The decision has three parts:

1. **No blanket `owner_id` migration now.** Adding a column that can only hold the
   string `'owner'` buys nothing today and is a rewrite of every read path for a
   guess about tomorrow. The rule instead is explicit: **a table with no owner
   column is owned by the owner.** That is not an ambiguity; it is the schema-level
   statement of a single-tenant product.
2. **The one place the boundary is real gets a real column.** The new `shares` table
   carries `owner_id TEXT NOT NULL DEFAULT 'owner'`. It is the only object whose
   whole purpose is to cross the boundary, so it is the only object that should name
   the boundary. A share resolves by `token_hash` alone — never by thread id, never
   by owner id — so the read path never needs a join to a table that has no owner.
3. **If collaborators are ever admitted, the migration is additive and backfilled
   by its default.** Idempotent column addition guarded by `PRAGMA table_info`
   (`ALTER TABLE ... ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'owner'`), which is
   safe on SQLite, needs no data rewrite, and makes every existing row the owner's.
   That migration ships with the issue that admits collaborators, never before, and
   its test asserts no data loss on threads, messages, and artifacts — the same
   shape as KEL-24's upgrade test.

Also decided: `threads.deleted_at` is a marker, not a tombstone a share may read
through. A share cannot be created for a thread with `deleted_at` set, and a share
whose thread is gone is an orphan the sweep removes — never a `404` that reveals
whether the thread ever existed.

## 10. Threat model

The share surface is the first unauthenticated read path in the product, so it gets
its own short model. Scope: the link, not the owner's machine.

| Threat | Why it is real here | Mitigation |
|---|---|---|
| **Token leakage via `Referer`** | The token is in a URL, so any outbound navigation or third-party subresource from the share page can carry it downstream | `Referrer-Policy: no-referrer` on every share response — `strict-origin-when-cross-origin` (as `netlify.toml` sets for the static tracker) still sends the origin, and the owner's origin is identifying. The page loads **no third-party asset at all**: no font, script, image, or iframe. Any outbound link carries `rel="noreferrer noopener"`. CSP `default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'` |
| **Token leakage via logs** | A path segment lands in access logs, proxy logs, and metrics labels by default | The token is a path segment, so the route is logged as a **pattern**, never a value: the access line already logs `route`, not `path` (`chat/logging.py::_ALLOWED_FIELDS`), and `_route_label` collapses any non-`/api` path to `/other`. KEL-28 adds a bounded `/s/{token}` label and a `share` metric class. A reverse proxy in front of the app must not log the path or must redact `/s/*` — that runbook note is a follow-up (§15), not yet written |
| **Enumeration** | A guessable or oracle-revealing token turns a private thread into a scan target | 256 bits of `secrets` entropy; **one** lookup path for every input: the presented value is hashed with SHA-256 and looked up by that primary key, malformed input included, so there is no shape check to branch on. **One** response builder means unknown, expired, revoked, and malformed are byte-identical `404`s with no distinguishing header, and the work before the response is the same — hash, one keyed lookup, one render. Rate limiting is charged before the lookup, so a scan is slow even though it is hopeless |
| **Revocation latency** | A link that keeps working after "revoke" is worse than no revoke button | Revocation is a row update visible to the next request, with no cache to expire (`no-store`), no CDN in front of the route, and no in-process token cache. There is no SSE share stream in this design, so there is nothing to close; if one is ever added it must close within one tick, the KEL-24 pattern |
| **CSRF** | Not applicable, and that is worth writing down rather than leaving as an omission | The surface is GET-only, sets no cookie, and grants no ambient authority, so a cross-site request carries no credential the attacker does not already possess (the token). There is no state to change and no CSRF token to protect. **Any** non-GET on a share route is `405` before the token is even looked up — which is also what makes the absence of CSRF safe rather than merely convenient |
| **Cross-read** | One token resolving another thread is the catastrophic bug | A share route accepts a token and nothing else. No thread id is accepted alongside it, and the snapshot is a copy in the row, so the read path cannot reach a thread even by accident |
| **Content injection in the shared page** | Assistant text and citations are rendered to an unauthenticated stranger | The page is server-rendered with HTML escaping, no script tag, no inline event handler, and no artifact HTML. CSP has no `script-src`, so an escape bug is a layout bug, not an XSS |
| **Link as a covert channel** | A share link is a bearer URL: forwardable, and indistinguishable from a legitimate reader | Explicitly accepted and stated in the UI. The owner's remedy is revocation and a short TTL, not detection — there is no viewer identity to detect |

Out of scope by decision, not by omission: screenshot and print hardening, DRM, and
watermarking (KEL-28 says so too).

## 11. What sharing does not do

- It does not let anyone **write**: no reply, no comment, no edit, no reaction, no
  title change, no delete.
- It does not let anyone **ask the assistant** anything. No model call, no tool
  call, no token spend.
- It does not grant **artifact bytes**. Citation metadata only; the share view has
  no download, no preview, no `content` route.
- It does not reveal **another thread**, the thread list, the dashboard, or that any
  other thread exists.
- It does not create an **identity**. There is no viewer account, no email, no
  invite, no per-viewer ACL, no "who viewed this" list.
- It does not **un-share** what has already been read. Revocation stops the next
  request; it cannot recall a copy.
- It does not make the **model provider** see less. The thread was already sent turn
  by turn; sharing is downstream of that and changes nothing about it.
- It does not survive the **thread**. Delete the thread and the links are gone, in
  the same transaction.
- It is not a **security boundary against the owner's own device**. A person using
  the owner's unlocked browser is the owner.

## 12. Options considered

| Option | What it is | Migration cost | Blast radius | Verdict |
|---|---|---|---|---|
| **(a) Single-owner box + read-only per-thread capability links** | This ADR. One principal, one credential family, one budget; sharing is a token that authorizes one frozen snapshot | **Low.** Additive `shares` and `share_views` tables, one public route, one limiter class, one projection function. No existing table changes, no read path changes, no backfill | **Small and bounded.** The worst case is one thread's snapshot readable by whoever holds one URL, for at most 30 days, revocable instantly. It cannot write, spend, or read anything else | **Recommended** |
| **(b) Multi-principal workspace membership** | Workspace with members, invites, roles (viewer/editor/admin), per-principal credentials and scopes | **High.** `owner_id` on every table plus backfill; a principals/membership/invite schema; every read path filtered by principal; per-principal limiter buckets; per-principal spend sub-caps under one ceiling (or a billing model); approval authority redesigned; ownership transfer and offboarding (what happens to a departing member's threads?); artifact access control rewritten from "knows the thread id" to "is entitled to the thread"; retention per principal | **Large.** Every invariant in §1 changes at once, on the same database that holds the only copy of the owner's conversations. A scoping bug is a cross-tenant read. It also breaks the product promise — a private assistant on your own box — and needs an answer to "who owns this thread when the person who wrote it leaves?" | **Rejected for now.** Revisit on the trigger below |
| **(c) Multi-tenant separation** | Several independent owners on one deployment, isolated by tenant | **Very high.** Tenant id everywhere, per-tenant keys, per-tenant storage roots, isolation testing, and a new threat model in which the operator is inside the trust boundary | **Largest.** One missed filter is one customer reading another's documents. It also contradicts the deployment shape in `docs/DEPLOY.md` (one box, one owner, one database) and the privacy review's premise | **Rejected.** Not a sharing feature; a different product with a different privacy claim |

**Why (a) over (b).** The job to be done is "show a teammate this thread", and a
capability link does that completely. Workspace membership buys collaboration —
shared write access inside one live thread — which nobody has asked for, and it
pays for it with the five invariants that currently make the security story short
enough to be true. The ordering matters too: (a) is a strict subset of (b)'s
requirements, so its `shares` table, its attribution vocabulary, and its limiter
class survive a later migration, while (b)'s design would have been written against
requirements nobody has confirmed.

**The trigger that reopens this.** Admit named collaborators only when there is a
concrete request for a second person to *write in the same thread* — not merely to
read one. At that point (b) is the answer, and it starts with §9's additive
migration, per-principal sub-caps (§7), and the non-negotiable rule from §5 that a
collaborator never approves a spend or a write.

**Why not a middle option** (named readers with accounts but no write)? It carries
most of (b)'s cost — identity, invites, a second credential family, per-principal
retention questions — to deliver what (a) already delivers with a URL, and it makes
the recipient's identity a record the product then has to protect. A capability is
less data, not more.

## 13. Explicit non-decisions

These are out of scope here. Each has an owner and a place it will be decided; none
of them may be inferred from this ADR.

| Topic | Status | Why it is not decided here |
|---|---|---|
| **Payments and wallet** (`payments/wallet.py`) | Deferred | Payments are not wired (`HttpStripeGateway` is never constructed). The invariant this ADR does fix: a share cannot initiate a payment, and no non-owner can approve one. Whether a share surfaces a receipt, and whether a future paid share exists, is a separate decision with legal and pricing input |
| **Telegram mirror identity** | Deferred | The mirror is the owner's own channel (Q7). A share link deep-linked into Telegram is just a URL in a message; it creates no identity and no new egress recipient. If the mirror ever accepts inbound commands, that is a new principal and needs its own ADR |
| **Plugin identity** | Deferred to KEL-25 | A plugin has no identity, no credential, and no budget of its own: it borrows the owner's turn and is constrained by its manifest plus the approval gate. Per-plugin credentials and per-plugin sub-caps are not part of the digest-pin slice |
| **Named collaborators, roles, invites** | Not admitted (§12) | Trigger above |
| **Tenant isolation** | Rejected (§12) | Different product |
| **Legal, compliance, residency** | Out of scope | The issue says so; nothing here is legal advice |

## 14. The smallest shippable slice: KEL-28, specified

KEL-28 is the implementation, and this section is the part of its spec the ADR
fixes so an engineer does not have to guess. Anything not listed is KEL-28's own
detail.

- **Route.** `GET /s/{token}` returns a self-contained `text/html` document: no
  script, no third-party asset, no cookie. A JSON variant is a later issue with a
  real consumer; today there is no dashboard code in this repository
  (`docs/frontend-scope-simple.md` is design-only), so a server-rendered page is the
  only surface that can work in a signed-out window without inventing a frontend.
- **Token.** `secrets.token_urlsafe(32)`; stored as `sha256` hex primary key;
  returned once in the creation response; never in a thread id, a redirect, or a
  log. Malformed input is **not** rejected by shape before the lookup: every
  presented value is hashed, a malformed one simply misses, and it renders the
  same `404` through the same builder. An early shape check is exactly the oracle
  this design is avoiding.
- **Expiry.** 7 days default; `KELP_SHARE_MAX_TTL_SECONDS` hard ceiling (30 days);
  enforced on every read, not at creation only.
- **Snapshot.** Materialized at creation from settled state, per §8: the projection
  drops storage paths, hashes, and artifact handles; pending approval cards are
  excluded and counted; a turn in progress refuses creation with `409`.
- **Read path.** One `shares` lookup by `token_hash`, one response builder, then
  render. No join to `threads`, no artifact call, no model, no tool.
- **No side effects.** Every non-GET is `405` before the lookup. Tests assert zero
  provider calls, zero tool calls, zero artifact reads, and no mutation on any share
  request.
- **Indistinguishability.** Unknown, expired, revoked, and malformed produce
  byte-identical `404` bodies, no distinguishing headers, and the same work:
  hash, one keyed lookup, one response builder. Malformed input is not
  short-circuited, so it does not return measurably faster than a well-formed
  miss. The acceptance test compares the bytes of all four bodies, the header
  set, and the status.
- **Headers.** `Cache-Control: no-store`, `X-Robots-Tag: noindex, nofollow`,
  `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`, and the CSP in
  §10. No `Set-Cookie`, and no way to mint a session from a share request.
- **Limits.** New `share` class, per-token bucket plus a global ceiling, charged
  before the lookup, failing closed.
- **Audit.** `share_created` and `share_revoked` carry `token_hash` and never the
  token; `share_viewed` is aggregated per day in `share_views (day, token_hash,
  views)` with no per-request row and no viewer data. `share_views` rows are
  deleted by the thread purge in the same transaction as the `shares` rows — the
  counter has no separate clock and therefore no retention row.
- **Owner endpoints.** Create, list-for-thread, and revoke are authenticated,
  CSRF-protected mutations under the existing `chat` class, so the existing
  `_authorize` path covers them unchanged.

**Three deviations from KEL-28's text, all narrowing and all deliberate:**

1. KEL-28 says revocation "closes any live share viewer streams". This design has
   no share stream — the page is a static snapshot per request — so there is nothing
   to close, and the guarantee is "the next request is a `404`". A stream, if ever
   added, inherits KEL-24's one-tick close rule. **Superseded 2026-10-30 by §14.1:
   the shipped slice does have a stream.**
2. The header set is KEL-28's plus one: `Referrer-Policy: no-referrer`. It is
   called out as a deviation rather than as a detail because a token in a path plus
   any outbound link is the single most likely leak in the whole design; a later
   change that drops it has to delete this paragraph, not just a line in a list.
3. KEL-28's text names a per-token `share` bucket and no process-wide bound. This
   ADR adds the global `share` ceiling on top of it (§7). Without it, every distinct
   token carries its own full allowance, so the aggregate a stranger can drive is
   bounded by how many links exist rather than by the process. The global ceiling is
   the bound that survives a leaked link.

### 14.1 Amendment: what KEL-28 actually shipped (2026-10-30)

This section records the implementation as merged. Where it disagrees with §14
above, this is the operative text; nothing here widens a guarantee — it replaces
an unbuilt surface with the one that exists.

- **Route.** The read surface is `GET /api/shared/{token}` returning JSON
  (`{ok, share}`), not `GET /s/{token}` returning HTML. There is no frontend in
  this build, and a JSON body is the contract every other route already uses;
  a server-rendered page is still a later issue with a real consumer. Owner
  endpoints are also under `/api/threads/{id}/…`, as §14's table says, not `/s`.
- **Stream.** `GET /api/shared/{token}/stream` is an SSE replay of the frozen
  snapshot (`snapshot`, then one `message` frame per message, then 15-second
  heartbeats) and tails nothing new, because the snapshot is immutable. It
  re-checks the row every `_SHARE_STREAM_TICK` and closes within one tick on
  revoke, expiry, or purge — so deviation 1 above is superseded: there *is* a
  stream, and it does inherit KEL-24's one-tick close rule. Open streams are
  capped process-wide (`KELP_MAX_SHARE_STREAMS`, default 16) with the `_Lease`
  release pattern; a refused open is a `429` (`details.class =
  "share_streams"`) that still carries the share headers.
- **Views.** The counter is `share_views (day, token_hash, views)`, one row per
  day and link, incremented by every successful snapshot or stream open; the
  first view of a day also writes one `share_viewed` audit record with
  `actor="share"`. The owner's listing reports the aggregate `views`. A refused
  open is not a view.
- **Methods.** Every non-GET under `/api/shared/*` is a `405` with
  `Allow: GET`, including `HEAD`, `OPTIONS`, `PUT`, and `TRACE`. The stdlib
  adapter routes those verbs into the API rather than emitting its own `501`
  HTML page, and the request body is never read; the stdlib's error path is
  overridden for this prefix so the share header set and typed envelope survive
  even there.
- **Writes re-check the parent.** `share_create` inserts with
  `INSERT … WHERE EXISTS (threads … deleted_at IS NULL)` inside its write
  transaction; a purge that commits between the API's read and the insert
  inserts nothing and answers `404 unknown_thread`. `share_revoke_prefix`
  accepts exactly the 16-hex public id issued by the listing.
- **Projection.** The snapshot is scrubbed with the orphan sweep's artifact
  handle pattern, so a handle typed into text, a tool summary, or a citation is
  replaced by `[attachment]` as well as being dropped from structured fields.
- **Sweep.** Expired, revoked, and parentless (including soft-deleted parent)
  share rows are deleted by `ChatStore.share_sweep`, which
  `RetentionMaintenance.run_once` calls on the boot pass and then daily; owner
  create also calls it opportunistically. A share read never sweeps.

## 15. Consequences and follow-ups

**Good.** The security story stays short enough to be true: one principal, one
budget, one approval authority, and a sharing primitive that cannot write, spend, or
read across threads. The share surface adds no outbound recipient, so
`docs/PRIVACY.md`'s egress ledger stays exactly as it is. The attribution vocabulary
declared in §6 — `actor` plus the `extra` keys `on_behalf_of` and `origin_ref` — is
what KEL-23 and KEL-27 need anyway, so the scheduler and the share link land on one
model instead of two; KEL-24's `credential` and `id_short` keys stay as they are and
are not part of it.

**Cost.** A snapshot duplicates chat content at rest. That is a real privacy surface,
paid for by making purge delete it in the same transaction and by never retaining it
past expiry. It also means "the thread changed after I shared it" is a visible,
intentional behaviour the UI must state.

**Debt accepted.** Share links are bearer URLs with no viewer identity, so a leak is
undetectable and unrevocable once a copy is taken. Named readers would fix that at
the cost in §12(b). This is named in the UI copy, not hidden.

| Follow-up | Why | Where |
|---|---|---|
| Share link implementation | This ADR's slice | **KEL-28** (shipped 2026-10-30; route/stream/views reality in §14.1) |
| Scheduled-turn actor, `paused_needs_approval`, `origin=schedule` | §4, §5, §6 bind the scheduler | **KEL-23**, **KEL-27** (exist) |
| Plugin attribution (`actor="plugin"`, digest in `extra`) | §6 vocabulary applies to plugin skills | **KEL-25** (exists) |
| Attribution keys `on_behalf_of` and `origin_ref` in `extra` | §6 declares them; `AuditLog.record` has no such parameter and nothing writes them today, so these issues own the extension | **KEL-28** (`share`), **KEL-23/KEL-27** (`schedule`), **KEL-25** (`plugin`) — each writes its own key; no separate issue needed |
| Actor-vocabulary conversion and `system` attribution | §6 closes the vocabulary at `AuditLog.record` and `loader.invoke`; `chat/recovery.py` records `system` in its event payload but leaves the `approval_expired` audit line's `actor` unset | Inside the next issue that touches `chat/recovery.py` (a one-line `actor="system"`); nothing writes `agent` today |
| `share_views` deletion on thread purge | §8 puts the counter under **Chat content**, so it is deleted with the thread and no retention row is owed | **KEL-28** (exists) |
| Add ADR 0002 to the reference table | Discoverability next to ADR 0001 | Proposed: docs chore, `docs/CHAT_SPEC.md` §19 (no code) |
| Redact `/s/*` in any reverse-proxy access log | §10 log-leak row; the app's own log is safe, a proxy's may not be | Proposed: `docs/DEPLOY.md` runbook note |
| `Referrer-Policy` and CSP on the authenticated app, not just the share page | The share route is the first place they are required; the dashboard needs them too | Proposed: security hardening issue |
| Per-principal spend sub-caps | Required before any collaborator is admitted (§7, §12) | Proposed: ships with the collaborator decision |
| Share retention row, only if KEL-28 adds a share clock | `RETENTION_ROWS`, `DEPLOY.md` §8.1, `PRIVACY.md` §3 must change together; today purge deletes `shares` and `share_views`, so no row is owed | Proposed: inside KEL-28 only if it changes the clock |

These follow-ups were raised on 2026-10-30, when this ADR was accepted.

## 16. Open questions

| Question | Owner | Date / status |
|---|---|---|
| Should a revoked share row be kept for audit convenience, or pruned immediately? | KEL-28 implementer | **Decided 2026-10-30: kept, pruned at purge.** Revoke sets `revoked_at`; the row is deleted by the thread purge and by the share sweep, not by the revoke request. Pruning at revoke would race a concurrent read and would lose why the link stopped working; the `share_revoked` audit record is the durable account |
| Should the share page show the thread title? | KEL-28 implementer | **Yes** (2026-10-30) — a reader with no context cannot use a page without one, and the title is already in the snapshot. Not a privacy change: the owner chose to share the thread |
| Re-snapshotting: new row or update in place? | KEL-28 implementer | **New row, new token** (2026-10-30) — an updated snapshot must not silently change what a previously shared URL shows, or revocation stops meaning anything. The old link keeps its old content until it expires |
| Does the model provider's copy of a shared thread matter? | Owner | Out of scope (§11), 2026-10-30. It was sent before sharing existed |

## 17. How to check this ADR

Every claim above is checkable from the repository:

- §1's invariants: `chat/api.py` (`OWNER_IDENTITY`, `_RATE_LIMIT_CLASSES`,
  `SessionStore`, `_authorize`), `chat/store.py` (`_SCHEMA`), `kernel/usage.py`
  (`SpendGuard`, `UsageRecord`), `openshell/approvals.py`, `chat/service.py`,
  `chat/artifacts.py` (`get`).
- §6's field vocabulary: `openshell/audit.py::AuditLog.record`,
  `chat/logging.py::_ALLOWED_FIELDS`, `chat/api.py::_route_label`.
- §8's retention class: `chat/retention.py::RETENTION_ROWS` and
  `tests/test_privacy.py`, which fails when a class in `RETENTION_ROWS` is missing
  from `docs/PRIVACY.md`. The reverse direction — a row left in the review after
  the code drops it — is not test-bound, and `docs/DEPLOY.md` §8.1 is mirrored by
  hand, so both are checked at review time rather than by a test.
- §9's migration: `chat/store.py::ChatStore.__init__` (`CREATE TABLE IF NOT
  EXISTS`, no version table) and `tests/`' KEL-24 upgrade test for the precedent.
- §12's deployment premise: `docs/DEPLOY.md` and `docs/PRIVACY.md`.
- §14's acceptance, once implemented: `tests/` fixtures asserting no provider call,
  no tool call, no artifact read, byte-identical `404`s, and `405` on every non-GET.

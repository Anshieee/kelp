# ADR 0003 — a service control layer for the bot, without multi-tenancy

- **Status:** Accepted
- **Date:** 2026-11-09
- **Issue:** P0-1 (planning gate; P1-P6 wait on it)
- **Related:** [ADR 0002](0002-sharing-and-identity.md) (single-owner identity),
  [`docs/PRIVACY.md`](../PRIVACY.md), [`docs/KERNEL.md`](../KERNEL.md),
  [`docs/CHAT_SPEC.md`](../CHAT_SPEC.md), and the frozen shapes in
  [`docs/SERVICE_CONTRACT.md`](../SERVICE_CONTRACT.md)
- **Machine-checked by:** [`tests/test_service_contract.py`](../../tests/test_service_contract.py)

## 1. Context

The product today is one owner on one process: one identity, one provider
credential, one spend ceiling, and one approval authority. That is a deliberate
decision in ADR 0002, not an accident, and it stays true in this ADR.

The next slices turn the bot into a service that accepts work from more than one
surface (web, desktop, mobile, Telegram, CLI, scheduled jobs). Each surface needs
the same answers to the same questions:

- Which tenant does this work belong to?
- Which provider call is allowed, and what is the run's identity?
- Is the caller entitled to the task, under the plan's limit and window?
- Is there budget left before the provider sees the transcript?
- What may an operator read about the run without reading the conversation?

Today those answers are spread across `chat/api.py`, `chat/service.py`,
`kernel/provider.py`, `kernel/usage.py`, and the scheduler. A surface that grows
its own answer will diverge from the others. The cost of divergence is not
cosmetic: a skipped gate is a spend overrun or a cross-tenant read, and a run
record that can be written twice is an accounting lie.

P0-1 therefore freezes the **contract**, not the implementation. No runtime code
changes ship with this ADR. P1-P6 implement against these shapes.

## 2. Decision

Introduce a control layer with five frozen pieces:

1. **Tenant model.** One owner tenant, id `"owner"`, now. A `tenant_id` field
   appears in the request and run shapes as the seam for later slug-shaped
   tenant ids. Isolation is not shipped in P0.
2. **Provider contract.** One streaming method,
   `stream(ChatRequest) -> AsyncIterator[ProviderEvent]`, behind one
   module-level patchable provider symbol and an injectable `provider=` keyword.
   The contract is name-agnostic so the seam can be renamed without rewriting
   callers or tests.
3. **Run record intent.** `TaskRun` is the operational record of one unit of
   work: external `run_…` id, internal `turn_…` id, status, timestamps, model,
   token and cost totals, step/tool/approval counts, error code, retry link, and
   idempotency key. It has exactly one terminal record per run.
4. **Entitlement gate order.** `auth → tenant → entitlement → spend → provider`,
   each gate failing closed before the next runs.
5. **Privacy and retention boundary.** The admin read model exposes operational
   metadata only: no prompt text, no secret values, no provider response body,
   and no cross-tenant rows.

The normative field lists, statuses, gate order, error codes, and the
machine-readable schema live in [`docs/SERVICE_CONTRACT.md`](../SERVICE_CONTRACT.md).
This ADR records why those choices were made; the contract document is the thing
implementers and tests read.

## 3. Tenant model

**Now:** exactly one tenant, `tenant_id = "owner"`. The owner is the only
principal, the only role, and the only approval authority. A table with no owner
column belongs to the owner (ADR 0002 §9); this ADR adds no ownership column and
no migration.

**Later:** tenant ids become URL-safe slugs. The migration is additive: a new
column defaults to `'owner'`, every existing row backfills to the owner, and the
one-tenant code path remains valid. No P0 decision forces a rewrite of read
paths.

**What this ADR does not decide.** It does not admit a second tenant, define
cross-tenant administration, or change the privacy claim that one deployment
serves one owner. ADR 0002 §12 still rejects multi-tenant separation. The
`tenant_id` field is a boundary marker for future work, not a promise that the
boundary is enforced today. Enforcing it is P1+ and needs its own review, with
isolation tests and a threat model in which the operator is inside the trust
boundary.

`TenantContext` carries the four values every gate needs before doing work:
`tenant_id`, `actor_id`, `role`, and `surface`. `actor_id` is `"owner"` for
human actions and a controlled non-human value (`system`, `scheduler`,
`plugin`, `share`) elsewhere; it is not free text.

## 4. Provider contract

The provider seam is one method:

```text
stream(ChatRequest) -> AsyncIterator[ProviderEvent]
```

`ChatRequest` is a frozen dataclass in `kernel/provider.py`. Today it has exactly
five fields — `model`, `messages`, `tools`, `temperature`, `max_tokens` — and no
`tenant_id`. P0 forbids runtime changes, so the contract marks `tenant_id` as the
P1 target rather than pretending it is already there. The test asserts both
facts: the current five-field shape and the schema's P1 addition.

`ProviderEvent` is the closed union already used by the loop: `TextDelta`,
`ToolCallReady`, `UsageReport`, `ProviderFailure`, and `StreamEnd`. Provider
failures are typed events, not exceptions; the loop turns them into user-visible
errors.

`ProviderSpec` is the configuration record the control layer uses to select a
provider: `name`, `base_url`, `api_key_env`, `default_model`, `pricing`. The API
key is named by environment variable, never stored in the spec. Renaming the
provider symbol does not change this contract.

**The seam rule is name-agnostic.** On this branch the module-level symbol is
`chat.server.ProviderClient` and `build_app(..., provider=...)` injects a
replacement. A future branch may call the same seam `TokenFactoryClient`. The
guarantee is structural: exactly one module-level patchable provider symbol plus
the injectable `provider=` keyword. Tests accept either name and fail if the
structure is missing.

Why keep the seam rather than import the class at each call site? Tests and
deployments replace one symbol to simulate provider behavior without network
access. A second seam would mean one replacement path is silently untested.

## 5. Run record intent

A `TaskRun` answers "what happened to this unit of work" without answering "what
was said". It is durable operational state, not chat content.

- `run_id` is external and starts `run_…`; it is the id a surface or operator
  uses. `turn_id` is internal and starts `turn_…`; it is the id the chat store
  already understands. They are separate values because a retry creates a new
  run while the turn remains the conversation's unit.
- Statuses are `queued`, `running`, `awaiting_approval`, `stopping`,
  `completed`, `failed`, `cancelled`, and `interrupted`. The terminal statuses
  are `completed`, `failed`, `cancelled`, and `interrupted`.
- **Exactly one terminal record per `run_id`.** A terminal status is written
  once and never overwritten. A late writer loses; it does not append a second
  ending. This is what makes retries, crash recovery, and the admin view
  trustworthy.
- Token and cost totals are recorded on the run so a surface can show spend
  without reading usage files. They mirror the usage record's numbers; the usage
  record remains the accounting source.
- `idempotency_key` lets a surface retry a submission without creating a second
  run. `retry_of` links a new run to the one it retries.

P0 freezes the shape and the terminal rule. It does not add the store. When the
store lands, its retention row must be added to `chat/retention.py`'s
`RETENTION_ROWS`, `docs/DEPLOY.md` §8.1, and `docs/PRIVACY.md` §3 in the same
change. Until then this record adds no retention row and no new file on disk.

## 6. Entitlement and gate order

Every request passes five gates in this order:

```text
auth → tenant → entitlement → spend → provider
```

1. **auth** — is the caller authenticated, and does the session carry a valid
   CSRF token where the method requires one?
2. **tenant** — does the `tenant_id` on the request match the tenant resolved
   from the authenticated principal? A mismatch is denied, not coerced.
3. **entitlement** — does the plan allow this task type now? The decision is an
   `EntitlementDecision`: `allow` or `deny`, a machine-readable `code`, the
   `limit` and `window` that applied, and the `plan_id`.
4. **spend** — is there budget left in the current spend window?
5. **provider** — only now may the provider see the request.

Each gate fails closed: a denial stops the request before the next gate and
before any provider call. The order matters. Authenticating before revealing
tenant data prevents an unauthenticated tenant oracle. Checking entitlement
before spend avoids charging a caller for a task the plan never allowed.
Checking spend before the provider avoids a transcript leaving the machine for a
request that cannot be paid for.

P0 freezes the order and the decision shape. It does not implement entitlement;
today's spend guard and provider call remain where they are. P1 inserts the new
gates in this order rather than reordering the existing path ad hoc.

## 7. Privacy and retention

The control layer is designed to be readable by an operator without exposing the
conversation:

- **No prompt text.** `TaskRun` and the admin read model carry counts, ids,
  statuses, model names, and costs, never message content, tool arguments, tool
  results, or provider response bodies.
- **No secret values.** `ProviderSpec` names an environment variable; the admin
  view never shows the value. The existing scrubber remains the last line of
  defense, not the first.
- **No cross-tenant data.** Every admin query is scoped by `tenant_id`, even
  while the only value is `"owner"`. A query that is not scoped is a bug today
  and a breach later.

The existing privacy review already fixes the recipient list and the retention
clocks; this ADR does not change either. The run record is operational state and
inherits the usage/audit boundary described in `docs/PRIVACY.md` §3-§4. The
provider remains the recipient of the transcript; the control layer only decides
whether the call happens, not what the provider does with it afterwards.

See [`docs/PRIVACY.md`](../PRIVACY.md) §2 (recipients), §3 (local storage), §4
(the egress ledger), and §8 (cross-references).

## 8. Consequences

**Good.** Surfaces and future slices share one vocabulary. Gates have a fixed
order, so a review can ask "which gate denied this" and get one answer. The run
record makes retries and recovery auditable without duplicating chat content. The
provider seam stays patchable, so tests do not need the network.

**Cost.** The contract is intentionally narrower than a full multi-tenant design.
`tenant_id` exists but does not isolate anything yet; a reader could mistake the
field for an implemented boundary, which is why every document says "single owner
tenant" in the same breath. The `run_…`/`turn_…` split adds one indirection to
error messages and support tooling.

**Debt accepted.** P0 ships documentation and a contract test only. The store,
entitlement engine, and gate wiring are P1+ work. The contract test fails if the
documents drift from the frozen schema or if the provider seam disappears from
this branch's code; it does not prove the gates run, because they do not run yet.

**Revisit when.** A second tenant is actually admitted, an entitlement plan with
limits ships, or a surface needs a run field the contract does not have. Any of
those reopens this ADR; none of them may be inferred from the field names alone.

## 9. Compliance with ADR 0002

ADR 0002 rejected multi-tenant separation and blanket owner columns. This ADR
does not reverse that. It keeps one owner tenant, adds no ownership column, and
does not let a share, a plugin, or a scheduled job become a principal. Those
actors keep the attribution vocabulary ADR 0002 §6 already defines
(`actor`, `on_behalf_of`, `origin_ref`). When collaborators are admitted, ADR
0002 §9's additive migration is still the path, and this contract's tenant
fields must be enforced in that change, not before it.

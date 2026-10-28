# Linear issue pack — Always-On Private Assistant (team KEL)

Status: **prepared, nothing created in Linear.** This file is the create-ready source of truth.
Verified: 2026-10-28 via Linear MCP.

Supersedes the `NEB` target in `docs/linear-p0-plan.md`. Structure, milestones, and issue
content are identical; only the team key changes to `KEL`.

## 1. Verified state

| Check | Result |
|---|---|
| MCP auth | OK — workspace read + write tools available |
| Workspace | `kelp` — https://linear.app/kelp-ai — `ad8e918a-296b-4e99-ac70-313019458f5c` |
| Team | **`KEL`** (Kelp) — `6e2b9438-ee47-4bcc-adc5-67fe7f890581` — **only team in workspace** |
| `NEB` | does **not** exist; no `create_team` tool is exposed via MCP (UI/API only) |
| Projects | none |
| Issue labels | `Bug`, `Improvement`, `Feature` (defaults only, workspace-level) |
| Workflow states (KEL) | Backlog, Todo (`844f8bfc-…`), In Progress, Done, Canceled, Duplicate |
| Users | `anshchampion` (`838c64bd-f997-45e4-ac34-edd03cb56e8b`, admin) |

**Decision:** file into `KEL` now. Branch prefix must be `kel-<n>-<slug>`; `neb-…` will
silently break PR auto-linking. If a dedicated team is wanted later, create it in the UI and
move issues — identifiers re-key, so doing it before creation is cheaper.

## 2. Project

`save_project`:

```json
{
  "name": "Always-On Private Assistant",
  "leadTeam": "KEL",
  "lead": "anshchampion",
  "priority": 2,
  "state": "Planned",
  "summary": "Always-on private assistant: local-first agent kernel on Nebius GPU, dashboard-primary chat, approval-gated tools, eval-backed reliability.",
  "description": "..."
}
```

Summary is 148 chars (limit 255). If `Planned` does not resolve against KEL project statuses,
fall back to `Backlog` and set the state later.

Description body:

> ## Goal
> A private, always-on assistant the owner talks to from a dashboard, backed by a local-first
> agent kernel running on a Nebius GPU. It answers with sources, reads the owner's documents,
> and takes real actions only behind explicit human approval.
>
> ## Non-goals for the MVP
> Voice memo input, WhatsApp as a first-class surface, team sharing, and third-party plugin
> installs are **post-prototype**. Payments: Stripe now, Razorpay deferred, balance P2 (Q18).
> See the decision log below.
>
> ## Architecture
> Dashboard (primary) → kernel agent loop (streaming + tool calls) → skill contracts →
> OpenShell policy gate → audit log. Provider behind an adapter, model swappable.
>
> ## Decision log
> | ID | Decision |
> |---|---|
> | Q7 | Dashboard is primary; WhatsApp is a **mirror**, P1 |
> | Q8 | Prototype cut = dashboard chat, PDF/CSV answers, `web.search` + `github.read` (read-only), approvals + scheduler |
> | Q9 | Saved cards via Stripe vault — **superseded by Q18** |
> | Q10 | Voice memo — **P1 only** |
> | Q18 | Payments: Stripe now, Razorpay deferred, balance **P2** |
>
> ADRs live in `docs/adr/`.

## 3. Milestones

`save_milestone` per row, `project: "Always-On Private Assistant"`.

| # | Name | Scope | Target |
|---|---|---|---|
| M0 | M0 — Foundation | repo scaffold, CI, dev env, model/provider selection, threat model | 2026-11-07 |
| M1 | M1 — Prototype | query → tool → answer loop, persistence, single-user auth, dashboard chat, approvals, demo script | 2026-11-28 |
| M2 | M2 — Polished MVP | restart/resume, health checks, retrieval quality, evals, observability, Nebius GPU deploy | 2026-12-19 |
| M3 | M3 — Hardening | privacy controls, secret isolation, rate limits, retention, cost guardrails | 2027-01-23 |
| M4 | M4 — Full vision | multi-device presence, proactive tasks, plugin surface, team sharing | 2027-03-06 |

Dates are placeholders — confirm or drop before creating.

## 4. Labels

Team-scoped (`teamId: 6e2b9438-ee47-4bcc-adc5-67fe7f890581`). Groups first with
`isGroup: true`, `groupType: "multiSelect"`, then children with `parent: "<group>"`.

**Only these are required for the 5 P0 issues** (create the rest of the tree as needed):

| Label | Parent | Color |
|---|---|---|
| `area` | — (group) | `#5E6AD2` |
| `type` | — (group) | `#4EA7FC` |
| `risk` | — (group) | `#F2994A` |
| `phase` | — (group) | `#27AE60` |
| `area/core-agent` | `area` | `#8B93E8` |
| `area/inference` | `area` | `#8B93E8` |
| `area/tools` | `area` | `#8B93E8` |
| `area/security` | `area` | `#8B93E8` |
| `area/ux` | `area` | `#8B93E8` |
| `area/evals` | `area` | `#8B93E8` |
| `type/feature` | `type` | `#4EA7FC` |
| `type/chore` | `type` | `#4EA7FC` |
| `risk/privacy` | `risk` | `#F2994A` |
| `risk/cost` | `risk` | `#F2994A` |
| `phase/prototype` | `phase` | `#27AE60` |

Deferred until needed: `area/memory`, `area/infra`, `type/bug`, `type/spike`, `type/docs`,
`risk/reliability`, `phase/mvp`, `phase/vision`.

Existing `Bug` / `Feature` / `Improvement` stay untouched; `type/*` supersedes them once the
tree exists. Do not create duplicates of the three defaults.

---

## 5. P0 issues — ready to create

All: `team: "KEL"`, `project: "Always-On Private Assistant"`, `state: "Todo"`,
`assignee: "me"`. Create labels and milestones first, otherwise name resolution fails.

---

### P0-1 — Kernel LLM: streaming agent loop with tool calling

`priority: 1 (Urgent)` · `milestone: M1 — Prototype`
`labels: area/core-agent, area/inference, type/feature, phase/prototype, risk/cost`

> Build the kernel agent loop that turns a user turn into a streamed answer with tool calls.
>
> **Scope**
> - Single provider interface (Nebius-hosted endpoint) behind an adapter so the model is swappable.
> - Streaming tokens to the dashboard over one persistent channel; tool calls interleaved mid-stream.
> - Tool-call protocol: model emits a typed call → runtime validates → executes → returns observation → model continues.
> - Hard turn budget (max steps, max tokens, wall-clock cap) to bound cost.
> - Structured failure: provider timeout / 429 / 5xx surfaces as a retryable event, not a dropped stream.
> - Usage accounting per turn (tokens in/out, estimated cost) persisted for the M3 cost guardrail.
>
> **Out of scope**
> - Memory and retrieval (separate issue).
> - Multi-agent orchestration.
> - Voice input (P1, Q10).
>
> **Acceptance**
> - A turn with 0, 1, and 3 sequential tool calls completes end-to-end against a stub tool.
> - Killing the provider mid-stream yields a visible error event and the session stays usable.
> - Step / token budget is enforced and logged.
> - Unit tests for the loop state machine; one integration test against a recorded fixture.
>
> **Open decisions** — concrete model ID and endpoint (confirm at kickoff); transport (SSE vs WebSocket).

`blocks: P0-2, P0-5`

---

### P0-2 — Dashboard chat surface (primary interface)

`priority: 1 (Urgent)` · `milestone: M1 — Prototype`
`labels: area/ux, area/core-agent, type/feature, phase/prototype`

> The dashboard is the primary product surface. WhatsApp is a P1 mirror, not a parallel build (Q7).
>
> **Scope**
> - Threaded chat: message list, streaming assistant turns, retry/stop, error states.
> - Tool-call rendering: collapsed step card per call (name, args summary, status, duration), expandable.
> - Approval prompts render inline in the thread (see P0-3) with approve/deny and a clear rationale.
> - **Answer artifacts:** PDF and CSV results render as an inline preview card with download, alongside the textual answer. CSV previews as a table (first N rows); PDF previews page 1 with an "open" action.
> - Citation / source chips on answers that used retrieved documents.
> - Session list, rename, resume.
>
> **Out of scope**
> - Razorpay integration and balance work (deferred / P2, Q18); Stripe saved-card vault is in scope.
> - Voice memo capture (P1, Q10).
> - Mobile layout beyond a usable responsive fallback.
>
> **Acceptance**
> - A PDF question and a CSV question both produce an inline artifact card that opens correctly.
> - Approval-gated call renders inline and blocks the stream until answered.
> - Reconnect during a stream restores the partial turn without duplicating messages.
> - Playwright smoke covering send → stream → artifact → approval.

`blockedBy: P0-1` · `blocks: P0-5`

---

### P0-3 — OpenShell: approval-gated tool execution + audit

`priority: 1 (Urgent)` · `milestone: M1 — Prototype`
`labels: area/security, area/tools, type/feature, phase/prototype, risk/privacy`

> Every side-effecting action goes through a policy check before it runs, and anything not
> explicitly allowed becomes a human approval request.
>
> **Scope**
> - Capability model: each tool declares effects (`read`, `write`, `network`, `execute`) and a scope (paths, hosts, repos). Consumes the manifest from P0-4.
> - Policy evaluation: allow / deny / require-approval; default deny for writes and network egress to new hosts.
> - Approval object: what will run, exact args, blast radius, expiry (default 10 min), single-use.
> - Approval channel for the prototype: inline dashboard prompt. Pluggable, so the WhatsApp mirror can be added in P1 without rework.
> - Audit log: append-only record of decision, actor, args hash, result, duration. No secrets in the log.
> - Deny-by-default for shell exec in the prototype; reads only under the workspace allowlist.
>
> **Out of scope**
> - Sandboxing / container isolation beyond process cwd + env scrubbing (M3).
> - Multi-user approval routing.
>
> **Acceptance**
> - A write outside the allowlist produces an approval request and executes only after approval.
> - Denied approval leaves no side effect and records the denial.
> - Expired approval cannot be replayed.
> - Audit log reconstructs a full turn's actions; grep for secrets in the log returns nothing.

`blockedBy: P0-4` · `blocks: P0-5`

---

### P0-4 — Skill contracts: schema + capability manifest (+ 4 prototype skills)

`priority: 2 (High)` · `milestone: M1 — Prototype`
`labels: area/tools, area/core-agent, type/feature, phase/prototype`

> Define the contract every skill implements, so tools are addable without touching the kernel.
>
> **Scope**
> - Manifest per skill: name, version, description, JSON-Schema input/output, declared effects and scopes, timeout, idempotency, cost class.
> - Loader: discovery from a directory, schema validation at load, reject invalid manifests at startup (fail fast, named error).
> - Runtime: argument validation before execution, output validation after, typed error envelope (`retryable`, `user_fixable`, `message`).
> - **Prototype skill set (Q8 cut):**
>   1. `web.search` — read-only search, network egress allowlisted.
>   2. `github.read` — read-only repo / issue / PR / file reads via the GitHub API; token from env, never logged.
>   3. `docs.answer` — PDF / CSV ingest + question answering; returns answer + artifact handle.
>   4. `schedule.create` — create a scheduled task (feeds the scheduler); requires approval.
> - Contract tests: a golden manifest fixture plus one negative test per validation rule.
>
> **Out of scope**
> - Third-party plugin installation and signing (M4).
> - GitHub **write** operations (post-prototype; needs its own approval policy).
>
> **Acceptance**
> - Adding a new skill requires only a manifest + handler, zero kernel edits.
> - Invalid manifest fails startup with the offending field named.
> - All four prototype skills pass contract tests and are callable from P0-1's loop under P0-3 policy.
>
> **Decision to record (ADR)** — whether `docs.answer` is one skill or `pdf.extract` + `csv.query` behind it.

`blocks: P0-1, P0-3`

---

### P0-5 — Demo choreography: 6-beat scripted end-to-end run (`npm run demo`)

`priority: 2 (High)` · `milestone: M1 — Prototype`
`labels: area/ux, area/evals, type/chore, phase/prototype`

> One deterministic script that exercises the whole prototype story, used for review and as
> the regression harness for the demo path.
>
> **Beats**
> 1. Dashboard chat open; ask a question needing `web.search` → answer with source chips.
> 2. Ask "summarize this PDF" (fixture) → PDF artifact card; ask a CSV question → table preview + numeric answer.
> 3. Ask about a GitHub issue → `github.read`, read-only, no approval needed.
> 4. Ask for something that writes → inline approval prompt → approve → action executes → audit entry visible.
> 5. Ask to schedule a recurring task → approval → task appears in the scheduler list.
> 6. Kill the server mid-turn, restart, resume the thread → no data loss, no duplicate turns.
>
> **Requirements**
> - Seed script: fixture files, stub or recorded model responses so the run is deterministic and offline-safe.
> - Timing budget printed per beat; total under 4 minutes.
> - `npm run demo` single entry point; exit non-zero on any beat failure.
> - Screenshot capture per beat for the review doc.
>
> **Explicitly not in the prototype demo**
> - Razorpay and balance work (deferred / P2, Q18); Stripe saved-card vault is in scope.
> - WhatsApp mirror — dashboard is primary, mirror is P1 (Q7).
> - Voice memo input — P1 (Q10).
>
> **Acceptance**
> - Clean machine: seed → `npm run demo` → all beats pass, artifacts on disk.
> - A model change does not break the run (recorded fixtures).
> - Beats 4 and 5 fail closed if approval is not granted.

`blockedBy: P0-1, P0-2, P0-3, P0-4`

> Milestone note: the original plan put P0-5 at M2. It is set to **M1** here because the demo
> gates prototype exit — every beat is a prototype feature. Say so if you want it back at M2.

---

## 6. Creation order

1. `save_project` (Always-On Private Assistant, leadTeam KEL).
2. `save_milestone` × 5 (M0–M4).
3. `save_issue_label` — 4 groups, then the 11 children listed above.
4. `save_issue` × 5, in order P0-4 → P0-1 → P0-3 → P0-2 → P0-5 (so `blockedBy` IDs exist).
5. Add relations: P0-4 blocks P0-1/P0-3; P0-1 blocks P0-2; P0-1/2/3/4 block P0-5.
6. Optional: `save_document` on the project with the ADR pointers.

## 7. Git / GitHub / Linear integration — required steps

Current: `/home/ansh/Projects/nebius-nvidia` is **not a git repo** (only `docs/`, `logs/`).
Verified: `gh` 2.102.0 authed as **Anshieee** with `repo` + `workflow` scopes;
`Anshieee/nebius-nvidia` **does not exist yet** (name is free).
`logs/` contains local MCP audit logs — no credential patterns found (checked 2026-10-28),
but they are still excluded from git.

**Do before the first commit**

1. `.gitignore` first: `node_modules/`, `.venv/`, `__pycache__/`, `.env` / `.env.*`
   (keep `!.env.example`), `logs/`, `*.log`, `*.gguf`, `*.safetensors`, `.cache/`, `dist/`,
   `.DS_Store`.
2. Secret scan gate: `gitleaks` (or `detect-secrets`) as a pre-commit hook **and** a CI job.
   `logs/` never gets committed.
3. No model weights in git. If a binary is unavoidable, Git LFS must be configured in the
   same commit that adds it.

**Commands (agent-runnable once approved)**

```bash
cd /home/ansh/Projects/nebius-nvidia
git init -b main
# write .gitignore first, then:
git add .gitignore docs/
git commit -m "chore: scaffold docs + planning pack"
gh repo create nebius-nvidia --private --source=. --remote=origin --push
```

Private is the right default: the product claims privacy and the repo will accumulate
architecture and threat-model detail.

**Manual steps (account owner only)**

4. **Branch protection** on `main` (Settings → Branches): require PR, require CI status,
   no force-push, no direct pushes.
5. **Linear ↔ GitHub** (Linear Settings → Integrations → GitHub, workspace admin): install the
   app, grant access to `Anshieee/nebius-nvidia` **only**, then enable
   - link PRs/branches by issue ID,
   - auto-move to In Progress on branch create,
   - auto-close on merge to `main`.
6. **Branch prefix must be `kel-`** — `kel-123-short-slug`. A `neb-` prefix will not match
   team `KEL` and auto-linking will silently fail.
7. **CI baseline** (GitHub Actions): secret scan, lint, typecheck, unit tests. Add a `demo`
   workflow only after P0-5 exists.

## 8. Not done

- No project, milestone, label, or issue was created in Linear.
- Nothing committed; repo still not initialised.

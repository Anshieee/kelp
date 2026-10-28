# Linear plan — Always-On Private Assistant (P0 ready-to-create)

> **Superseded target:** this doc files into team `NEB`, which does not exist and cannot be
> created via MCP. The active, create-ready pack is **`docs/linear-issue-pack-kel.md`**
> (team `KEL`, branch prefix `kel-`). Content below is kept for rationale.

Status: **prepared, not yet created.** Nothing below has been written to Linear.
Verified: 2026-10-28.

## 1. Verification

| Check | Result |
|---|---|
| Linear MCP auth | OK (OAuth, read + write tools available) |
| Workspace | `kelp` — https://linear.app/kelp-ai (`ad8e918a-296b-4e99-ac70-313019458f5c`) |
| Teams | **only `KEL` (Kelp)** — `NEB` does not exist |
| Projects | none |
| Labels in KEL | `Bug`, `Improvement`, `Feature` (defaults only) |
| Workflow states (KEL) | Backlog, Todo, In Progress, Done, Canceled, Duplicate |
| Users | anshchampion (admin), Linear app |
| MCP team-create tool | **absent** — no `create_team`; team creation is UI/API only |

Blocker: the workspace was created today and contains a single team. Team `NEB` must be
created in the Linear UI (Settings → Teams → Create team, key `NEB`) or via the Linear
GraphQL API with an API key before any issue can be filed there.

Two options:

- **A (as planned):** create team `NEB`, then everything below applies verbatim.
- **B (unblock now):** file into `KEL` and keep branch convention `kel-<n>-<slug>`.
  Project/milestones/labels are identical; only the key changes.

## 2. Structure to create

### Team
`NEB` — display name "Nebius Assistant", key `NEB`, public.

### Project
**Always-On Private Assistant** — lead team `NEB`, lead `anshchampion`, priority High,
state `Planned`.

Summary (≤255 chars):
> Always-on private assistant: local-first agent kernel on Nebius GPU, dashboard-primary chat, approval-gated tools, eval-backed reliability.

Description: goal, non-goals (voice memo, WhatsApp, team sharing are post-MVP), architecture
one-pager link, decision log pointer to `docs/adr/`.

### Milestones

| # | Name | Scope | Target (proposed) |
|---|---|---|---|
| M0 | Foundation | repo scaffold, CI, dev env, model/provider selection, threat model | 2026-11-07 |
| M1 | Prototype | query → tool → answer loop, local persistence, single-user auth, dashboard chat | 2026-11-28 |
| M2 | Polished MVP | restart/resume, health checks, retrieval quality, evals, observability, Nebius GPU deploy | 2026-12-19 |
| M3 | Hardening | privacy controls, secret isolation, rate limits, retention, cost guardrails | 2027-01-23 |
| M4 | Full vision | multi-device presence, proactive tasks, plugin surface, team sharing | 2027-03-06 |

Dates are placeholders — confirm or drop before creating.

### Labels (4 groups + 19 children)

| Group | Children | Color |
|---|---|---|
| `area` | `area/core-agent`, `area/inference`, `area/memory`, `area/tools`, `area/infra`, `area/security`, `area/evals`, `area/ux` | `#5E6AD2` (group), children `#8B93E8` |
| `type` | `type/feature`, `type/bug`, `type/spike`, `type/chore`, `type/docs` | `#4EA7FC` |
| `risk` | `risk/privacy`, `risk/cost`, `risk/reliability` | `#F2994A` |
| `phase` | `phase/prototype`, `phase/mvp`, `phase/vision` | `#27AE60` |

Create each group with `isGroup: true`, then children with `parent: "<group>"`.
Note: KEL already has `Bug` / `Feature` / `Improvement` — do not duplicate; `type/bug`
and `type/feature` are the replacements once the tree exists.

## 3. P0 issues (ready to create)

All in project *Always-On Private Assistant*, milestone per table, state `Todo`,
assignee `anshchampion`.

### P0-1 — Kernel LLM: streaming agent loop with tool calling

- Labels: `area/core-agent`, `area/inference`, `type/feature`, `phase/prototype`, `risk/cost`
- Milestone: M1 — Prototype
- Priority: Urgent

> Build the kernel agent loop that turns a user turn into a streamed answer with tool calls.
>
> **Scope**
> - Single provider interface (Nebius-hosted endpoint) behind an adapter so the model is swappable.
> - Streaming tokens to the dashboard over one persistent channel; tool calls interleaved mid-stream.
> - Tool-call protocol: model emits a typed call → runtime validates → executes → returns observation → model continues.
> - Hard turn budget (max steps, max tokens, wall-clock cap) to bound cost.
> - Structured failure: provider timeout/429/5xx surfaces as a retryable event, not a dropped stream.
> - Usage accounting per turn (tokens in/out, estimated cost) persisted for the cost guardrail in M3.
>
> **Out of scope**
> - Memory and retrieval (separate issue).
> - Multi-agent orchestration.
> - Voice input (P1).
>
> **Acceptance**
> - A turn with 0, 1, and 3 sequential tool calls completes end-to-end against a stub tool.
> - Killing the provider mid-stream yields a visible error event and the session stays usable.
> - Step/token budget is enforced and logged.
> - Unit tests for the loop state machine; one integration test against a recorded fixture.
>
> **Open decisions** — concrete model ID and endpoint (confirm at kickoff), streaming transport (SSE vs WebSocket).

### P0-2 — Dashboard chat surface (primary interface)

- Labels: `area/ux`, `area/core-agent`, `type/feature`, `phase/prototype`
- Milestone: M1 — Prototype
- Priority: Urgent

> The dashboard is the primary product surface. WhatsApp is a P1 mirror, not a parallel build.
>
> **Scope**
> - Threaded chat: message list, streaming assistant turns, retry/stop, error states.
> - Tool-call rendering: collapsed step card per call (name, args summary, status, duration), expandable.
> - Approval prompts render inline in the thread (see P0-3) with approve/deny and a clear rationale.
> - **Answer artifacts:** PDF and CSV results render as an inline preview card with download, plus the textual answer. CSV previews as a table (first N rows); PDF previews page 1 with "open" action.
> - Citation/source chips on answers that used retrieved documents.
> - Session list, rename, resume.
>
> **Out of scope**
> - Saved cards / payments UI (P1, see Q9).
> - Voice memo capture (P1, Q10).
> - Mobile layout beyond a usable responsive fallback.
>
> **Acceptance**
> - A PDF question and a CSV question both produce an inline artifact card that opens correctly.
> - Approval-gated call renders inline and blocks the stream until answered.
> - Reconnect during a stream restores the partial turn without duplicating messages.
> - Playwright smoke covering send → stream → artifact → approval.

### P0-3 — OpenShell: approval-gated tool execution

- Labels: `area/security`, `area/tools`, `type/feature`, `phase/prototype`, `risk/privacy`
- Milestone: M1 — Prototype
- Priority: Urgent

> Every side-effecting action goes through a policy check before it runs, and anything not
> explicitly allowed becomes a human approval request.
>
> **Scope**
> - Capability model: each tool declares effects (`read`, `write`, `network`, `execute`) and a scope (paths, hosts, repos).
> - Policy evaluation: allow / deny / require-approval, default deny for writes and network egress to new hosts.
> - Approval object: what will run, exact args, blast radius, expiry (default 10 min), single-use.
> - Approval channel for the prototype: inline dashboard prompt. Pluggable so a push/mirror channel can be added later.
> - Audit log: append-only record of decision, actor, args hash, result, duration. No secrets in the log.
> - Deny-by-default for shell exec in the prototype; reads under the workspace allowlist only.
>
> **Out of scope**
> - Sandboxing/container isolation beyond process cwd + env scrubbing (M3).
> - Multi-user approval routing.
>
> **Acceptance**
> - A write outside the allowlist produces an approval request and executes only after approval.
> - Denied approval leaves no side effect and records the denial.
> - Expired approval cannot be replayed.
> - Audit log reconstructs a full turn's actions; a grep for secrets in the log returns nothing.

### P0-4 — Skill contracts: schema + capability manifest

- Labels: `area/tools`, `area/core-agent`, `type/feature`, `phase/prototype`
- Milestone: M1 — Prototype
- Priority: High

> Define the contract every skill implements, so tools are addable without touching the kernel.
>
> **Scope**
> - Manifest per skill: name, version, description, JSON-Schema input/output, declared effects and scopes, timeout, idempotency, cost class.
> - Loader: discovery from a directory, schema validation at load, reject invalid manifests at startup (fail fast, named error).
> - Runtime: argument validation before execution, output validation after, typed error envelope (`retryable`, `user_fixable`, `message`).
> - **Prototype skill set (Q8 cut):**
>   1. `web.search` — read-only search, network egress allowlisted.
>   2. `github.read` — read-only repo/issue/PR/file reads via the GitHub API; token from env, never logged.
>   3. `docs.answer` — PDF/CSV ingest + question answering, returns answer + artifact handle.
>   4. `schedule.create` — create a scheduled task (feeds the scheduler), requires approval.
> - Contract tests: a golden manifest fixture plus a negative test per validation rule.
>
> **Out of scope**
> - Third-party plugin installation and signing (M4).
> - GitHub write operations (post-prototype; would require its own approval policy).
>
> **Acceptance**
> - Adding a new skill requires only a manifest + handler, zero kernel edits.
> - Invalid manifest fails startup with the offending field named.
> - All four prototype skills pass contract tests and are callable from P0-1's loop under P0-3 policy.
>
> **Decision to record (ADR)** — whether `docs.answer` is one skill or `pdf.extract` + `csv.query` behind it.

### P0-5 — Demo choreography: scripted end-to-end run

- Labels: `area/ux`, `area/evals`, `type/chore`, `phase/prototype`
- Milestone: M2 — Polished MVP
- Priority: High

> One deterministic script that exercises the whole prototype story, used for review and as
> the regression harness for the demo path.
>
> **Beats**
> 1. Dashboard chat open, ask a question that needs `web.search` → answer with source chips.
> 2. Ask "summarize this PDF" (fixture) → PDF artifact card; ask a CSV question → table preview + numeric answer.
> 3. Ask a question about a GitHub issue → `github.read`, read-only, no approval needed.
> 4. Ask for something that writes → approval prompt inline → approve → action executes → audit entry visible.
> 5. Ask to schedule a recurring task → approval → task appears in scheduler list.
> 6. Kill the server mid-turn, restart, resume the thread → no data loss, no duplicate turns.
>
> **Requirements**
> - Seed script: fixture files, stub or recorded model responses so the run is deterministic and offline-safe.
> - Timing budget printed per beat; total under 4 minutes.
> - `npm run demo` (or equivalent) single entry point; exit non-zero on any beat failure.
> - Screenshot capture per beat for the review doc.
>
> **Explicitly not in the prototype demo** (P1, per Q8/Q9/Q10)
> - Saved payment cards via Stripe vault — dashboard flow, post-prototype.
> - WhatsApp mirror (Q7: dashboard is primary; mirror is P1).
> - Voice memo input (Q10: P1).
>
> **Acceptance**
> - Clean machine: seed → `npm run demo` → all beats pass, artifacts on disk.
> - A model change does not break the run (recorded fixtures).
> - Beats 4 and 5 fail closed if approval is not granted.

## 4. Decisions locked in (context for the issues)

| ID | Decision |
|---|---|
| Q7 | Dashboard is primary; WhatsApp is a mirror, P1 |
| Q8 | Prototype cut = dashboard chat, PDF/CSV answers, search + GitHub read-only, approvals + scheduler |
| Q9 | Saved cards via Stripe vault — dashboard flow, post-prototype |
| Q10 | Voice memo — P1 only |

## 5. Git / GitHub / Linear integration requirements

Current state of `/home/ansh/Projects/nebius-nvidia`: **not a git repo.** Contains
`docs/linear-setup.md`, `docs/linear-p0-plan.md`, `logs/` (local MCP logs). No source yet.

`git init` + push needs:

1. `.gitignore` **before** the first commit — `node_modules/`, `.venv/`, `__pycache__/`,
   `.env*` (keep `.env.example`), `logs/`, `*.log`, model weights (`*.gguf`, `*.safetensors`),
   `.cache/`, `dist/`, `.DS_Store`.
2. Secret hygiene: `logs/` must not be committed (MCP audit logs). Add a pre-commit secret
   scan (gitleaks or `detect-secrets`) and a CI job running it.
3. `git init -b main`, initial commit of docs + scaffold, then
   `gh repo create nebius-nvidia --private --source=. --remote=origin --push`.
   **Private repo is recommended** — the product claims privacy and the repo will accumulate
   architecture and threat-model detail.
4. Large files: no model weights in git; if unavoidable use Git LFS and configure it in the
   same commit that adds the first binary.
5. Branch protection on `main`: require PR, require CI, no force-push, no direct pushes.
6. Linear ↔ GitHub integration (Linear Settings → Integrations → GitHub, workspace admin):
   - Install the Linear GitHub app, grant access to `Anshieee/nebius-nvidia` only.
   - Enable: link PRs/branches by issue ID, auto-move to In Progress on branch create,
     auto-close on merge to `main`.
   - The branch prefix must match the team key. `neb-123-slug` only works if team `NEB`
     exists; with team `KEL` use `kel-123-slug`. Mismatch silently breaks auto-linking.
7. CI baseline (GitHub Actions): secret scan, lint, typecheck, unit tests; add a `demo`
   workflow only once P0-5 exists.

Manual steps only the account owner can do: create team `NEB`, install the GitHub app,
create the GitHub repo, set branch protection.

# Review guide: KEL-6 and KEL-7 before KEL-8

Two branches are ready for review. Both are self-contained Python slices with offline
tests and no UI; KEL-8 (dashboard chat surface) builds directly on top of them, so
this review is the last checkpoint before work gets a frontend.

| Slice | Branch | Head | What it adds |
|---|---|---|---|
| KEL-6 | `kel-6` | `4c72afc` | Streaming Token Factory client + agent loop + usage accounting |
| KEL-7 | `kel-7` | `ef6ea9f` | OpenShell policy, 10-minute single-use approvals, gated exec, audit log, Stripe placeholder |

Both branches branch off `main` at `eae9027` (payments decision docs), so the two
diffs are disjoint (kernel vs openshell + payments) and can be reviewed and merged
in either order without conflicts.

---

## 1. What these slices do, in plain words

### KEL-6: the assistant's "engine room"

This is the piece that actually talks to the AI model. Today the repo has skills
(what tools exist, from KEL-5) but nothing that calls a model. KEL-6 adds:

- **A connection to Nebius Token Factory** — the hosted endpoint that runs the
  Nvidia Nemotron models. It streams: words arrive piece by piece instead of
  after a long pause, which is what makes the final chat feel fast.
- **One "turn" logic.** You send one message; the loop sends it to the model,
  and if the model decides to use a tool (search the web, read a file), it runs
  that tool, hands the result back, and lets the model continue — repeating
  until the answer is done or the turn runs out of room.
- **Guard rails on cost and runaway behavior.** A turn can use at most 6 model
  steps, ~4096 output tokens, 8 tool calls, and 2 minutes. Going over stops the
  turn cleanly; the session stays usable.
- **Metering.** Every turn records how many tokens went in and out and an
  estimated dollar cost, appended to a private JSONL file. This is the data the
  later cost guardrail (M3) needs.
- **Graceful failure.** If the provider is down, rate-limited, or times out,
  the user sees a typed error event instead of a crashed app. A missing API key
  does not stop startup; the first call reports it cleanly.

Out of scope on purpose: chat UI, memory, prompts/history assembly, Telegram —
those are the adapters and later slices.

### KEL-7: the "locks on the doors"

This is the security layer between "the model wants to do something" and
"something actually happens". Default deny; three jobs:

- **Policy.** Every action is checked against declared capability: reads must
  stay inside the workspace folder, network egress only to allowlisted hosts,
  writes inside the workspace need a human yes, anything else is denied, and
  running shell commands is off unless explicitly switched on per deployment —
  and even then always needs a human yes.
- **Approvals.** Anything not explicitly allowed becomes an approval request:
  it shows exactly what will run, expires 10 minutes later, works only once,
  and cannot be replayed. A denial leaves nothing changed.
- **Audit.** Every decision and execution is appended to a log file, with
  secrets, command output bodies, and environment values scrubbed before they
  hit disk. A full turn's actions can be reconstructed from it.

It also includes a **Stripe placeholder**: a tiny module that reads the Stripe
key from the server environment and reveals only the mode ("test"/"live"),
nothing else. No network calls yet — real payments are KEL-10, once the
account owner provides a test key.

Also out of scope on purpose: the approval *buttons/UI* (that is KEL-8), OS
sandboxing (M3), and the policy editor.

---

## 2. Dev-level details

### KEL-6 (branch `kel-6`, head `4c72afc`)

Diff vs `main`: +2,024 lines, 12 files. No existing file is modified except
`pyproject.toml` (adds `httpx>=0.27,<1`); tests are the only existing-file touches.

| File | Contents |
|---|---|
| `kernel/provider.py` | `TokenFactoryClient`: OpenAI-compatible `POST /chat/completions` with `stream: true`, `stream_options.include_usage: true`; tool deltas assembled into `ToolCallReady` with parsed arguments; redirects disabled; HTTP statuses mapped to typed errors with a retryable flag; error bodies never echoed into events. Built on `httpx.AsyncClient` (new dependency). |
| `kernel/loop.py` | `AgentLoop.run_turn()` — the state machine. Emits typed events: `turn_started`, `token_delta`, `tool_call_started/finished`, `usage_updated`, `error`, `turn_finished` (all JSON-serializable with `to_dict()`). Tool execution is fully delegated to the injected executor (`RegistryToolExecutor` from the KEL-5 registry), so validation, approval hook, timeout, and error envelopes are unchanged. No exception crosses `run_turn`. |
| `kernel/config.py` | `KernelConfig.from_env()`: `NEBIUS_API_KEY`, `NEBIUS_TOKEN_FACTORY_BASE_URL` (legacy `NEBIUS_BASE_URL` accepted), `KELP_PRIMARY_MODEL` (default `nvidia/Nemotron-3_5-Super-VL`), `KELP_BACKGROUND_MODEL` (`Nemotron-3_5-Lightning` for background roles only). `DEFAULT_PRICING` holds USD-per-1M list prices; unknown models get `cost_known: false` instead of an invented price. |
| `kernel/usage.py` | `UsageRecord` + `JsonlUsageStore`: one record per turn, appended with `fsync`, file created `0600`. Contains counts and identifiers only — no prompts, responses, or secrets. A broken or missing store flags `usage_persisted: false`; accounting never kills a turn. |
| `kernel/events.py` | The typed event definitions shared by provider and loop. |
| `.env.example` | Env-var template, keys only, no values; `.env` and `var/` are gitignored. |
| `tests/test_kernel_provider.py`, `tests/test_kernel_loop.py` | ~675 lines, fully offline via `httpx.MockTransport`: streaming split across chunks, mid-stream tool calls, 0/1/3 sequential tool calls, provider failure mid-stream, budget exhaustion, missing key, non-reporting usage backends, unpersisted usage. |

Budget table (defaults): `max_steps` 6, `max_output_tokens` 4096,
`max_tokens_per_step` 1024, `max_tool_calls` 8, `max_wall_seconds` 120.
Exceeding any emits `error.code = "turn_budget_exceeded"` with `details.limit`,
then `turn_finished(reason="budget")`.

### KEL-7 (branch `kel-7`, head `ef6ea9f`)

Diff vs `main`: +2,380 lines, 16 files, all new. Nothing existing is modified.

| File | Contents |
|---|---|
| `openshell/policy.py` | `PolicyEngine.evaluate()` returns `allow` / `require_approval` / `deny`; worst result across a capability's effects wins. Read: only inside `${WORKSPACE}`, symlinks resolved, `..` cannot escape. Write: in-scope needs approval, out-of-scope denied. Network: exact hosts or leading `*.` wildcard only — the head fix additionally rejects IP literals in the allowlist. Execute: denied unless the deployment sets `allow_exec=True` AND argv0 is allowlisted AND cwd is in workspace — and even then always approval. Unknown capability: deny. |
| `openshell/approvals.py` | `ApprovalStore`: `pending -> approved -> consumed` or `denied`/`expired`; TTL 600 s, always single-use; `argument_hash` is `sha256:` over canonical JSON; blast-radius targets are effect-labelled (`fs:write:...`, `net:...`, `exec:...`, `cwd:...`); `consume` is atomic under a lock (second call gets `already_used`); expired requests cannot be approved, denied, or consumed. Model-facing `to_dict()` omits command and metadata. |
| `openshell/exec_gate.py` | `request()` (policy check + approval object), `approve()` (human decision), `execute()` (re-checks policy at execution time, atomic consume, spawn). argv must be a sequence — a shell string is rejected, no `shell=True` path. Child gets a minimal environment and no variable whose name looks secret-like (`*TOKEN*`, `*KEY*`, `*SECRET*`, `*PASSWORD*`, `*CREDENTIAL*`, `*AUTH*`, `*PRIVATE*`). stdin is `/dev/null`, own process session, timeout kills the whole process group; stdout/stderr capped, scrubbed, length + SHA-256 hashed for the audit. |
| `openshell/audit.py` | Append-only JSONL, mode `0600`. Events per turn: `exec_requested`, `approval_requested`, `approval_granted`/`approval_denied`, `exec_started`, `exec_finished`, and `exec_blocked` with an `outcome` (`not_approved`, `denied`, `expired`, `already_used`, `policy_denied`). Redaction: declared secret-looking env values replaced with `[redacted]`; plus `Bearer ...`, `sk_live_`/`sk_test_`, and `token=`/`password:` patterns scrubbed even when undeclared. Never stores stdout/stderr bodies or env dumps. |
| `openshell/errors.py`, `openshell/__init__.py` | Typed error codes and package exports. |
| `payments/stripe_wallet.py`, `payments/.env.example` | Placeholder: reads `STRIPE_SECRET_KEY` from server env only, exposes only the mode (`test`/`live`/`unknown`); `repr`/`str` redact the value. No SDK, no network calls. |
| `tests/test_openshell_*.py`, `tests/test_payments.py` | ~1,079 lines: workspace/symlink escape, host and IP-literal allowlist rules, approval lifecycle including expiry and replay, exec gating and env scrubbing, audit reconstruction and secret-free grep, payment placeholder redaction. |

Relationship to KEL-5: the skill loader still rejects the `execute` effect in
v1.0 manifests. This gate is a separate, explicitly enabled capability; the two
connect later. Manifest-approved skills keep going through the KEL-5 approval
hook; OpenShell is the additional wall for anything with side effects.

---

## 3. Optimal review procedure

Run this top to bottom. The unit runs on `main`; the checkout steps change your
working tree, so commit or stash anything pending first (`git status` should show
a clean tree, nothing to touch in this repo).

### 3.1 Setup and branch inspection

```bash
git fetch origin --prune
git log --oneline -3 origin/kel-6   # expect: 4c72afc -> 4dd2e84 -> eae9027
git log --oneline -3 origin/kel-7   # expect: ef6ea9f -> dad8d80 -> eae9027
git diff main..origin/kel-6 --stat  # +2,024 lines, kernel/ + docs/KERNEL.md + 2 test files
git diff main..origin/kel-7 --stat  # +2,380 lines, openshell/ + payments/ + docs/OPENSHELL.md + 5 test files
```

To read branch files without checking out:

```bash
git show origin/kel-6:docs/KERNEL.md
git show origin/kel-7:docs/OPENSHELL.md
```

To run the tests you must check out (a block of `git show` / one-off script;
using a temporary worktree also works):

```bash
git checkout kel-6 && python -m pytest && git checkout main
git checkout kel-7 && python -m pytest && git checkout main
```

All tests are offline; no API keys are needed for either suite. The only new
runtime dependency is `httpx` (installed by `pip install -e .` if needed).
Additional manual checkouts worth a minute while on each branch:

```bash
# on kel-6:
grep -rn "bare_token\|Authorization\|api_key" kernel/ | grep -v test   # key only in headers, never logged
grep -rn "print( logging.info(" kernel/                                # no stray payload logging
# on kel-7:
grep -rE "sk_(live|test)_[A-Za-z0-9]{8,}" tests/ -l                    # no real-looking keys
grep -rn "shell=True" openshell/                                       # must return nothing
```

### 3.2 GitHub compare pages

Open the compare view `main...kel-6` and `main...kel-7` on the repo
(https://github.com/Anshieee/nebius-nvidia/compare/main...kel-6 and
`...kel-7`). Suggested reading order:

1. **Files changed tab.** Confirm the two diffs touch disjoint directories
   (kernel vs openshell/payments) and that no existing skill or loader file is
   modified by either.
2. **kel-6, read in this order:** `docs/KERNEL.md` (the design contract, 130
   lines) -> `kernel/config.py` -> `kernel/provider.py` -> `kernel/loop.py` ->
   `kernel/usage.py` -> skim both test files to confirm each promise in the doc
   has a matching test.
3. **kel-7, same pattern:** `docs/OPENSHELL.md` -> `openshell/policy.py` ->
   `openshell/approvals.py` -> `openshell/exec_gate.py` -> `openshell/audit.py`
   -> `payments/stripe_wallet.py` -> the five test files.
4. **The two fix commits** (`4c72afc` unpersisted-usage reporting,
   `ef6ea9f` IP-literal rejection): use "Commits" tab, confirm each fix has a
   test covering the exact regression it fixes.
5. **Check returns:** green CI if configured, no accidentally committed files
   (`.env` values, usage JSONL, or audit files must not appear; `var/` is
   gitignored on both branches).

### 3.3 Linear checks

- **KEL-6** — https://linear.app/kelp-ai/issue/KEL-6/kernel-llm-streaming-agent-loop-with-tool-calling
  Confirm scope items 1-6 map to shipped code; accept criteria cross-checked
  in section 4; the open decision "transport (SSE vs WebSocket)" remains open
  by design (no UI in this slice).
- **KEL-7** — https://linear.app/kelp-ai/issue/KEL-7/openshell-approval-gated-tool-execution-audit
  Note the acceptance criterion about the approval prompt rendering inline is
  *deliberately deferred to KEL-8* — here, the pluggable approval object is the
  deliverable, so do not block the merge for missing UI.
- Move both issues to review/in-progress status or attached PRs as the team's
  flow requires, so KEL-8 can reference them.

### 3.4 Docs to read alongside

| Doc | Branch | Why |
|---|---|---|
| `docs/KERNEL.md` | `kel-6` | 130 lines, written for this review; tables for env vars, budgets, HTTP status mapping. |
| `docs/OPENSHELL.md` | `kel-7` | 105 lines, same style; policy effects, approval lifecycle, audit redaction rules, payments placeholder limits. |
| `docs/SKILL_SPEC.md` | both (same version) | v1.0 contract from KEL-5; KEL-6 consumes it for tool schemas, KEL-7 §1 and §8.2 consume it for declared effects and scopes. |
| `docs/adr/0001-docs-answer-single-skill.md` | both | Context for why `docs.answer` is one skill; fine to skip. |
| Linear issue body of KEL-6 and KEL-7 | web | Authoritative acceptance criteria. |

---

## 4. Acceptance criteria, evidence, and what to verify

### KEL-6

| Linear acceptance | Evidence to look for while reviewing |
|---|---|
| A turn with 0, 1, 3 sequential tool calls completes against a stub tool | `tests/test_kernel_loop.py`: tool-call turn tests over `RegistryToolExecutor` |
| Killing the provider mid-stream yields a visible error, session usable | `test_kernel_provider.py`: mid-stream `MockTransport` failures; `loop.py` guarantees no exception escapes `run_turn` |
| Step/token budget enforced and logged | `test_kernel_loop.py`: `max_steps` / `max_output_tokens` / `max_tool_calls` / `max_wall_seconds` exhaustion -> `turn_budget_exceeded` -> `turn_finished(reason="budget")` |
| Unit tests for the loop state machine | Both test files, fully offline (`httpx.MockTransport`) |
| One integration test against a recorded fixture | **Not in this slice, intentionally.** The fixture needs a live Token Factory key. `docs/KERNEL.md` section 4 names this as deferred. Verify the issue is opened/tracked (KEL-8 tip or follow-up) before approval; do not block the merge, but confirm the decision is on record. |

Also verify: usage record structure matches `docs/KERNEL.md` section 3 sample
(input, output, total, estimated cost, `cost_known`, `usage_reported`,
`usage_persisted`), and check that zero prices are invented for unknown models
(`cost_known: false`).

### KEL-7

| Linear acceptance | Evidence to look for while reviewing |
|---|---|
| Write outside allowlist -> approval request, executes only after approval | `test_openshell_policy.py` + `test_openshell_approvals.py` + `test_openshell_exec_gate.py` |
| Denial leaves no side effect, records denial | approval state machine denial path; audit `approval_denied` event; child never spawned |
| Expired approval cannot be replayed | expiry tests with rewritten submitted-at timestamps; expired cannot be approved, denied, or consumed |
| Audit log reconstructs a full turn | `test_openshell_audit.py` — one JSONL record per event with turn_id grouping |
| grep secrets in the log returns nothing | review the redaction pipeline in `audit.py` (declared env values + undeclared `Bearer`/`sk_...`/`token=`/`password:` patterns + nested fields) and confirm no stdout/stderr body is ever written (only length + SHA-256) |

---

## 5. Signals that should block merge (and ones that should not)

Block if any of these shows up:

- Secrets or audit usage files are committed, or `var/` is not gitignored on both branches.
- Provider or audit code logs tokens, prompts, error bodies, secret values, or `Authorization`/`NEBIUS_API_KEY` anywhere.
- OpenShell: `shell=True` or any string-command path; an approval that is not single-use and 10-minute-bounded; policy not re-checked at execution time.
- Budget exhaustion kills the process or the session instead of the turn; a missing or broken usage store raises instead of flagging `usage_persisted: false`.
- The `httpx` dependency is missing or pinned loosely (<0.27 or >=1).

Do not block for:

- No chat surface, no approval UI, no recorded-fixture integration test, no real Stripe calls — those are KEL-8, KEL-10, and live-key follow-ups.
- Default pricing values being hardcoded — the doc says to bump them when Token Factory prices change; that is future maintenance, not a defect.

## 6. Verdict

Approve `kel-6` and `kel-7` for merge when: both test suites pass locally,
both docs exist and match the code, the disjoint-diffs claim holds, the gaps
above are explicitly recorded on the issues (deferred fixture test; UI in
KEL-8; Stripe in KEL-10), and no blocking signal from section 5 appears.
Merging order does not matter; merging both before KEL-8 keeps the frontend
slice on top of stable foundations.

# Skill contract spec (v1.0)

- **Status:** Accepted, v1.0 — 2026-10-28
- **Issue:** KEL-5 / P0-4 (milestone M1 — Prototype)
- **Normative language:** MUST / MUST NOT / SHOULD per RFC 2119
- **Reference implementation:** `skills/` (loader, meta-schema, four prototype manifests)
- **Contract tests:** `tests/test_skill_loader.py` (`python -m pytest`)

This is the contract between the kernel agent loop (P0-1), the approval gate (P0-3), and every
tool the assistant can call. A skill is a **manifest** plus an **async handler**. Adding one
MUST NOT require editing kernel code.

Surfaces (dashboard, Telegram, the WhatsApp mirror) are adapters over the same contract and are
not defined here.

---

## 1. Scope and non-goals

In scope for v1.0:

- the manifest shape and its validation rules;
- discovery and fail-fast loading;
- input/output JSON-Schema validation;
- declared capabilities (`effects`, scopes, secrets) consumed by P0-3;
- a typed error envelope returned for every invocation;
- the four prototype skills: `web.search`, `github.read`, `docs.answer`, `schedule.create`.

Out of scope (explicitly deferred):

| Deferred | Tracked by |
|---|---|
| Third-party skill installation, signing, version pinning | M4 |
| GitHub write operations (comment, label, merge, push) | post-prototype, needs its own approval policy |
| Shell/`execute` capability | M3 (sandboxing); v1.0 rejects it outright |
| Multi-tool manifests | v1.1 if needed (see §5) |
| Approval UI, policy engine, audit log | P0-3 |
| Streaming, token/turn budgets, usage accounting | P0-1 |
| Real handler bodies for the four skills | this slice ships stubs (§11) |

---

## 2. Invariants

| ID | Invariant |
|---|---|
| I1 | A skill is a directory with `manifest.json` + a handler referenced by `entry`. No kernel edits. |
| I2 | Loading is fail-fast and all-or-nothing: any invalid manifest aborts startup; no partial registry. |
| I3 | Arguments are validated before execution; output is validated after execution. |
| I4 | `invoke()` never raises for skill-level failures; it returns the error envelope in §8. |
| I5 | Read by default. `write` requires approval. `execute` is forbidden in v1.0. |
| I6 | Secrets are referenced by env-var name only; values are resolved at invocation and never logged, never placed in manifests, never echoed in errors. |
| I7 | Manifests are trusted local files (unsigned plugins are M4). Remote schema `$ref`s are rejected. |
| I8 | Every handler call has an enforced timeout. |
| I9 | The declared schemas are the whole interface. The kernel MUST NOT special-case skill names. |

---

## 3. Layout and discovery

```
skills/
  manifest.schema.json        # meta-schema for manifests
  loader.py                   # discovery, validation, registry, invoke
  errors.py                   # error types + envelope codes
  context.py                  # SkillContext + ApprovalRequest
  builtin/
    web_search/manifest.json + handler.py
    github_read/…
    docs_answer/…
    schedule_create/…
```

Rules:

- The loader scans **immediate subdirectories** of the skill root (`skills/builtin` by default).
- Entries starting with `.` or `_` are ignored (packaging and `__pycache__`).
- Every other subdirectory MUST contain `manifest.json`. A stray directory is a startup error.
- The root MUST exist, MUST contain at least one skill, and skill names MUST be globally unique.
- Loading happens once at process start, before the first request. The registry is immutable.

The reference implementation is Python; the contract is language-neutral. If the kernel lands
in another runtime, port the shapes and rules, not the files.

---

## 4. Manifest shape

A complete example (`web.search`, abbreviated schemas):

```json
{
  "manifest_version": "1.0",
  "name": "web.search",
  "version": "0.1.0",
  "description": "Search the public web and return ranked results with titles, URLs, and snippets.",
  "triggers": [
    {
      "kind": "model",
      "description": "Use when the user asks about recent events or external facts.",
      "examples": ["What is the latest Nebius GPU pricing?"]
    }
  ],
  "tools": ["web.search"],
  "input_schema": { "type": "object", "additionalProperties": false, "…": "…" },
  "output_schema": { "type": "object", "additionalProperties": false, "…": "…" },
  "permissions": {
    "effects": ["read", "network"],
    "scopes": { "paths": [], "hosts": ["api.tavily.com"], "repos": [] },
    "requires_approval": false,
    "secrets": ["TAVILY_API_KEY"]
  },
  "entry": { "module": "skills.builtin.web_search.handler", "callable": "run" },
  "timeout_ms": 15000,
  "idempotent": true,
  "cost_class": "metered"
}
```

### 4.1 Field reference

| Field | Type | Rules |
|---|---|---|
| `manifest_version` | string | const `"1.0"`. Bumped when the shape changes incompatibly. |
| `name` | string | Dotted namespace, 2+ segments, each `[a-z][a-z0-9_]{1,31}` (2–32 chars). Unique. Equals the primary tool name. |
| `version` | string | SemVer `MAJOR.MINOR.PATCH[-pre][+build]`. |
| `description` | string | 10–500 chars. Written for the model: when to use it, what it returns. |
| `triggers` | array | 1–8 entries. At least one `kind: "model"`. Advisory metadata only (§6). |
| `tools` | array | **Exactly one** name in v1.0, equal to `name`. |
| `input_schema` | object | JSON Schema (Draft 2020-12). Root `type: object`; root `additionalProperties: false`. No remote `$ref`. |
| `output_schema` | object | JSON Schema (Draft 2020-12). Root `type: object`. `additionalProperties: false` strongly recommended. No remote `$ref`. |
| `permissions.effects` | array | Non-empty, unique subset of `read`, `write`, `network`, `execute`. `execute` rejected in v1.0. |
| `permissions.scopes.paths` | array | Workspace-relative globs starting with `${WORKSPACE}/`; no `..`, no backslashes. |
| `permissions.scopes.hosts` | array | Bare lowercase DNS names or a leading `*.` wildcard. No scheme, port, path, or IP literal. |
| `permissions.scopes.repos` | array | `owner/name` (GitHub charset) or `*`. |
| `permissions.requires_approval` | boolean | MUST be `true` for `write` and for non-idempotent skills. |
| `permissions.secrets` | array | Env-var names matching `^[A-Z][A-Z0-9_]{2,63}$`. Never values. |
| `entry.module` | string | Importable dotted Python module. |
| `entry.callable` | string | Name of an **async** function in that module. |
| `timeout_ms` | integer | 100–600000. Enforced per invocation. |
| `idempotent` | boolean | `false` means a retry can duplicate side effects and therefore forces approval. |
| `cost_class` | string | `free` \| `metered` \| `expensive`. Budgeting hint for P0-1/M3. |

Manifest files are capped at 256 KiB. Unknown top-level or nested fields are rejected
(`additionalProperties: false`); the meta-schema is `skills/manifest.schema.json`.

### 4.2 Naming

Names are the model-facing tool identifiers and the registry keys. They MUST be stable: renaming
a skill is a breaking change for prompts, evals, and audit records. Use one namespace segment
per domain (`web.`, `github.`, `docs.`, `schedule.`).

---

## 5. Tools

In v1.0, one skill exposes exactly one model-facing tool, and its name equals `name`. This is
deliberate: approval policy, audit records, and eval fixtures are keyed by tool name, so a 1:1
mapping keeps all three unambiguous.

Skills with several related operations use an **input discriminator** instead of multiple tools
(`github.read` uses `kind: "repo" | "issue" | "pull_request" | "file"`). Multi-tool manifests are
deferred to v1.1; the loader rejects more than one entry today.

---

## 6. Triggers

`triggers` tells the kernel/prompt builder when the skill is worth surfacing. It is **advisory,
never authorization**: no trigger value can widen permissions.

| `kind` | Meaning |
|---|---|
| `model` | Candidate for the LLM tool list (required in v1.0). |
| `schedule` | Reachable from a scheduler-created task. |
| `manual` | Reachable from a UI action (e.g. a document card follow-up). |
| `event` | Reachable from an inbound event (post-prototype). |

`description` is a one-sentence routing hint; `examples` are short user utterances (max 5).
Triggers are not a security boundary and are not validated against user input.

---

## 7. JSON-Schema contract

- Dialect: **Draft 2020-12**. The loader compiles every schema with `check_schema` at startup.
- Root MUST be `type: "object"` for both input and output.
- **Input** MUST set `additionalProperties: false` at the root, so hallucinated or stale
  arguments fail loudly instead of being silently ignored.
- **Output** SHOULD set `additionalProperties: false`; strict output catches handler drift.
- `$ref` MAY point inside the same document (`#/...`). Any non-local reference is rejected at
  load time (no network fetch, no SSRF surface).
- `format` is treated as an annotation: v1.0 does not run a format checker. Do not rely on
  `format` for validation; encode hard constraints as `pattern`/`enum`/bounds.
- Defaults in schemas are documentation only. The runtime does **not** inject them; handlers
  apply defaults.
- Schema validation covers presence, types, bounds, enums, `oneOf`/`anyOf`, and nesting. It does
  not execute custom keywords or remote code.

At runtime:

1. arguments MUST be a JSON object (`Mapping`); anything else is `invalid_arguments`;
2. `input_schema` validation runs before approval, so a malformed call never reaches the gate;
3. `output_schema` validation runs after the handler returns and before the envelope is
   returned;
4. output MUST be a `Mapping` and JSON-serializable.

Error details carry JSON Pointers (`/query`, `/results/0/url`). `""` addresses the root.

---

## 8. Permissions, approval, and secrets

### 8.1 Effects

| Effect | Meaning | v1.0 rule |
|---|---|---|
| `read` | Reads local or remote state. | Allowed. |
| `network` | Egress to a remote host. | Allowed only with a non-empty `hosts` allowlist. |
| `write` | Mutates local or remote state. | Requires `requires_approval: true` **and** a non-empty `paths` or `repos` scope. |
| `execute` | Spawns processes or shells. | **Rejected.** Shell execution is deny-by-default until M3. |

Cross-field rules enforced by the loader:

- `write` ⇒ `requires_approval: true` and at least one path or repo scope;
- `network` ⇒ at least one host;
- `idempotent: false` ⇒ `requires_approval: true`;
- `execute` in `effects` ⇒ manifest rejected.

### 8.2 Scope grammar

| Scope | Accepted | Examples |
|---|---|---|
| `paths` | `${WORKSPACE}/…` glob; no `..`, no `\` | `${WORKSPACE}/var/artifacts/**` |
| `hosts` | lowercase DNS or leading `*.` wildcard; no scheme/port/path/IP | `api.github.com`, `*.example.com` |
| `repos` | `owner/name` or `*` | `Anshieee/nebius-nvidia` |

Scopes are **declared capability**, not enforcement. P0-3 resolves `${WORKSPACE}`, checks the
actual target against these lists, and decides allow / deny / require approval. A skill that
exceeds its declared scope is a policy violation even if the manifest was valid.

### 8.3 Approval

The loader enforces the `requires_approval` flag, not the policy itself:

1. if the skill requires approval, the loader builds an `ApprovalRequest` and calls the injected
   async hook **before** the handler;
2. no hook configured ⇒ `approval_required` envelope containing the request (handler does not
   run);
3. hook returns `false` **or raises** ⇒ `approval_denied`, fail closed, nothing runs;
4. hook returns `true` ⇒ the handler runs exactly once.

`ApprovalRequest` fields:

| Field | Notes |
|---|---|
| `skill`, `tool` | Registry identity. |
| `arguments` | Exact arguments, available to the gate/UI. |
| `argument_hash` | `sha256:<hex>` of canonical JSON (`sort_keys`, compact separators). |
| `effects`, `scopes` | Copied from the manifest for the policy decision. |
| `manifest_version` | Which contract version produced the call. |
| `requested_at`, `expires_at` | ISO-8601 UTC; default TTL **10 minutes**. |
| `single_use` | Always `true`. Replay is P0-3's responsibility. |

The envelope copy of the approval request (`result["approval"]`) omits raw `arguments`; the
model sees the hash and scope, not the payload. Approval wait time is **not** counted against
`timeout_ms`; the timeout applies to handler execution only.

### 8.4 Secrets

- Manifests list env-var **names** in `permissions.secrets`; values never appear in a manifest.
- The loader resolves only declared names, from the process env or an explicit `secrets`
  mapping passed by the caller (tests, secret manager).
- Handlers read values through `context.secret(name)`. A missing value raises
  `missing_secret` when accessed — configuration problems surface as a typed envelope, not a
  crash, and only if the code path needs the secret.
- The context `repr`/`str` redacts secrets. Error messages and details are scrubbed by replacing
  known values of length ≥ 4 with `[redacted]`. Scrubbing is best-effort; the primary rule is
  that handlers never log arguments or secret values.

---

## 9. Handler contract

```python
async def run(arguments: Mapping[str, Any], context: SkillContext) -> Mapping[str, Any]:
    ...
```

- MUST be `async` (`inspect.iscoroutinefunction`); a sync handler fails at load.
- MUST return a JSON-serializable object matching `output_schema`.
- MUST NOT log arguments, secrets, or raw provider payloads. Audit belongs to P0-3.
- MUST NOT perform I/O outside its declared scopes (paths, hosts, repos).
- MUST honour `idempotent`: if `true`, repeating the same call is safe; if `false`, the caller
  must not auto-retry.
- SHOULD raise `SkillHandlerError(code, message, retryable=…, user_fixable=…, details=…)` to
  choose a specific failure; any other exception becomes `handler_error`.
- MUST NOT spawn processes (no `execute` capability exists).
- MAY use `context.workspace_root`, `context.secret`, `context.has_secret`, `context.scrub`.

Handler exceptions never escape `invoke()`; they are converted to envelopes.

---

## 10. Invocation pipeline

```
invoke(tool, arguments)
  ├─ unknown tool                    → unknown_skill
  ├─ arguments is not an object      → invalid_arguments
  ├─ input_schema validation         → invalid_arguments (field + details)
  ├─ requires_approval?
  │    ├─ no hook                    → approval_required (request attached)
  │    ├─ hook false/raises          → approval_denied
  │    └─ hook true                  → continue
  ├─ handler under timeout
  │    ├─ timeout                    → timeout (retryable)
  │    ├─ SkillHandlerError          → its code
  │    └─ other exception            → handler_error
  ├─ output is object + serializable → else invalid_output
  ├─ output_schema validation        → invalid_output
  └─ success envelope
```

### 10.1 Success envelope

```json
{
  "ok": true,
  "skill": "web.search",
  "tool": "web.search",
  "duration_ms": 412,
  "idempotent": true,
  "output": { "query": "…", "backend": "tavily", "results": [] }
}
```

### 10.2 Error envelope

```json
{
  "ok": false,
  "skill": "schedule.create",
  "tool": "schedule.create",
  "duration_ms": 3,
  "error": {
    "code": "invalid_arguments",
    "message": "Invalid arguments for 'schedule.create': /schedule/expr: '' does not match …",
    "retryable": false,
    "user_fixable": true,
    "field": "/schedule/expr",
    "details": { "errors": [ { "field": "/schedule/expr", "message": "…" } ] }
  }
}
```

| Field | Type | Notes |
|---|---|---|
| `ok` | boolean | Always present. |
| `skill`, `tool` | string | Registry identity; for `unknown_skill` both echo the requested tool. |
| `duration_ms` | integer | Whole pipeline, rounded. |
| `error.code` | string | One of the codes below. |
| `error.message` | string | One plain sentence, no stack trace, no secrets. |
| `error.retryable` | boolean | Safe to retry the same call unchanged? |
| `error.user_fixable` | boolean | Can the person fix it (bad args, denied approval)? |
| `error.field` | string? | JSON Pointer to the first failing field. |
| `error.details` | object? | Structured errors; never raw payloads. |
| `approval` | object? | Present only for `approval_required`. |

| Code | Retryable | User-fixable | Meaning |
|---|---|---|---|
| `unknown_skill` | no | yes | No such tool; the model picked an unknown name. |
| `invalid_arguments` | no | yes | Args failed the input schema. |
| `approval_required` | no | yes | Waiting on a human; retry after approval. |
| `approval_denied` | no | no | A human declined; nothing ran. |
| `invalid_output` | no | no | Handler violated its output schema; a bug. |
| `timeout` | yes | no | Handler exceeded `timeout_ms`; side effects may be partial. |
| `handler_error` | no | no | Unhandled handler failure (handler may opt in to retryable). |
| `missing_secret` | no | no | A declared secret is not configured. |
| `not_implemented` | no | no | Prototype stub; the handler is not built yet. |

UI mapping: `message` is the plain-language sentence shown behind "See details" (see
`docs/frontend-scope-simple.md`). Do not show codes to the person.

---

## 11. Startup validation (fail fast)

The loader validates **every** manifest, collects all violations, and raises one
`SkillManifestError`. No registry is returned if anything is wrong, so the process cannot serve
a half-valid tool set.

```
SkillManifestError: 2 invalid skill manifest(s) under /app/skills/builtin
  - schedule.create: /permissions/requires_approval: skills declaring the 'write' effect must set requires_approval=true
  - web.search: /permissions/scopes/hosts/0: 'https://api.tavily.com' must be a bare hostname (no scheme, port, or path)
```

- Each violation names the **skill** and the **JSON Pointer** of the offending field.
- `SkillManifestError.violations` is machine-readable; `.to_dict()` returns
  `{error, root, violations[]}` for CI logs.
- Structural (meta-schema) errors short-circuit semantic checks for that manifest; other
  manifests are still checked so one run reports every problem it can.

### 11.1 Rule index

| Rule | Field | Enforced by |
|---|---|---|
| R1 `manifest.json` exists and is valid JSON | `/` | loader |
| R2 size ≤ 256 KiB | `/` | loader |
| R3 required fields present | `/…` | meta-schema |
| R4 no unknown fields | `/…` | meta-schema |
| R5 name is dotted, lowercase, 2–32 chars/segment | `/name` | meta-schema |
| R6 version is SemVer | `/version` | meta-schema |
| R7 1–8 triggers, valid kinds | `/triggers` | meta-schema |
| R8 at least one `model` trigger | `/triggers` | semantic |
| R9 exactly one tool, equal to `name` | `/tools` | meta-schema + semantic |
| R10 schemas are valid JSON Schema | `/input_schema`, `/output_schema` | `check_schema` |
| R11 schemas have `type: object` roots | `/…_schema/type` | meta-schema |
| R12 no remote `$ref` | `/…_schema/$ref` | loader |
| R13 input schema closed | `/input_schema/additionalProperties` | semantic |
| R14 effects non-empty, unique, valid | `/permissions/effects` | meta-schema |
| R15 `execute` rejected | `/permissions/effects` | semantic |
| R16 `network` needs hosts | `/permissions/scopes/hosts` | semantic |
| R17 `write` needs approval + scope | `/permissions/requires_approval`, `/permissions/scopes` | semantic |
| R18 non-idempotent needs approval | `/permissions/requires_approval` | semantic |
| R19 host/path/repo grammar | `/permissions/scopes/…` | semantic |
| R20 secret names are env vars | `/permissions/secrets/…` | meta-schema + semantic |
| R21 timeout in 100–600000 | `/timeout_ms` | meta-schema |
| R22 cost class enum | `/cost_class` | meta-schema |
| R23 entry module imports | `/entry/module` | loader |
| R24 entry callable exists and is async | `/entry/callable` | loader |
| R25 skill names unique | `/name` | loader |

---

## 12. Prototype skills (Q8 cut)

| Skill | Effects | Approval | Scope | Timeout | Idempotent | Cost | Secrets |
|---|---|---|---|---|---|---|---|
| `web.search` | read, network | no | hosts: `api.tavily.com` | 15 s | yes | metered | `TAVILY_API_KEY` |
| `github.read` | read, network | no | hosts: `api.github.com`; repos: `Anshieee/nebius-nvidia` | 20 s | yes | free | `GITHUB_TOKEN` |
| `docs.answer` | read | no | paths: `${WORKSPACE}/var/artifacts/**` | 30 s | yes | metered | — |
| `schedule.create` | write | **yes** | paths: `${WORKSPACE}/var/scheduler/**` | 5 s | **no** | free | — |

### 12.1 `web.search`

Read-only web search. Input `{query, max_results?, freshness?}` → `{query, backend, results[]}`
with `title`, `url`, `snippet`, optional `published_at`. Egress is limited to the allowlisted
search host; the backend is swappable by editing the manifest host and the handler, with no
kernel change. Never mutates remote state.

### 12.2 `github.read`

Read-only GitHub access, discriminated by `kind`:

| `kind` | Required extra | Returns |
|---|---|---|
| `repo` | — | `full_name`, `default_branch`, `private`, `html_url`, description, open-issue count |
| `issue` | `number` | number, title, state, labels, body, URL |
| `pull_request` | `number` | number, title, state, merged, head/base, body, URL |
| `file` | `path` (+ optional `ref`) | path, ref, content, encoding, size |

GET-only. No comments, labels, merges, or pushes. The repo allowlist is per-deployment; widening
it is a manifest change and a policy review, not a code change. `GITHUB_TOKEN` is read through
`context.secret` and never logged.

### 12.3 `docs.answer`

One skill, internal PDF/CSV dispatch (ADR 0001). Input
`{artifact, kind: "pdf"|"csv", question, max_rows?}` where `artifact` is an opaque handle
matching `artifact:<id>`. Output `{answer, artifact:{handle, kind, mime, pages?|rows?},
citations[]}` with page or row locators. Reads only under the artifacts scope; no network.

### 12.4 `schedule.create`

The only write skill in the prototype. Input
`{title, prompt, schedule:{kind:"cron", expr}, timezone, start_at?, end_at?}` → 
`{task_id, status:"scheduled", next_run_at}`. Non-idempotent (a retry creates a second task),
so the manifest forces approval. Writes only under the scheduler scope; the created task feeds
the scheduler surface in P0-2/P0-5.

---

## 13. Adding a skill

1. Create `skills/builtin/<name>/manifest.json` (copy a prototype manifest, change name,
   schemas, permissions, entry).
2. Create `skills/builtin/<name>/handler.py` with `async def run(arguments, context)`.
3. Add a golden fixture and one negative test per new rule you rely on (see §14).
4. Run `python -m pytest`. If the loader rejects the manifest, the error names the field.
5. No kernel, loader, or policy edits. If you need one, the contract is missing something —
   change this spec first.

---

## 14. Tests

- **Golden fixture:** `tests/fixtures/skills/golden/manifest.json` loads and invokes cleanly.
- **One negative test per rule** R1–R25 in `tests/test_skill_loader.py`, asserting the field
  pointer in the startup error.
- **Runtime tests:** success, unknown skill, bad args, unknown arg, non-object args, approval
  required / denied / hook failure / approved execution, timeout, handler exception, invalid
  output, non-object output, secret scrubbing, context redaction.
- **Built-ins:** all four load, network skills are allowlisted, the three read-only skills
  declare no `write`, and stubs return `not_implemented` (including argument validation before
  the stub).
- Every new skill PR MUST include its contract tests. CI wiring is a separate M0 task.

Run:

```bash
python -m pytest
```

---

## 15. Change control

- `manifest_version` is `1.0` for this shape. Incompatible changes bump it and require a
  loader migration path; additive optional fields MAY stay in `1.x`.
- `error.code` values are a closed set (`skills/errors.py::ERROR_CODES`). Adding one requires
  updating the table in §10.2, the frontend mapping, and the tests.
- Changing approval semantics, scope grammar, or the single-tool rule requires an ADR and a
  spec revision — not just a loader edit.

---

## 16. Implementation status

| Piece | Status |
|---|---|
| Meta-schema + fail-fast loader + registry | done (`skills/loader.py`, `skills/manifest.schema.json`) |
| Typed envelopes, timeout, approval hook, secret scrubbing | done |
| Four prototype manifests | done, loader-valid |
| Four handlers | **stubs** raising `not_implemented`; real bodies land with P0-1/P0-3 and the M1 demo |
| Kernel integration (tool list, mid-stream calls) | P0-1 |
| Policy engine, approval UI, audit log | P0-3 |
| Contract tests | done (60 tests, offline) |

# Service control-layer contract (P0-1)

- **Status:** frozen, 2026-11-09
- **Issue:** P0-1 (planning gate; P1-P6 wait on it)
- **Scope:** the shapes that every bot surface, gate, and admin view uses. This
  document freezes interfaces; P0-1 ships no runtime change.
- **Decision record:** [`docs/adr/0003-service-control-layer.md`](adr/0003-service-control-layer.md)
- **Companions:** [`docs/KERNEL.md`](KERNEL.md) (provider loop),
  [`docs/CHAT_SPEC.md`](CHAT_SPEC.md) (chat surface),
  [`docs/PRIVACY.md`](PRIVACY.md) (recipients and retention)
- **Checked by:** [`tests/test_service_contract.py`](../tests/test_service_contract.py)

The rules below are normative. Field order is part of the contract: readers,
fixtures, and serialized admin rows may assume it. The JSON block at the end of
this file is the machine-readable copy; the prose and the block must agree, and
the test fails if they do not.

## 1. Tenant model

There is one tenant today: `tenant_id = "owner"`. It is the owner's tenant, not a
shared workspace. Tenant ids become URL-safe slugs later; the migration is
additive and every existing row defaults to `"owner"` (ADR 0003 §3).

`TenantContext` is resolved before any gate runs. Its field order is
`tenant_id`, `actor_id`, `role`, `surface`.

| Field | Meaning | Today |
|---|---|---|
| `tenant_id` | The tenant the work belongs to | `"owner"` |
| `actor_id` | Who initiated it: `owner`, `system`, `scheduler`, `plugin`, or `share` | `"owner"` for human actions |
| `role` | The principal's role | `"owner"` |
| `surface` | Where the work came from: `web`, `desktop`, `mobile`, `telegram`, `cli`, `scheduler` | one of these |

`TenantContext` is also the principal for the control layer's purposes: P0 has no
separate account table, and `actor_id` plus `role` carry the identity that ADR
0002 defines. A second tenant is not admitted in P0 and needs its own review.

## 2. Provider contract

The provider seam is exactly one streaming method:

```text
stream(ChatRequest) -> AsyncIterator[ProviderEvent]
```

`ChatRequest` is the kernel's frozen request dataclass. `ProviderEvent` is the
closed union of `TextDelta`, `ToolCallReady`, `UsageReport`, `ProviderFailure`,
and `StreamEnd`. Failures are typed events; a provider call does not raise into
the caller.

### 2.1 ChatRequest

Field order is `model`, `messages`, `tools`, `temperature`, `max_tokens`,
`tenant_id`.

| Field | Type | Default | Status |
|---|---|---|---|
| `model` | string | — (required) | present today |
| `messages` | array | — (required) | present today |
| `tools` | array | `[]` | present today |
| `temperature` | number | `0.2` | present today |
| `max_tokens` | integer or null | `null` | present today |
| `tenant_id` | string | `"owner"` | added in P1-1 |

P1-1 adds `tenant_id` with the default `"owner"` and does not reorder the five
existing fields. A provider instance is bound to one tenant; a request whose
`tenant_id` differs yields the typed `tenant_mismatch` failure before any call,
and the field is never serialized into the upstream request body.

### 2.2 ProviderSpec

Field order is `name`, `base_url`, `api_key_env`, `default_model`, `pricing`.

| Field | Type | Meaning |
|---|---|---|
| `name` | string | Stable provider id used in records and admin rows |
| `base_url` | string (URI) | OpenAI-compatible API root; no default host |
| `api_key_env` | string | Name of the environment variable holding the key |
| `default_model` | string | Model used when a request does not override it |
| `pricing` | object | `input_per_million_usd`, `output_per_million_usd` |

`api_key_env` is a name, never a value. The modern Python API key, headers, and
provider response bodies never enter a run record, a log line, or the admin read
model.

### 2.3 The seam rule

The contract is name-agnostic: **one module-level patchable provider symbol plus
an injectable `provider=` keyword**. On this branch the symbol is
`chat.server.ProviderClient`, imported at module level and passed as
`build_app(..., provider=...)`; on another line the same seam may be called
`TokenFactoryClient`. The test accepts either name. What is frozen is the
structure, not the spelling: replacing the module-level symbol must affect the
runtime, and `provider=` must let a caller inject a replacement without touching
the rest of the app.

## 3. Run record

`TaskRun` is the operational record of one unit of work. It is durable state
about execution, not a copy of the conversation.

Field order:
`run_id`, `tenant_id`, `thread_id`, `turn_id`, `surface`, `task_type`, `status`,
`created_at`, `updated_at`, `started_at`, `finished_at`, `model`, `provider`,
`input_tokens`, `output_tokens`, `total_tokens`, `cost_usd`, `steps`,
`tool_calls`, `approvals`, `error_code`, `retry_of`, `idempotency_key`.

| Field | Meaning |
|---|---|
| `run_id` | External id, format `run_…` |
| `tenant_id` | Tenant that owns the run |
| `thread_id` | Conversation thread, when the run belongs to one |
| `turn_id` | Internal id, format `turn_…`; the chat store's unit |
| `surface` | Originating surface, same vocabulary as `TenantContext.surface` |
| `task_type` | Controlled task label (`chat`, `scheduled`, `plugin`, …) |
| `status` | One of the statuses below |
| `created_at`, `updated_at` | ISO-8601 UTC timestamps |
| `started_at`, `finished_at` | Nullable ISO-8601 UTC timestamps |
| `model`, `provider` | Selected model and provider name |
| `input_tokens`, `output_tokens`, `total_tokens` | Token totals reported for the run |
| `cost_usd` | Estimated cost, a decimal number of US dollars |
| `steps` | Count of model/tool steps |
| `tool_calls` | Count of tool calls |
| `approvals` | Count of approval decisions |
| `error_code` | One value from §5, or null |
| `retry_of` | `run_id` this run retries, or null |
| `idempotency_key` | Caller-supplied key that deduplicates submissions |

### 3.1 Statuses

Non-terminal: `queued`, `running`, `awaiting_approval`, `stopping`.

Terminal: `completed`, `failed`, `cancelled`, `interrupted`.

Every `run_id` has **exactly one terminal record**. A terminal status is written
once and never overwritten; a late writer loses. `stopping` is not terminal: it
asks the run to stop, and the run then writes one of `cancelled` or
`interrupted` (or completes if it finished first). This rule is what makes a
retry and a crash-recovery pass safe to run concurrently.

## 4. Entitlement decision

`EntitlementDecision` field order is `decision`, `code`, `limit`, `window`,
`plan_id`.

| Field | Type | Meaning |
|---|---|---|
| `decision` | `allow` or `deny` | The gate's answer |
| `code` | string | Stable machine code from §5 |
| `limit` | integer or null | The limit that applied |
| `window` | `minute`, `hour`, `day`, `month`, or null | The window the limit applies to |
| `plan_id` | string or null | Plan that produced the decision |

A decision is total: every request gets an `allow` or a `deny`, and a denial
carries a code. A missing decision is not treated as `allow`.

## 5. Gate order and error codes

Every request passes these gates in order:

```text
auth → tenant → entitlement → spend → provider
```

1. **auth** — authenticate the caller and validate CSRF where required.
2. **tenant** — resolve the tenant and reject a mismatch.
3. **entitlement** — allow or deny the task type under the plan.
4. **spend** — check the spend window.
5. **provider** — call the provider only after all four pass.

Each gate fails closed: a denial stops the request before the next gate and
before any provider call. The order is frozen; P1 wires the gates into this
order rather than choosing a new one per surface.

Frozen error codes:

| Code | Produced by |
|---|---|
| `invalid_request` | Request validation, before or inside `auth` |
| `unauthenticated` | `auth` |
| `forbidden` | `auth` |
| `tenant_missing` | `tenant` |
| `tenant_unknown` | `tenant` |
| `tenant_mismatch` | `tenant` |
| `entitlement_denied` | `entitlement` |
| `entitlement_exhausted` | `entitlement` |
| `spend_cap_exceeded` | `spend` |
| `missing_base_url` | `provider` configuration, before any call |
| `missing_api_key` | `provider` configuration, before any call |
| `provider_auth` | `provider` |
| `provider_model_not_found` | `provider` |
| `provider_timeout` | `provider` |
| `provider_rate_limited` | `provider` |
| `provider_unavailable` | `provider` |
| `provider_rejected` | `provider` |
| `provider_unreachable` | `provider` |
| `provider_protocol_error` | `provider` |
| `provider_error` | `provider` |
| `internal_error` | Any gate, for an unexpected failure |

The provider codes match `kernel.provider.classify_status`; `internal_error` is a
fixed value so an unexpected exception never leaks a message or a secret into a
record. `missing_base_url` and `missing_api_key` are the exception: they describe
configuration, are produced before any network call, and therefore have no HTTP
status to classify. Either one means the request never reached a provider.

## 6. Admin read model

The admin view may expose the operational fields below:

`run_id`, `tenant_id`, `thread_id`, `turn_id`, `surface`, `task_type`, `status`,
`created_at`, `updated_at`, `started_at`, `finished_at`, `model`, `provider`,
`input_tokens`, `output_tokens`, `total_tokens`, `cost_usd`, `steps`,
`tool_calls`, `approvals`, `error_code`, `retry_of`, `idempotency_key`.

`turn_id` is internal and optional. A run with no chat turn — scheduled,
plugin, or system work — omits it rather than inventing a placeholder, and an
admin row for that run omits it too. `run_id` remains the external identifier.

It must not expose:

- `prompt_text`, `message_text`, `tool_arguments`, `tool_results`, or
  `provider_response_body` — the conversation and provider payload stay out of
  operational views;
- `api_key`, `secret_values`, `headers`, or `cookies` — configuration names may
  appear, values may not;
- `cross_tenant_data` — every admin query is scoped by `tenant_id`, so rows from
  another tenant cannot appear even when the caller is an operator.

These exclusions are not a redaction step performed after the fact. The read
model is built from fields that cannot hold content, the same rule the egress
ledger follows.

## 7. Compatibility and change control

- **P0 is documentation plus one test.** No runtime module changed in P0. P1-1
  implements the P1 targets the contract declared: `kernel.provider.ChatRequest`
  has six fields with `tenant_id` last, the provider implementation lives in
  `kernel.providers`, `kernel.provider` is a thin compatibility shim, the
  provider seam is one patchable module-level symbol plus `provider=`, and
  `TokenFactoryClient` is an alias rather than a required second name.
- **Additive changes only inside a major version.** Adding an optional field at
  the end of `TaskRun` is a minor version; reordering a field list, deleting a
  status, changing a gate's position, or renaming an error code is a new contract
  version with a migration note.
- **The JSON schema is the source of truth for tests.** Prose tables explain it;
  the block between the markers is parsed.
- **A plan change is not a contract change.** Adding a plan, limit, or window
  value does not reorder the gates or change `EntitlementDecision`.

## 8. Machine-readable schema

<!-- contract-schema:start -->
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://kelp.example/contracts/service-control-layer.schema.json",
  "title": "Kelp bot control-layer service contract",
  "contract_version": "1.0.0",
  "status": "frozen",
  "frozen_on": "2026-11-09",
  "tenant_model": {
    "mode": "single_owner",
    "owner_tenant_id": "owner",
    "principal_roles": ["owner"],
    "future": {
      "tenant_id_format": "slug",
      "status": "P1+",
      "migration": "additive; existing rows default to owner",
      "isolation_shipped": false
    }
  },
  "provider": {
    "request_type": "ChatRequest",
    "event_type": "ProviderEvent",
    "stream_signature": "stream(ChatRequest) -> AsyncIterator[ProviderEvent]",
    "seam": {
      "module": "chat.server",
      "rule": "one module-level patchable provider symbol plus an injectable provider= keyword",
      "accepted_symbols": ["ProviderClient", "TokenFactoryClient"],
      "injectable_keyword": "provider",
      "module_level_only": true
    },
    "provider_spec": {
      "field_order": ["name", "base_url", "api_key_env", "default_model", "pricing"],
      "required": ["name", "base_url", "api_key_env", "default_model", "pricing"],
      "properties": {
        "name": {"type": "string"},
        "base_url": {"type": "string", "format": "uri"},
        "api_key_env": {"type": "string"},
        "default_model": {"type": "string"},
        "pricing": {
          "type": "object",
          "field_order": ["input_per_million_usd", "output_per_million_usd"],
          "required": ["input_per_million_usd", "output_per_million_usd"],
          "properties": {
            "input_per_million_usd": {"type": "number", "minimum": 0},
            "output_per_million_usd": {"type": "number", "minimum": 0}
          }
        }
      },
      "secret_rule": "api_key_env names an environment variable; its value never appears in the spec, logs, events, records, or the admin read model"
    }
  },
  "ChatRequest": {
    "type": "object",
    "status": "six fields; tenant_id added in P1-1 and always last",
    "field_order": ["model", "messages", "tools", "temperature", "max_tokens", "tenant_id"],
    "p0_present_field_order": ["model", "messages", "tools", "temperature", "max_tokens"],
    "p1_additions": ["tenant_id"],
    "required": ["model", "messages"],
    "properties": {
      "model": {"type": "string"},
      "messages": {"type": "array"},
      "tools": {"type": "array", "default": []},
      "temperature": {"type": "number", "default": 0.2},
      "max_tokens": {"type": ["integer", "null"], "default": null},
      "tenant_id": {"type": "string", "default": "owner", "since": "P1"}
    }
  },
  "TenantContext": {
    "type": "object",
    "field_order": ["tenant_id", "actor_id", "role", "surface"],
    "required": ["tenant_id", "actor_id", "role", "surface"],
    "properties": {
      "tenant_id": {"type": "string", "default": "owner"},
      "actor_id": {"type": "string", "description": "owner for human actions; system, scheduler, plugin, or share for non-human actors"},
      "role": {"type": "string", "enum": ["owner"]},
      "surface": {"type": "string", "enum": ["web", "desktop", "mobile", "telegram", "cli", "scheduler"]}
    }
  },
  "TaskRun": {
    "type": "object",
    "field_order": ["run_id", "tenant_id", "thread_id", "turn_id", "surface", "task_type", "status", "created_at", "updated_at", "started_at", "finished_at", "model", "provider", "input_tokens", "output_tokens", "total_tokens", "cost_usd", "steps", "tool_calls", "approvals", "error_code", "retry_of", "idempotency_key"],
    "required": ["run_id", "tenant_id", "status", "created_at", "updated_at"],
    "run_id": {"format": "run_<opaque>", "visibility": "external"},
    "turn_id": {"format": "turn_<opaque>", "visibility": "internal"},
    "statuses": ["queued", "running", "awaiting_approval", "stopping", "completed", "failed", "cancelled", "interrupted"],
    "terminal_statuses": ["completed", "failed", "cancelled", "interrupted"],
    "terminal_rule": "exactly one terminal record per run_id; a terminal status is written once and never overwritten",
    "properties": {
      "run_id": {"type": "string", "pattern": "^run_"},
      "tenant_id": {"type": "string", "default": "owner"},
      "thread_id": {"type": ["string", "null"]},
      "turn_id": {"type": "string", "pattern": "^turn_"},
      "surface": {"type": "string", "enum": ["web", "desktop", "mobile", "telegram", "cli", "scheduler"]},
      "task_type": {"type": "string"},
      "status": {"type": "string", "enum": ["queued", "running", "awaiting_approval", "stopping", "completed", "failed", "cancelled", "interrupted"]},
      "created_at": {"type": "string", "format": "date-time"},
      "updated_at": {"type": "string", "format": "date-time"},
      "started_at": {"type": ["string", "null"], "format": "date-time"},
      "finished_at": {"type": ["string", "null"], "format": "date-time"},
      "model": {"type": "string"},
      "provider": {"type": "string"},
      "input_tokens": {"type": "integer", "minimum": 0},
      "output_tokens": {"type": "integer", "minimum": 0},
      "total_tokens": {"type": "integer", "minimum": 0},
      "cost_usd": {"type": "number", "minimum": 0},
      "steps": {"type": "integer", "minimum": 0},
      "tool_calls": {"type": "integer", "minimum": 0},
      "approvals": {"type": "integer", "minimum": 0},
      "error_code": {"type": ["string", "null"]},
      "retry_of": {"type": ["string", "null"]},
      "idempotency_key": {"type": ["string", "null"]}
    }
  },
  "EntitlementDecision": {
    "type": "object",
    "field_order": ["decision", "code", "limit", "window", "plan_id"],
    "required": ["decision", "code"],
    "properties": {
      "decision": {"type": "string", "enum": ["allow", "deny"]},
      "code": {"type": "string"},
      "limit": {"type": ["integer", "null"]},
      "window": {"type": ["string", "null"], "enum": ["minute", "hour", "day", "month", null]},
      "plan_id": {"type": ["string", "null"]}
    }
  },
  "gate_order": ["auth", "tenant", "entitlement", "spend", "provider"],
  "gate_rule": "each gate runs in order and fails closed; a denial at any gate stops the request before the next gate and before any provider call",
  "error_codes": [
    "invalid_request",
    "unauthenticated",
    "forbidden",
    "tenant_missing",
    "tenant_unknown",
    "tenant_mismatch",
    "entitlement_denied",
    "entitlement_exhausted",
    "spend_cap_exceeded",
    "missing_base_url",
    "missing_api_key",
    "provider_auth",
    "provider_model_not_found",
    "provider_timeout",
    "provider_rate_limited",
    "provider_unavailable",
    "provider_rejected",
    "provider_unreachable",
    "provider_protocol_error",
    "provider_error",
    "internal_error"
  ],
  "admin_read_model": {
    "fields": ["run_id", "tenant_id", "thread_id", "turn_id", "surface", "task_type", "status", "created_at", "updated_at", "started_at", "finished_at", "model", "provider", "input_tokens", "output_tokens", "total_tokens", "cost_usd", "steps", "tool_calls", "approvals", "error_code", "retry_of", "idempotency_key"],
    "excluded": ["prompt_text", "message_text", "tool_arguments", "tool_results", "provider_response_body", "api_key", "secret_values", "headers", "cookies", "cross_tenant_data"],
    "rules": [
      "no prompt, message, tool-argument, or provider-response text",
      "no API-key, credential, header, or cookie values",
      "no rows outside the caller's tenant; every admin query is tenant-scoped"
    ]
  }
}
<!-- contract-schema:end -->

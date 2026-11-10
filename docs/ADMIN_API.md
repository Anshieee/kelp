# Admin API (P6)

- **Status:** shipped 2026-11-09
- **Scope:** read-only operational visibility for the single-owner control layer.
- **Contract:** [`docs/SERVICE_CONTRACT.md`](SERVICE_CONTRACT.md) §6 freezes the
  admin read model. This document describes the HTTP projection of that model;
  it does not extend it.
- **Implementation:** [`chat/admin.py`](../chat/admin.py), mounted by
  [`chat/api.py`](../chat/api.py) under `/api/admin`.
- **Tests:** [`tests/test_admin_api.py`](../tests/test_admin_api.py) and
  [`tests/test_support_runbook.py`](../tests/test_support_runbook.py).

## Design rules

1. **Owner-only.** Every route runs behind the same owner bearer token or
   session cookie as the rest of `/api/*`. There is no unauthenticated admin
   route, no share-token admin route, and no second credential type.
2. **Tenant-scoped.** The authenticated credential resolves one tenant. Every
   run, usage aggregation, spend total, and audit row is filtered by that
   tenant. A tenant detail request for any other id is the same `404` as a
   missing id, so the route cannot be used to discover another tenant.
3. **Read-only.** Admin calls never write a row, append an audit entry, change a
   plan, or reach a provider. There is no admin mutation surface in this phase.
4. **Content-free.** Responses are built from the frozen §6 operational fields
   and an audit allow-list. Prompt text, message text, tool arguments, tool
   results, provider bodies, API-key/secret values, headers, and cookies have no
   field in any response.
5. **Bounded.** The run index and run counts cover the store's newest-first
   500-row window (`window: 500`). They are not lifetime counters. The audit
   index pages through the persisted audit log with `limit` and `offset`.
6. **Observable.** Every response carries `X-Request-Id`; the JSON body repeats
   the same value as `request_id`. Admin reads are charged to the existing
   `admin` rate-limiter class (the same generous bucket used by `GET
   /api/metrics` and `GET /api/readyz`).

## Authentication and transport

```bash
export KELP_BASE_URL="http://127.0.0.1:8765"
export KELP_AUTH_TOKEN="<owner-token>"   # never commit or paste the real value

curl -sS \
  -H "Authorization: Bearer $KELP_AUTH_TOKEN" \
  "$KELP_BASE_URL/api/admin/summary" | python -m json.tool
```

A session cookie works the same way for `GET` requests:

```bash
curl -sS --cookie "$KELP_COOKIE_JAR" "$KELP_BASE_URL/api/admin/summary"
```

Admin routes are `GET`-only. A non-`GET` method on a known admin path returns a
typed `405` with `Allow: GET`.

## Common response envelope

Every successful response is JSON with:

```json
{
  "ok": true,
  "request_id": "req_example",
  "...": "resource-specific keys"
}
```

`request_id` is the same opaque value returned in `X-Request-Id`. Errors use the
ordinary service envelope:

```json
{
  "ok": false,
  "error": {
    "code": "invalid_request",
    "message": "limit must be an integer.",
    "retryable": false,
    "user_fixable": true
  }
}
```

## Endpoint reference

### `GET /api/admin/summary`

One tenant's operational summary: current plan id, run counts, usage totals, and
day/month spend. No raw rows.

```json
{
  "ok": true,
  "request_id": "req_example",
  "tenant_id": "owner",
  "summary": {
    "tenant_id": "owner",
    "plan_id": "owner-unlimited",
    "runs": {
      "total": 12,
      "window": 500,
      "by_status": {
        "queued": 0,
        "running": 1,
        "awaiting_approval": 0,
        "stopping": 0,
        "completed": 10,
        "failed": 1,
        "cancelled": 0,
        "interrupted": 0
      },
      "by_task_type": {"chat": 11, "scheduled": 1},
      "by_surface": {"web": 9, "scheduler": 1, "mobile": 2}
    },
    "usage": {
      "records": 12,
      "input_tokens": 1200,
      "output_tokens": 400,
      "total_tokens": 1600,
      "known_cost_usd": 0.42,
      "unknown_price_records": 0
    },
    "spend": {
      "day": {
        "used_usd": 0.05,
        "limit_usd": 5.0,
        "records": 2,
        "unknown_price_records": 0,
        "ratio": 0.01
      },
      "month": {
        "used_usd": 0.42,
        "limit_usd": 20.0,
        "records": 12,
        "unknown_price_records": 0,
        "ratio": 0.021
      }
    }
  }
}
```

`limit_usd` is `null` when no spend cap is configured for that window; `ratio` is
omitted in that case.

### `GET /api/admin/tenants`

The tenant index. **It contains only the caller's tenant.** There is no
cross-tenant listing: the contract forbids rows outside the caller's tenant even
when the caller is an operator.

```json
{
  "ok": true,
  "request_id": "req_example",
  "tenants": [
    {
      "tenant_id": "owner",
      "plan_id": "owner-unlimited",
      "runs": {"total": 12, "window": 500, "by_status": {"completed": 10}},
      "usage": {"records": 12, "total_tokens": 1600},
      "spend": {"day": {"used_usd": 0.05}, "month": {"used_usd": 0.42}}
    }
  ]
}
```

### `GET /api/admin/tenants/{tenant_id}`

Tenant detail for the caller's tenant.

- `tenant_id` must equal the tenant the credential resolved. Any other value —
  well-formed or not — returns the same `404` as an unknown tenant.
- `plan` is the **current** configured plan as a read-only snapshot. Plan
  history is not reconstructed: `TaskRun` has no plan column, and snapshots are
  kept recorder-side by [`chat/runs.py`](../chat/runs.py).
- `keys` reports **presence only** (`true`/`false`) by configuration name. A
  secret value never appears.

```json
{
  "ok": true,
  "request_id": "req_example",
  "tenant": {
    "tenant_id": "owner",
    "plan": {
      "plan_id": "pilot",
      "window": "month",
      "max_turns": 100,
      "max_runs": 50,
      "max_cost_usd": null,
      "allowed_task_types": ["chat"]
    },
    "runs": {"total": 12, "window": 500, "by_status": {"completed": 10}},
    "usage": {
      "records": 12,
      "input_tokens": 1200,
      "output_tokens": 400,
      "total_tokens": 1600,
      "known_cost_usd": 0.42,
      "unknown_price_records": 0
    },
    "spend": {
      "day": {"used_usd": 0.05, "limit_usd": 5.0, "records": 2},
      "month": {"used_usd": 0.42, "limit_usd": 20.0, "records": 12}
    },
    "keys": {
      "provider_api_key": true,
      "FAKE_PROVIDER_TOKEN": true
    }
  }
}
```

### `GET /api/admin/runs`

One page of the caller's runs, newest first. The row is exactly the frozen §6
admin projection produced by `TaskRun.to_admin_row()`: `turn_id` is omitted when
the run has no chat turn (scheduled, plugin, or system work).

Query parameters:

| Name | Type | Default | Meaning |
|---|---|---|---|
| `limit` | integer 1–200 | `50` | Rows in this page |
| `offset` | integer 0–500 | `0` | Offset inside the newest-first window |
| `thread_id` (or `thread`) | string | — | Restrict to one thread |
| `status` | frozen status | — | Restrict to one status |

```json
{
  "ok": true,
  "request_id": "req_example",
  "tenant_id": "owner",
  "runs": [
    {
      "run_id": "run_example",
      "tenant_id": "owner",
      "thread_id": "ses_example",
      "turn_id": "turn_example",
      "surface": "web",
      "task_type": "chat",
      "status": "completed",
      "created_at": "2026-11-09T10:00:00Z",
      "updated_at": "2026-11-09T10:00:01Z",
      "started_at": "2026-11-09T10:00:00Z",
      "finished_at": "2026-11-09T10:00:01Z",
      "model": "nvidia/nemotron-3.5-super-vl",
      "provider": "example-provider",
      "input_tokens": 10,
      "output_tokens": 5,
      "total_tokens": 15,
      "cost_usd": 0.0001,
      "steps": 1,
      "tool_calls": 0,
      "approvals": 0,
      "error_code": null,
      "retry_of": null,
      "idempotency_key": "client-key-example"
    }
  ],
  "next_offset": null,
  "window": 500
}
```

`next_offset` is `null` on the last page; otherwise pass it as `offset` for the
next page. `window` names the store ceiling, not a promise of lifetime coverage.

### `GET /api/admin/runs/{run_id}`

One run in the caller's tenant. A run that belongs to another tenant returns the
same `404` body as a missing id; existence is not an oracle.

```json
{
  "ok": true,
  "request_id": "req_example",
  "run": {"run_id": "run_example", "status": "completed", "...": "..."}
}
```

### `GET /api/admin/audit`

A paged index of audit metadata for the caller's tenant, newest first. Only an
allow-list is projected:

`ts`, `event`, `tenant_id`, `turn_id`, `request_id`, `tool`, `actor`,
`decision`, `outcome`, `exit_code`, `duration_ms`.

`command`, `argument_hash`, and `extra` are **never** returned: those are the
audit fields most likely to carry tool input. Audit rows without a `tenant_id`
are excluded rather than attributed to the caller.

Query parameters:

| Name | Type | Default | Meaning |
|---|---|---|---|
| `limit` | integer 1–200 | `50` | Rows in this page |
| `offset` | integer 0–500 | `0` | Offset in the newest-first index |
| `event` | string | — | Exact event name filter |

```json
{
  "ok": true,
  "request_id": "req_example",
  "tenant_id": "owner",
  "audit": [
    {
      "ts": "2026-11-09T10:00:01.000000+00:00",
      "event": "tool_call",
      "tenant_id": "owner",
      "turn_id": "turn_example",
      "request_id": "req_example",
      "tool": "web.search",
      "decision": "allow",
      "outcome": "ok",
      "duration_ms": 42
    }
  ],
  "next_offset": null,
  "window": 500
}
```

## Errors

| Status | Code | Cause |
|---|---|---|
| `400` | `invalid_request` | `limit`/`offset` not an integer, or an unknown `status` |
| `401` | `unauthenticated` | Missing or invalid owner credential |
| `403` | `forbidden` / `csrf_failed` | Owner auth failure on a mutation attempt |
| `404` | `not_found` | Unknown admin path, unknown run, or another tenant's run/tenant id |
| `405` | `method_not_allowed` | A non-`GET` method on a known admin path |
| `429` | `rate_limited` | The `admin` limiter class refused the request |

## Known P5 interactions

These are surfaced here so an operator does not misread the numbers; none is
changed by P6.

- **Entitlement counter increments twice per accepted `POST /api/runs`.** The
  submission path checks the entitlement gate once at the HTTP boundary and the
  service checks it again inside `send`. Both use the same injected counter. The
  admin usage/spend views read persisted usage records, so they do not expose a
  synthetic "checks" count.
- **`max_turns` and `max_runs` share one injected usage counter.** A plan that
  sets both to the same value is not counting two independent series.
- **Plan snapshots are recorder-side.** The frozen `TaskRun` field order has no
  plan column. `GET /api/admin/tenants/{tenant_id}` reports the current plan
  configuration; it does not invent per-run plan history.
- **Usage records default to the owner tenant.** A usage row is filtered by its
  `tenant_id`; a row that cannot name a tenant is excluded from every admin
  aggregate.

## Non-goals

P6 does not add tenant CRUD, billing or invoice views, plan editing, retention
changes, entitlement rewiring, or any write surface. Those belong to later
phases and their own review. The frozen contract and
[`tests/test_service_contract.py`](../tests/test_service_contract.py) are
unchanged.

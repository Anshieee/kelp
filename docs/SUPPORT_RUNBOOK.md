# Support runbook (P6)

- **Status:** shipped 2026-11-09
- **Audience:** the owner-operator of a single KELP process. No second-tier
  vendor, no managed-service console, and no infrastructure provisioning is
  assumed.
- **Scope:** triage, evidence collection, secret rotation, tenant
  suspension/offboarding, spend and entitlement issues, rollback, and pilot
  handoff for the control layer shipped in P0–P6.
- **Companions:** [`docs/ADMIN_API.md`](ADMIN_API.md),
  [`docs/SERVICE_CONTRACT.md`](SERVICE_CONTRACT.md),
  [`docs/DEPLOY.md`](DEPLOY.md), [`docs/PRIVACY.md`](PRIVACY.md).

> **Non-goals.** This runbook does not provision databases, hosts, networks,
> certificates, or provider accounts, and it does not contain vendor-specific
> console steps. It operates an already-deployed process with the commands in
> this repository. Anything outside that is an infrastructure change and needs
> its own reviewed procedure.

## State locations

All paths are relative to the deployment working directory unless the
environment variable says otherwise. Values in `$KELP_*` variables are read at
process start; never echo their contents.

| State | Default path | Environment variable | Notes |
|---|---|---|---|
| Chat database | `var/chat.db` | `KELP_DB_PATH` | threads, turns, runs, sessions, shares, approvals, artifacts metadata |
| SQLite lock | `var/chat.db.lock` | derived from `KELP_DB_PATH` | held by the one live process |
| Audit log | `var/audit/audit.jsonl` (plus rotated segments) | `KELP_AUDIT_DIR` | append-only, scrubbed |
| Usage log | `var/usage/usage.jsonl` (plus rotated segments) | `KELP_USAGE_DIR` | one record per finished turn |
| Spend state | `var/usage/spend-state.json` | `KELP_USAGE_DIR` | 80% warning bookkeeping |
| Artifacts | `var/workspace/var/artifacts` | `KELP_ARTIFACT_DIR` | uploaded bytes and metadata |
| Workspace | `var/workspace` | `KELP_WORKSPACE_ROOT` | scheduler and workspace files |
| Scheduler tasks | `var/workspace/var/scheduler` | `KELP_WORKSPACE_ROOT` | one `task_<id>.json` per schedule |
| Credential vault | `var/credentials.db` | next to `KELP_DB_PATH` | encrypted handoff requests |
| Wallet ledger | `var/wallet/wallet.db` | next to `KELP_DB_PATH` | payment cards and proposals |
| Provider key | process environment or tenant secrets file | `KELP_PROVIDER_API_KEY`, `KELP_TENANT_SECRETS_FILE` | the value is never printed |

The admin surface, described in [`docs/ADMIN_API.md`](ADMIN_API.md), is the
preferred way to read operational state without opening these files:

| Endpoint | Use |
|---|---|
| `GET /api/admin/summary` | one-tenant plan id, run counts, usage, spend |
| `GET /api/admin/tenants` | tenant index (caller's tenant only) |
| `GET /api/admin/tenants/{tenant_id}` | plan, usage, spend, run counts, key presence |
| `GET /api/admin/runs` | paged run index, newest first |
| `GET /api/admin/runs/{run_id}` | one run |
| `GET /api/admin/audit` | paged audit metadata, newest first |

Set a shell prefix once per support session. It never prints the token:

```bash
export KELP_BASE_URL="http://127.0.0.1:8765"
export KELP_AUTH_TOKEN="$(cat "$HOME/.config/kelp/owner-token")"
export KELP_DB_PATH="var/chat.db"
export KELP_AUDIT_DIR="var/audit"
export KELP_USAGE_DIR="var/usage"
```

## Triage tree

Work top-down. Stop at the first branch whose symptom matches. Every branch names
one safe first command; use the section it points to for the full procedure.

### Branch 1 — The process will not start or exits immediately

**First command** — validate configuration without creating files or binding a
port:

```bash
python -m chat.server --check
```

Interpretation:

- `missing_api_key` / `missing_base_url`: the process has no provider
  configuration. Fix the environment or the tenant secrets file, then rerun the
  same command. Do not print the key.
- `missing_auth_token`: `KELP_AUTH_TOKEN` is unset. Set a non-empty value, rerun.
- `database_locked`: another live process owns `var/chat.db.lock`. Find it with
  `pgrep -af "chat.server"`; stop the stale process before starting a second.
- `invalid_secret_file`: the tenant secrets file is missing, malformed, or not
  mode `0600`. See “Secret rotation” below.
- Any other `startup_failed`: run `python -m pytest -q` before changing anything;
  a failing suite is the first evidence.

### Branch 2 — Every `/api/*` call returns `401` or `403`

**First command** — prove the owner credential and request path:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $KELP_AUTH_TOKEN" \
  "$KELP_BASE_URL/api/admin/summary"
```

Interpretation:

- `401`: the token is missing, stale, or the process restarted with a different
  token. Confirm the process environment and the file you sourced.
- `403` with `csrf_failed`: a mutation was sent without a matching
  `x-csrf-token` or same-origin `Origin`. Read-only admin calls should never hit
  this; if one does, check the reverse proxy for header rewriting.
- `200`: auth is healthy; continue to the branch for the actual symptom.
- A lockout after repeated failures clears after
  `KELP_AUTH_LOCKOUT_SECONDS` (default `300`).

### Branch 3 — Admin calls return `429`

**First command** — inspect the limiter class and retry hint:

```bash
curl -sS -D - \
  -H "Authorization: Bearer $KELP_AUTH_TOKEN" \
  "$KELP_BASE_URL/api/admin/summary" | sed -n '1,20p'
```

The admin surface uses the `admin` class (`KELP_RATE_LIMIT_ADMIN_RPM`,
`KELP_RATE_LIMIT_ADMIN_BURST`). A `429` is expected only under a misconfigured
budget or a scrape loop; the default is 1200/minute with a 300 burst. Wait for
`Retry-After`, or fix the scraper. Do not disable the limiter permanently.

### Branch 4 — Spend looks wrong or a turn is refused with `spend_cap_exceeded`

**First command** — read the tenant summary and the persisted windows:

```bash
curl -sS -H "Authorization: Bearer $KELP_AUTH_TOKEN" \
  "$KELP_BASE_URL/api/admin/tenants/owner" | python -m json.tool
```

Check:

1. `spend.day.used_usd` and `spend.month.used_usd` against the configured
   `KELP_SPEND_CAP_DAY_USD` / `KELP_SPEND_CAP_MONTH_USD`.
2. `unknown_price_records`: a nonzero count means at least one model had no
   price; the guardrail either blocks or charges a fixed configured amount
   (`KELP_SPEND_UNKNOWN_PRICE_MODE`).
3. `usage.records` against the usage log line count:

```bash
wc -l "$KELP_USAGE_DIR"/usage.jsonl \
  "$KELP_USAGE_DIR"/usage.jsonl.* 2>/dev/null | tail -n 5
```

If the totals disagree with the file, capture an evidence bundle (below) and
compare the newest rotated segment timestamps. Do not delete usage records to
“reset” spend; that is a policy change and hides the real total.

### Branch 5 — A task type is denied or `entitlement_exhausted`

**First command** — read the current plan and the run counts:

```bash
curl -sS -H "Authorization: Bearer $KELP_AUTH_TOKEN" \
  "$KELP_BASE_URL/api/admin/tenants/owner" | python -m json.tool
```

Compare `plan.allowed_task_types`, `plan.max_turns`, `plan.max_runs`, and
`plan.window` with `runs.total` / `usage.records`. Two known P5 facts:

- The entitlement counter increments **twice** per accepted `POST /api/runs`
  (boundary check plus service check). A limit of 10 can therefore stop work
  after roughly 5 submissions. This is documented, not rewired in P6.
- `max_turns` and `max_runs` share the same injected counter; they are not
  independent budgets.

A plan change is configuration, not a contract change: update
`KELP_ENTITLEMENT_PLANS_JSON` / `KELP_ENTITLEMENT_TENANT_PLANS_JSON`, validate,
and restart. Do not edit a `TaskRun` row to “free” capacity.

### Branch 6 — A run is stuck in `running`, `queued`, or `awaiting_approval`

**First command** — list the non-terminal runs for the tenant:

```bash
curl -sS -H "Authorization: Bearer $KELP_AUTH_TOKEN" \
  "$KELP_BASE_URL/api/admin/runs?status=running" | python -m json.tool
```

Then inspect the persisted row and the turn state:

```bash
sqlite3 "$KELP_DB_PATH" \
  "SELECT run_id, status, turn_id, created_at, updated_at FROM runs WHERE tenant_id='owner' AND status NOT IN ('completed','failed','cancelled','interrupted') ORDER BY created_at;"
```

- `awaiting_approval`: a human decision is pending. Use the approvals route;
  do not force the run terminal by SQL.
- `running` with no live task after a restart: boot recovery should have closed
  it as `interrupted`. If it did not, capture the evidence bundle and restart
  once. A second restart must not change a terminal row.
- Never set a terminal status by hand. The `runs_turn` uniqueness and the
  terminal-once guard are the correctness boundary.

### Branch 7 — The provider is failing (`provider_*` errors)

**First command** — check readiness and the provider probe:

```bash
curl -sS -H "Authorization: Bearer $KELP_AUTH_TOKEN" \
  "$KELP_BASE_URL/api/readyz" | python -m json.tool
```

`checks.provider` is cached for 30 seconds. A `provider_auth`/`missing_api_key`
failure points at rotation (below); `provider_unreachable`/`provider_timeout`
points at network or base URL configuration; `provider_rate_limited` points at
the upstream. The readiness body carries stable codes only, never a response
body or a key.

### Branch 8 — Audit or usage files grow, rotate, or look torn

**First command** — list the active file and its rotated segments with sizes:

```bash
ls -l "$KELP_AUDIT_DIR"/audit.jsonl* "$KELP_USAGE_DIR"/usage.jsonl* 2>/dev/null
```

The retention pass renames the active file to a timestamped segment and expires
whole segments. A torn final line is ignored by the usage reader and counted as
unknown-price spend; the audit reader skips a segment it cannot read. Do not
truncate or rewrite an audit file. If a segment is unexpectedly large, check the
retention policy and the process log for `retention_boot_failed`.

### Branch 9 — Database integrity or lock suspicion

**First command** — run SQLite's read-only integrity check:

```bash
sqlite3 "$KELP_DB_PATH" "PRAGMA integrity_check;"
```

`ok` is the only healthy answer. Any other output means: stop the process,
copy the database and its `-wal`/`-shm` siblings into an evidence bundle, and
restore from the last verified backup. Never run `VACUUM` or destructive SQL
against a live file.

### Branch 10 — A secret may have leaked

**First command** — prove presence without printing the value, then search the
scrubbed logs for the redaction marker:

```bash
test -n "$KELP_PROVIDER_API_KEY" && echo "provider key is set"
grep -c '\[redacted\]' "$KELP_AUDIT_DIR"/audit.jsonl 2>/dev/null || true
```

If a value may have escaped, rotate it immediately (below), revoke owner
sessions, and capture an evidence bundle before rotating so the timeline is
preserved. The admin surface only ever returns key-presence booleans; a response
containing a value is a defect, not an operational state.

## Admin visibility recipes

Every recipe is read-only and uses the same owner token. Pipe through
`python -m json.tool` (or `jq`) for readability.

```bash
# Summary: plan id, run counts, usage, spend.
curl -sS -H "Authorization: Bearer $KELP_AUTH_TOKEN" \
  "$KELP_BASE_URL/api/admin/summary" | python -m json.tool

# Tenant index (only the caller's tenant appears).
curl -sS -H "Authorization: Bearer $KELP_AUTH_TOKEN" \
  "$KELP_BASE_URL/api/admin/tenants" | python -m json.tool

# Tenant detail: current plan, usage, spend, run counts, key presence booleans.
curl -sS -H "Authorization: Bearer $KELP_AUTH_TOKEN" \
  "$KELP_BASE_URL/api/admin/tenants/owner" | python -m json.tool

# Run index: filter by status or thread, page with limit/offset.
curl -sS -H "Authorization: Bearer $KELP_AUTH_TOKEN" \
  "$KELP_BASE_URL/api/admin/runs?limit=50&offset=0" | python -m json.tool

# One run by id (foreign ids look missing).
curl -sS -H "Authorization: Bearer $KELP_AUTH_TOKEN" \
  "$KELP_BASE_URL/api/admin/runs/run_example" | python -m json.tool

# Audit index: metadata only; command/extra are never returned.
curl -sS -H "Authorization: Bearer $KELP_AUTH_TOKEN" \
  "$KELP_BASE_URL/api/admin/audit?limit=50&offset=0" | python -m json.tool
```

Run counts are bounded by the newest 500-run window (`window: 500`); they are
not lifetime totals. Plan snapshots are recorder-side, so tenant detail shows
the current plan, not a reconstructed per-run history.

## Secret rotation

Rotate the provider key and the owner token independently. No command in this
section prints a value.

1. Write the new value to the deployment's private environment file or tenant
   secrets file. The tenant file must be mode `0600`:

```bash
chmod 600 "$KELP_TENANT_SECRETS_FILE"
python -m chat.server --check
```

2. Roll the process so it reads the new value:

```bash
systemctl --user restart kelp.service
# or, for a foreground process: send SIGTERM to the PID from pgrep -af "chat.server"
```

3. Prove presence and a healthy provider probe without printing the key:

```bash
test -n "$KELP_PROVIDER_API_KEY" && echo "provider key is set"
curl -sS -H "Authorization: Bearer $KELP_AUTH_TOKEN" \
  "$KELP_BASE_URL/api/readyz" | python -m json.tool
```

4. Rotate the owner token the same way. Existing sessions remain valid until
   they expire; revoke them explicitly if the token was exposed:

```bash
curl -sS -X DELETE -H "Authorization: Bearer $KELP_AUTH_TOKEN" \
  "$KELP_BASE_URL/api/auth/sessions"
```

5. Record the rotation in the audit trail by restarting once more, then confirm
   the admin surface still reports only booleans:

```bash
curl -sS -H "Authorization: Bearer $KELP_AUTH_TOKEN" \
  "$KELP_BASE_URL/api/admin/tenants/owner" | python -m json.tool
```

Never paste a real key into a shell history, an issue, a chat, or this runbook.
Examples in this repository use fake values only.

## Tenant suspension and offboarding

There is **no tenant CRUD endpoint** in P6. Suspension is a configuration change
and offboarding is a reviewed maintenance operation; neither is an admin API
mutation.

### Suspension

Map the tenant to a deny-all plan and restart. This keeps rows, usage, and audit
history intact while refusing new work through the ordinary entitlement gate.

```bash
export KELP_ENTITLEMENT_PLANS_JSON='{"suspended":{"plan_id":"suspended","allowed_task_types":[]}}'
export KELP_ENTITLEMENT_TENANT_PLANS_JSON='{"tenant-b":"suspended"}'
python -m chat.server --check
systemctl --user restart kelp.service
```

Verify the refusal and the plan id:

```bash
curl -sS -H "Authorization: Bearer $KELP_AUTH_TOKEN" \
  "$KELP_BASE_URL/api/admin/tenants/tenant-b" | python -m json.tool
```

(Suspension is still tenant-scoped: the current credential resolves `owner`, so
a request for `tenant-b` returns `404` from the admin detail route. Use the
service log and the returned `entitlement_denied` code to confirm the gate.)

### Offboarding

1. Revoke every owner session so no cookie survives the change:

```bash
curl -sS -X DELETE -H "Authorization: Bearer $KELP_AUTH_TOKEN" \
  "$KELP_BASE_URL/api/auth/sessions"
```

2. Take a consistent database backup during a stop window. `sqlite3 .backup`
   works while the process is stopped and is safe to verify after:

```bash
mkdir -p "$HOME/kelp-offboarding"
sqlite3 "$KELP_DB_PATH" ".backup '$HOME/kelp-offboarding/chat-$(date -u +%Y%m%dT%H%M%SZ).db'"
sqlite3 "$HOME/kelp-offboarding"/chat-*.db "PRAGMA integrity_check;"
```

3. Capture an evidence bundle (below), then archive it with the backup.
4. Delete tenant rows only in a reviewed maintenance window, with the process
   stopped and the backup verified. There is no supported admin endpoint for
   deletion; retention policy and `chat/retention.py` own normal expiry.
5. Rotate the tenant's provider key and any skill secrets that tenant could
   reach. A deleted row is not a rotated credential.

## Evidence bundle

Collect one directory per incident. It contains no secret values and no prompt
text; the admin endpoints are content-free by design.

```bash
set -eu
out="evidence-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$out"
curl -sS -H "Authorization: Bearer $KELP_AUTH_TOKEN" \
  "$KELP_BASE_URL/api/admin/summary" > "$out/summary.json"
curl -sS -H "Authorization: Bearer $KELP_AUTH_TOKEN" \
  "$KELP_BASE_URL/api/admin/tenants" > "$out/tenants.json"
curl -sS -H "Authorization: Bearer $KELP_AUTH_TOKEN" \
  "$KELP_BASE_URL/api/admin/tenants/owner" > "$out/tenant-owner.json"
curl -sS -H "Authorization: Bearer $KELP_AUTH_TOKEN" \
  "$KELP_BASE_URL/api/admin/runs?limit=200" > "$out/runs.json"
curl -sS -H "Authorization: Bearer $KELP_AUTH_TOKEN" \
  "$KELP_BASE_URL/api/admin/audit?limit=200" > "$out/audit.json"
curl -sS -H "Authorization: Bearer $KELP_AUTH_TOKEN" \
  "$KELP_BASE_URL/api/readyz" > "$out/readyz.json"
sqlite3 "$KELP_DB_PATH" "PRAGMA integrity_check;" > "$out/integrity.txt"
sha256sum "$out"/* > "$out/SHA256SUMS"
tar -czf "$out.tar.gz" "$out"
```

Before sharing the archive, confirm it contains only the files above and no
environment dump, key file, database copy, or raw conversation export.

## Rollback

Roll back the application, not the data. The frozen contract is additive inside
a major version; a schema rollback is a separate reviewed decision.

```bash
git log --oneline -10
git switch --detach <previous-known-good-commit>
python -m chat.server --check
python -m pytest -q
systemctl --user restart kelp.service
```

After restart:

```bash
curl -sS -H "Authorization: Bearer $KELP_AUTH_TOKEN" \
  "$KELP_BASE_URL/api/admin/summary" | python -m json.tool
```

If the rollback target predates a migration, stop and restore the verified
backup instead of running the older binary against the newer database. Never
delete `var/chat.db` to “start clean” without a backup and owner approval.

## Pilot handoff checklist

- [ ] `python -m chat.server --check` exits `0` and prints no secret value.
- [ ] `python -m pytest -q` is green apart from documented pre-existing skips.
- [ ] `GET /api/admin/summary` returns the expected plan id and run counts.
- [ ] `GET /api/admin/tenants` contains exactly the pilot tenant, not a list of
      every tenant the database has ever seen.
- [ ] `GET /api/admin/tenants/{tenant_id}` reports `keys` as booleans only.
- [ ] `GET /api/admin/runs` returns content-free rows; no `turn_id` placeholder
      appears for scheduled work.
- [ ] `GET /api/admin/audit` returns metadata only; no `command` or `extra`.
- [ ] Spend caps and the entitlement plan are written down in the handoff note.
- [ ] The owner token and provider key are stored outside the repository and
      are rotated on the pilot's schedule.
- [ ] The operator knows the six admin endpoints, the triage tree above, and how
      to build an evidence bundle.
- [ ] The on-call owner has read the known P5 interactions: double entitlement
      increments per accepted run, shared `max_turns`/`max_runs` counter, and
      current-plan-not-history in the tenant view.

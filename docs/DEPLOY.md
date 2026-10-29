# Deploy the KELP assistant (KEL-16, M2-F)

- **Status:** implemented, 30 October 2026
- **Branch:** `kel-16`, cut from `origin/main` at `e08b027`
- **Scope:** one container image, one Compose service, one systemd wrapper, one
  health probe, one `/data` volume. No application code changed.
- **Tests:** `tests/test_deploy_healthcheck.py` (23 cases); full suite 670 passed
- **Not in scope:** evals, chat/turn logic, payments, the tracker site, and any
  actual server. Nothing here was deployed to Nebius; every command below was
  executed locally against this image and is reported with its measured time.

This is the runbook a stranger follows to put the assistant on a VM and prove
it works. It assumes one operator, one VM, one owner, and a domain you control.
Read [§10 Single replica](#10-single-replica-is-a-hard-rule) before scaling
anything.

---

## 1. What actually runs

One Python process, `python -m chat.server`, serving plain HTTP on port `8765`.
It owns SQLite, the skill workspace, the audit log, and the usage log — all under
`/data`. Model calls leave the box over HTTPS to Nebius Token Factory; nothing
else does.

```
browser ──TLS──▶ Caddy/nginx on :443 ──HTTP──▶ kelp container :8765
                                                   │
                                          /data (named volume)
                                  chat.db · workspace/ · audit/ · usage/
```

The image is CPU-only. The "GPU" in the milestone is the *model host*: this
service is a thin client of Token Factory, so a 2 vCPU / 4 GB VM is enough
(measured idle footprint: ~50 MiB RSS, ~2% CPU).

### Files in this slice

| File | Purpose |
|---|---|
| `Dockerfile` | Two-stage build: dependencies resolved from `pyproject.toml` into a venv, runtime is `python:3.12-slim` with no compiler, runs as `10001:10001` |
| `.dockerignore` | Keeps secrets, state, tests, docs, and VCS metadata out of the build context |
| `docker-compose.yml` | The supported deployment: read-only rootfs, dropped capabilities, loopback publish, 1 GiB / 2 CPU, healthcheck, log rotation |
| `deploy/kelp.env.example` | The environment contract; copy to `deploy/kelp.env` (0600, gitignored) |
| `deploy/kelp.service` | systemd wrapper so the deployment starts at boot |
| `deploy/healthcheck.py` | Stdlib-only probe used by `HEALTHCHECK` and the smoke steps |

---

## 2. Prerequisites

- A Linux VM you can SSH into (x86_64; any Nebius CPU shape with 2 vCPU / 4 GB
  is sufficient). Open only `22`, `80`, `443`.
- Docker Engine 24+ with the Compose v2 plugin (`docker compose version`).
  Verified against Docker 29.9.1 / Compose 5.6.0.
- Outbound HTTPS to `https://api.tokenfactory.nebius.com` (readiness probes
  `GET /models`; turns call `POST /chat/completions`).
- A DNS name pointing at the VM, e.g. `kelp.example.com`.
- A Nebius Token Factory API key and one long random owner token:

  ```bash
  python3 -c "import secrets; print(secrets.token_urlsafe(32))"
  ```

The examples assume:

| Path | Meaning |
|---|---|
| `/opt/kelp` | This repository, checked out |
| `/etc/kelp` | Optional home for the env file if you do not keep it in `/opt/kelp/deploy` |
| `kelp-data` | Docker volume mounted at `/data` (name is pinned in the Compose file) |

---

## 3. Environment contract

The image sets every **non-secret** default below, so the env file only has to
carry the two required secrets. Secrets are not baked into the image and have
no default: the process refuses to start without them (`missing_api_key` or
`missing_auth_token`, exit `2`).

| Variable | Required | Default in image | Secret | What it does |
|---|---|---|---|---|
| `NEBIUS_API_KEY` | **yes** | — | **yes** | Token Factory key, server-side only |
| `KELP_AUTH_TOKEN` | **yes** | — | **yes** | Owner bearer token for every `/api/*` call except `/api/healthz` |
| `KELP_CSRF_TOKEN` | no | unset | **yes** | Checked as `X-CSRF-Token` on mutations; unset falls back to same-origin |
| `NEBIUS_TOKEN_FACTORY_BASE_URL` | no | `https://api.tokenfactory.nebius.com/v1` | no | Provider endpoint |
| `KELP_PRIMARY_MODEL` | no | `nvidia/Nemotron-3_5-Super-VL` | no | Conversational model |
| `KELP_BACKGROUND_MODEL` | no | `nvidia/Nemotron-3_5-Lightning` | no | Titles, summaries, compaction |
| `KELP_HOST` | no | `0.0.0.0` | no | Bind address inside the container |
| `KELP_PORT` | no | `8765` | no | Bind port |
| `KELP_DB_PATH` | no | `/data/chat.db` | no | SQLite database |
| `KELP_WORKSPACE_ROOT` | no | `/data/workspace` | no | Only tree skills may touch |
| `KELP_ARTIFACT_DIR` | no | `/data/workspace/var/artifacts` | no | Uploaded PDF/CSV content |
| `KELP_AUDIT_DIR` | no | `/data/audit` | no | Append-only `audit.jsonl` |
| `KELP_USAGE_DIR` | no | `/data/usage` | no | Append-only `usage.jsonl` |
| `KELP_SKILL_ROOT` | no | unset → built-in skills | no | Custom skill directory |
| `KELP_LOG_LEVEL` | no | `info` | no | `debug` / `info` / `warning` / `error` |
| `KELP_VERSION` / `KELP_GIT_COMMIT` | no | build args | no | Echoed by `/api/healthz`, `/api/readyz`, `/metrics` |

```bash
cd /opt/kelp
cp deploy/kelp.env.example deploy/kelp.env
chmod 600 deploy/kelp.env
$EDITOR deploy/kelp.env          # set NEBIUS_API_KEY and KELP_AUTH_TOKEN
```

`deploy/kelp.env` is gitignored and excluded from the build context. Never put
these values in `docker-compose.yml`, the `Dockerfile`, a shell history line, or
a ticket.

---

## 4. Build

```bash
cd /opt/kelp
export TAG=$(git rev-parse --short HEAD)
docker build \
  --build-arg KELP_VERSION=0.0.0 \
  --build-arg KELP_GIT_COMMIT="$TAG" \
  -t "kelp:$TAG" .
```

Measured: **7 s** cold on the verification box (no layer cache), **~195 MiB** on
disk, `USER 10001:10001`. The build context resolves to **66 files / 792 KB**:
`.dockerignore` keeps `tests/`, `evals/`, `var/`, `docs/`, `.git/`, and every env
file out — including the tracked `payments/.env.example` template.

What the image deliberately does *not* contain:

```bash
docker image inspect "kelp:$TAG" --format '{{range .Config.Env}}{{println .}}{{end}}'
# PATH, LANG, PYTHON*, KELP_HOST/PORT/paths, KELP_VERSION, KELP_GIT_COMMIT — no key, no token
```

---

## 5. First deployment

```bash
cd /opt/kelp
export TAG=$(git rev-parse --short HEAD)
export KELP_IMAGE="kelp:$TAG"

docker compose up -d
docker compose ps          # wait for STATUS "Up ... (healthy)"
```

Measured: container start to `healthy` in **7 s** from `up -d` (healthcheck
`start_period` is 40 s, so a cold first probe is never counted as a failure).

Confirm the surface from the host:

```bash
curl -fsS http://127.0.0.1:8765/api/healthz
# {"ok":true,"status":"ok","version":"0.0.0","commit":"e08b027",
#  "uptime_seconds":9.4,"checks":{}}
```

The port is published on `127.0.0.1` only. Do not change that until §8 is in
place: the service speaks plain HTTP and trusts its reverse proxy.

### Start at boot

```bash
sudo install -m 0644 deploy/kelp.service /etc/systemd/system/kelp.service
sudo systemctl daemon-reload
sudo systemctl enable --now kelp.service
systemctl status kelp.service     # active (exited); Compose owns the container
```

The unit is deliberately thin: systemd starts and stops the Compose project,
while Compose keeps the container up (`restart: unless-stopped`) and its
`HEALTHCHECK` reports readiness. Logs stay in `docker compose logs`:

```bash
docker compose -f /opt/kelp/docker-compose.yml logs --tail=100 kelp
```

`systemd-analyze verify deploy/kelp.service` passes.

---

## 6. Reverse proxy and TLS

Terminate TLS on the host and forward to loopback. The app must see the original
`Host` header (its CSRF fallback compares `Origin` to `Host`) and SSE must not be
buffered.

**Caddy** (automatic certificates):

```caddyfile
kelp.example.com {
    encode zstd gzip
    request_body {
        max_size 24MB
    }
    reverse_proxy 127.0.0.1:8765 {
        flush_interval -1
    }
}
```

**nginx**:

```nginx
server {
    listen 443 ssl;
    http2 on;
    server_name kelp.example.com;
    # 20 MiB upload cap plus multipart overhead
    client_max_body_size 24m;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Server-sent events: never buffer, and outlive the 300s idle window.
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
        add_header X-Content-Type-Options nosniff always;
    }
}
```

Notes:

- The stream route (`GET /api/threads/{id}/stream`) sends heartbeats every 15 s
  and closes after 300 s idle, so a proxy read timeout below 300 s will cut
  answers. `proxy_buffering off` is not optional.
- The app sets `X-Accel-Buffering: no`, which nginx honours for the stream even
  if buffering were left on; keep it off anyway.
- Do not add authentication at the proxy as a replacement for
  `KELP_AUTH_TOKEN`. Both are expected; the token is what the API checks.
- Behind the proxy, readiness and metrics are still owner-token routes. Use the
  token in `curl`, not a browser cookie, for those.

---

## 7. Health and readiness

| Route | Auth | Meaning |
|---|---|---|
| `GET /api/healthz` | none | Liveness. 200 `{"ok":true,...}` as long as the process serves HTTP. Never touches SQLite, disk, or the network. |
| `GET /api/readyz` | `Authorization: Bearer $KELP_AUTH_TOKEN` | Readiness. 200 only when every check passes; 503 with per-check codes otherwise. |
| `GET /api/metrics` | same token | Prometheus text (`chat_*`, `http_*`, tokens, cost) |
| `GET /api/health` | same token | Legacy alias, byte-compatible `{"ok":true}` |

Readiness checks: `store` (SQLite write lock), `artifacts` (throwaway file),
`audit` and `usage` (zero-byte append), `registry` (skill manifests loaded),
`provider` (`GET {base_url}/models`, 2 s timeout, cached 30 s). A check reports
only `not_configured`, `timeout`, or `unavailable` — never a path, a value, or
an exception message.

```bash
# liveness, unauthenticated, local
python3 deploy/healthcheck.py --live

# readiness, token read from the environment (never printed)
set -a; . deploy/kelp.env; set +a
python3 deploy/healthcheck.py --ready
# readyz ok (version=0.0.0 commit=e08b027)

# same through the public hostname once TLS is up
python3 deploy/healthcheck.py --ready --url https://kelp.example.com
```

`KELP_PORT` is the port *inside* the container. If you changed
`KELP_PUBLISH_PORT`, probe the host side explicitly:
`--url http://127.0.0.1:${KELP_PUBLISH_PORT}` (or the public `https://` name).

The image's `HEALTHCHECK` runs `--live` every 30 s (timeout 5 s, 3 retries,
start period 40 s) using only the standard library — no `curl` in the image.
`docker compose ps` therefore shows `(healthy)`/`(unhealthy)`.

Readiness is **not** wired into Docker restart: a process that is alive but
degraded stays up so the audit trail and in-flight turns survive. Alert on
`/api/readyz` returning 503, then investigate; restart deliberately with
`docker compose restart kelp`.

---

## 8. State layout

Everything mutable is one volume, `kelp-data`, mounted at `/data`:

| Path | Contents | Notes |
|---|---|---|
| `/data/chat.db` | threads, messages, blocks, events, turns, approvals | WAL mode, `synchronous=FULL`, one writer |
| `/data/chat.db-wal`, `-shm` | SQLite WAL sidecars | Present while the server runs |
| `/data/chat.db.lock` | advisory `flock` held by the live process | Second process exits `3` |
| `/data/workspace/` | skill workspace root (`${WORKSPACE}`) | Skills cannot escape it |
| `/data/workspace/var/artifacts/` | uploaded PDF/CSV bytes and metadata | 20 MiB per upload |
| `/data/workspace/var/index/` | `docs.answer` chunk cache | Created on the first document answer |
| `/data/audit/audit.jsonl` | append-only decisions, approvals, executions | Rotate by deployment, never by API |
| `/data/usage/usage.jsonl` | one record per turn: model, tokens, cost | Feeds the cost note |

Directories are created `0700`; new files are owner-only where the code creates
them (`chat.db`, `chat.db.lock`). All of it is owned by `10001:10001`. The
server adopts an existing directory's mode rather than re-permissioning it.

The root filesystem is read-only (`read_only: true`) with a 16 MiB `noexec`
`/tmp`. A write outside `/data` is a bug, and it fails loudly instead of
silently filling the container layer.

If you ever bind-mount a host directory instead of the named volume, create it
first with the right owner or the server cannot start:

```bash
sudo install -d -o 10001 -g 10001 -m 0700 /srv/kelp/data
```

---

## 9. Backup and restore

SQLite is in WAL mode, so copy it with the service stopped. The whole dataset is
small; a stop of a few seconds is cheaper than a torn snapshot.

```bash
cd /opt/kelp
sudo install -d -m 0700 /srv/kelp/backups
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

docker compose stop kelp
sudo docker run --rm --user 0:0 \
  -v kelp-data:/data:ro \
  -v /srv/kelp/backups:/backup \
  "kelp:$TAG" tar -C /data -czf "/backup/kelp-data-$STAMP.tar.gz" .
docker compose start kelp
```

Measured: **1 s** for the archive on the verification dataset (`chat.db` ~76 KB
plus logs). The archive preserves numeric ownership, so uid/gid 10001 survives.

Restore into a fresh volume (never untar over a running server):

```bash
cd /opt/kelp
ARCHIVE=/srv/kelp/backups/kelp-data-<stamp>.tar.gz

docker compose down
docker volume rm kelp-data
docker volume create kelp-data
sudo docker run --rm --user 0:0 \
  -v kelp-data:/data \
  -v /srv/kelp/backups:/backup:ro \
  "kelp:$TAG" tar -C /data --numeric-owner -xzf "/backup/$(basename "$ARCHIVE")"
docker compose up -d
```

Measured: restore **< 1 s**; the restored volume is `10001:10001` and the
database opens. Verify before trusting it:

```bash
sudo docker run --rm --user 0:0 -v kelp-data:/data:ro --entrypoint python "kelp:$TAG" -c "
import sqlite3
c = sqlite3.connect('file:/data/chat.db?mode=ro&immutable=1', uri=True)
print('threads', c.execute('select count(*) from threads').fetchone()[0])
print('turns',   c.execute('select count(*) from turns').fetchone()[0])
"
```

`immutable=1` is required for a read-only open of a WAL database with no shm
file; without it SQLite refuses to open the file. Then finish with the §12 smoke
so you have proven the restored service, not just the file.

Keep backups off the VM (`rsync`/object storage). A volume backup on the same
disk is not a backup.

---

## 10. Single replica is a hard rule

The server takes an exclusive `flock` on `<KELP_DB_PATH>.lock` for its lifetime.
A second instance against the same volume fails fast and honestly:

```
kelp: database_locked: Another KELP server already owns /data/chat.db
      (lock: /data/chat.db.lock).
$ echo $?    # 3
```

Verified by running a second `docker compose run` against the live volume.

Therefore:

- Never `docker compose up --scale kelp=2`, never two VMs behind one volume,
  never a rolling deployment that starts the new replica before the old stops.
  `docker compose up -d` recreates the container in place: stop, then start.
- Give shutdown room. `stop_grace_period: 30s` covers the server's 10 s turn
  settlement, so in-flight turns end `interrupted` and are resumable instead of
  vanishing with the process.
- Do not delete `chat.db.lock`; an empty file is expected, and deleting it does
  not release a lock. Find the owning process, stop it, then start.
- Horizontal scale needs a different store. That is not this milestone.

---

## 11. Rollout and rollback

Tag every build with the commit it came from; that tag is both the release
identity and the rollback target.

**Rollout**

```bash
cd /opt/kelp
git fetch origin
git switch --detach origin/main        # or the reviewed release commit
export TAG=$(git rev-parse --short HEAD)

# 1. build beside the running version — nothing restarts yet
docker build --build-arg KELP_GIT_COMMIT="$TAG" -t "kelp:$TAG" .

# 2. back up /data (§9) before touching the database
# 3. swap
KELP_IMAGE="kelp:$TAG" docker compose up -d
docker compose ps                       # (healthy)
python3 deploy/healthcheck.py --ready    # token from the env file
# 4. §12 smoke: one real turn and the offline eval summary
```

**Rollback**

```bash
cd /opt/kelp
export TAG=<previous-commit>
# If this release changed the database shape, restore the pre-rollout archive
# first (§9). The schema is currently CREATE TABLE IF NOT EXISTS with no
# migrations, so there is no down-migration to run.
KELP_IMAGE="kelp:$TAG" docker compose up -d
docker compose ps
```

Because there are no migrations yet, a rollback to an older image is safe only
when that image understands the current schema. That is exactly why rollout
starts with a backup: restoring the archive is the guaranteed path, and swapping
tags is the fast path.

Keep at least the previous tag (`docker image ls kelp`). Prune old tags only
after a new release has survived a day.

---

## 12. Post-deploy smoke

Three checks, in order. All of them are read-only except the one turn, which
writes normal state and costs a fraction of a cent.

### 12.1 Health and readiness

```bash
cd /opt/kelp
curl -fsS http://127.0.0.1:8765/api/healthz && echo
set -a; . deploy/kelp.env; set +a
python3 deploy/healthcheck.py --ready
```

Expected: `{"ok":true,...,"commit":"<tag>"}` and
`readyz ok (version=... commit=<tag>)`. A `503` here names the failing check
(`provider=timeout` is almost always egress or a bad key).

### 12.2 One real turn

Needs the real `NEBIUS_API_KEY`; this is the only step that proves the model
path, not just the process.

```bash
cd /opt/kelp
set -a; . deploy/kelp.env; set +a
BASE=${BASE:-http://127.0.0.1:8765}
AUTH="Authorization: Bearer $KELP_AUTH_TOKEN"

THREAD=$(curl -fsS -X POST "$BASE/api/threads" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"title":"deploy smoke"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["thread"]["id"])')
echo "thread=$THREAD"

curl -fsS -X POST "$BASE/api/threads/$THREAD/turns" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d "{\"client_turn_id\":\"smoke-$(date +%s)\",\"text\":\"Reply with the single word: ok\"}"

# Replay from the start; the stream tails until idle, so cap it.
timeout 60 curl -N -sS "$BASE/api/threads/$THREAD/stream?after=0" \
  -H "$AUTH" > /tmp/kelp-smoke.sse || true

grep -c '^event: text' /tmp/kelp-smoke.sse
grep -A1 '^event: turn_finished' /tmp/kelp-smoke.sse | grep -o '"reason":"[a-z]*"'
grep -A1 '^event: turn_finished' /tmp/kelp-smoke.sse | grep -o '"estimated_cost_usd":[^,}]*'
```

Expected:

- at least one `event: text` line — the model answered;
- `"reason":"complete"` on `turn_finished` (the failure path prints `"error"`);
- `"estimated_cost_usd"` greater than `0.0`;
- a matching line in `docker compose logs kelp` with `"event":"turn_finished"`,
  and one new record in `/data/usage/usage.jsonl`.

On the verification box, without a real key (provider pointed at a closed port),
the same commands produced
`["message_created","message_created","turn_started","error","turn_finished"]`
with `"reason":"error"` — which is exactly the distinction the smoke is meant to
catch. No token or key appeared in the logs, the SSE body, or the probe output.

### 12.3 Offline eval summary

No network, no provider key: the retrieval and answer-quality suites run from
fixtures. This is the honest offline quality gate in this milestone (the
dedicated eval harness is a separate work item).

```bash
cd /opt/kelp
python3 -m pytest tests/test_docs_answer.py tests/test_docs_answer_index.py | tail -1
python3 -m pytest | tail -1
```

Measured: **68 passed in 0.37 s** for the retrieval/answer suites and
**670 passed in 12.84 s** for the full suite. Record both numbers next to the
commit in the release note; a drop means the deploy is not the code you tested.

---

## 13. Cost

Two cost centres, both visible from `/data/usage/usage.jsonl` and `/api/metrics`.

**Tokens.** The repo's list prices (`kernel/config.py`, recorded 2026-10-28) are
per million tokens: Super VL `$0.30` in / `$0.90` out; Lightning `$0.06` in /
`$0.24` out. At an illustrative 2,000 input and 500 output tokens per turn:

```
2,000/1e6 × $0.30 = $0.00060
  500/1e6 × $0.90 = $0.00045
                    ─────────
                    $0.00105 per turn  ≈ $1.05 per 1,000 turns
```

Background work (titles, summaries) uses Lightning at roughly a fifth of that
rate. The number that matters is the one in `usage.jsonl`; unknown models record
no price rather than a fake `$0`.

**VM and disk.** One small CPU instance runs 24/7 at ~50 MiB RSS; a 20 GB volume
holds the database, uploads, and logs for a very long time. Check current Nebius
prices in the console — this document does not quote them. Egress is negligible:
requests are small, and the heavy lifting happens in Token Factory.

Cap spend at the provider, not just here. A missing cap is the only way this
gets expensive.

---

## 14. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Container restarts, logs `missing_api_key` / `missing_auth_token`, exit 2 | Env file not read | `docker compose config` shows the merged environment; check `deploy/kelp.env` path, mode `0600`, and that names have no typos |
| Exit 3, `database_locked` | Second replica, or an orphan process still holds the lock | `docker compose ps -a`; stop the extra container. Never delete the lock file |
| `(unhealthy)`, `curl /api/healthz` fails | Process wedged or port not bound | `docker compose logs kelp`; `docker compose restart kelp` |
| Healthy, but `readyz` 503 `provider=timeout` | Egress blocked, DNS broken, or key rejected | `curl -H "Authorization: Bearer $NEBIUS_API_KEY" $NEBIUS_TOKEN_FACTORY_BASE_URL/models` from the same network |
| `readyz` 503 `store=unavailable` | `/data` not writable by 10001 | Recreate the volume/dir with owner `10001:10001` |
| `401 unauthorized` on `/api/*` | Token mismatch, or the proxy strips `Authorization` | Send `Authorization: Bearer …`; check the proxy passes request headers untouched |
| `403 csrf_failed` on a browser mutation | `Origin` and `Host` disagree behind the proxy | Forward the original `Host`; or set `KELP_CSRF_TOKEN` and send `X-CSRF-Token` |
| Upload fails `413` | Proxy limit below the 20 MiB app cap | Raise `client_max_body_size` / Caddy `max_size` to ~24 MB |
| Answers stall mid-stream | Proxy buffering or a read timeout under 300 s | `proxy_buffering off`, `proxy_read_timeout 300s` |
| `touch: Read-only file system` inside the container | Something writes outside `/data` | Expected. Check `KELP_SKILL_ROOT` and any custom skill paths |
| Restore verification: `unable to open database file` | Read-only WAL database without a shm file | Add `immutable=1` to the SQLite URI (§9) |

---

## 15. Secret-handling checklist

Run these before you call a deployment done.

```bash
# 1. No secret in the image environment
docker image inspect "kelp:$TAG" --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | grep -Ei 'api_key|auth_token|csrf' && echo "LEAK" || echo "clean"

# 2. No secret in the layers
docker history --no-trunc "kelp:$TAG" | grep -Ei 'api_key|auth_token|secret' && echo "LEAK" || echo "clean"

# 3. No secret in the logs (one turn's worth)
docker compose logs --no-color kelp | grep -Ei 'bearer |api_key|auth_token' || echo "clean"

# 4. Readiness carries no paths or values
set -a; . deploy/kelp.env; set +a
curl -sS -H "Authorization: Bearer $KELP_AUTH_TOKEN" http://127.0.0.1:8765/api/readyz \
  | grep -Eo '"detail":"[a-z_]+"' | sort -u
```

On the verification run: image env clean, layer history clean, container logs
clean, and readiness details were `not_configured`, `timeout`, or `unavailable`
only. The env file lives outside the build context and is gitignored; the probe
sends the token as a header and never prints it.

---

## 16. What this runbook does not do

- **No HA.** One replica, one writer (see §10).
- **No automatic TLS.** Caddy/nginx does it; the image speaks HTTP only.
- **No eval harness.** §12.3 runs the existing offline quality suites; a
  dedicated repeatable eval is a separate milestone item.
- **No schema migrations.** `CREATE TABLE IF NOT EXISTS` only, so rollback safety
  depends on the backup, not a down-migration.
- **No remote state.** SQLite is a local file by design; do not put it on NFS.
- **Nothing was deployed.** All timings above were measured locally on 30
  October 2026 against this branch, image `kelp:kel-16`, and a disposable
  `kelp-data` volume; the volume, network, and containers were removed
  afterwards.

---

## 17. Verification record

| Check | Command | Result | Time |
|---|---|---|---|
| Image build | `docker build … -t kelp:kel-16 .` | 195 MiB, non-root `10001:10001`, 66-file context | 7 s |
| Boot to healthy | `docker compose up -d`, then poll `State.Health` | `healthy`, 0 restarts | 7 s |
| Liveness | `GET /api/healthz` | 200, `{"ok":true,...,"commit":"e08b027"}` | 1.3 ms |
| Readiness without token | `GET /api/readyz` | 401 `unauthorized` | — |
| Readiness with token | `healthcheck.py --ready` | `provider=unavailable` (dummy upstream), all other checks ok | — |
| Read-only rootfs | `touch /app/nope` in container | `Read-only file system` | — |
| Non-root | `id` in container | `uid=10001(kelp) gid=10001(kelp)` | — |
| State layout | `find /data` | dirs `0700`, db/lock `0600`, owner `kelp:kelp` | — |
| Single replica | second instance against the volume | exit `3`, `database_locked` | — |
| Restart persistence | stop + `up -d`, count rows | 2 threads / 2 turns before and after | — |
| Backup | tar of `/data` into a host backup dir | archive with numeric owners | 1 s |
| Restore | fresh volume + `tar --numeric-owner` | 2 threads / 2 turns, uid 10001 | < 1 s |
| Offline eval | `pytest tests/test_docs_answer*.py` | 68 passed | 0.37 s |
| Full suite | `python -m pytest` | 670 passed | 12.84 s |
| Unit tests | `pytest tests/test_deploy_healthcheck.py` | 23 passed | < 1 s |

Idle footprint: ~50 MiB RSS, ~2% CPU, no GPU, no swap.

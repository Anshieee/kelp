# Deploy the KELP assistant (KEL-16, M2-F)

- **Status:** implemented, merged into `main` at `3f4d6cc`, 30 October 2026
- **Branch:** `kel-16` (head `a63d49a`), cut from `origin/main` at `e08b027`
- **Scope:** one container image, one Compose service, one systemd wrapper, one
  health probe, one `/data` volume. No application code changed.
- **Tests:** `tests/test_deploy_healthcheck.py` (25 cases) and
  `tests/test_deploy_assets.py` (40 cases); full suite 712 passed on the branch
  and 734 on `main` after the merge, which added KEL-15's 22 eval tests
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
| `docker-compose.yml` | The supported deployment: image-only (never builds), read-only rootfs, dropped capabilities, loopback publish, 1 GiB / 2 CPU, healthcheck, log rotation. `KELP_IMAGE` is required with no default |
| `deploy/compose.env.example` | Compose interpolation variables (`KELP_IMAGE`, `KELP_PUBLISH_PORT`); copy to `/opt/kelp/.env`, no secrets |
| `deploy/kelp.env.example` | The container environment contract; copy to `deploy/kelp.env` (0600, gitignored) |
| `deploy/kelp.service` | systemd wrapper so the deployment starts at boot; loads `/opt/kelp/.env`, runs `up -d --no-build`, refuses to start without the tag |
| `deploy/healthcheck.py` | Stdlib-only probe used by `HEALTHCHECK` and the smoke steps |

---

## 2. Prerequisites

- A Linux VM you can SSH into (x86_64; any Nebius CPU shape with 2 vCPU / 4 GB
  is sufficient). Open only `22`, `80`, `443`.
- Docker Engine 24+ with the Compose v2 plugin (`docker compose version`).
  Verified against Docker 29.9.1 / Compose 5.6.0.
- `curl` 8.2+ on the VM for the smoke steps (`--header @file`, so the owner
  token never appears in `ps` or shell history). `python3` for the probe.
- `/opt/kelp` is the **build host**: the one place an image is built. A host that
  only runs the service needs Docker and the image, not a toolchain.
- For §12.3 only, `python3-venv` and `pip` on the build host, because the image
  deliberately ships no test dependencies:

  ```bash
  sudo apt-get install -y python3-venv    # Debian/Ubuntu, once
  ```

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
| `/opt/kelp` | This repository, checked out; the Compose project directory |
| `/opt/kelp/.env` | Compose interpolation only: `KELP_IMAGE`, `KELP_PUBLISH_PORT` (template `deploy/compose.env.example`). One path, because the systemd unit hardcodes it as `EnvironmentFile=` |
| `deploy/kelp.env` | Container environment: secrets and runtime config, mode `0600`, gitignored |
| `kelp-data` | Docker volume mounted at `/data` (name is pinned in the Compose file) |

Two env files, two jobs: the tag is Compose's business, the secrets are the
container's. Do not merge them — an env file listed under `env_file:` is passed
into the container and is not used for `${…}` interpolation, so putting a tag
there would be silently ignored.

---

## 3. Environment contract

The image sets every **non-secret** default below, so the env file only has to
carry the two required secrets. Secrets are not baked into the image and have
no default: the process refuses to start without them (`missing_api_key` or
`missing_auth_token`, exit `2`).

| Variable | Required | Default in image | Secret | What it does |
|---|---|---|---|---|
| `NEBIUS_API_KEY` | **yes** | — | **yes** | Token Factory key, server-side only |
| `KELP_AUTH_TOKEN` | **yes** | — | **yes** | Root owner bearer token for every `/api/*` call except `/api/healthz`; also the credential that mints a browser session |
| `KELP_CSRF_TOKEN` | no | unset | **yes** | Static header token for **bearer** mutations. Session cookies use a per-session double-submit pair instead (§11.1) |
| `KELP_SESSION_SECRET` | no | falls back to `KELP_AUTH_TOKEN` | **yes** | HMAC key for the derived per-device CSRF half; never stored. Set a dedicated value to rotate sessions independently of the root token (§11.1) |
| `KELP_SESSION_TTL_SECONDS` | no | `2592000` | no | Session lifetime from last use (30-day rolling); minimum `60` |
| `KELP_SESSION_MAX` | no | `1024` | no | Maximum live device sessions; oldest idle session is evicted |
| `KELP_SESSION_COOKIE_SECURE` | no | `1` | no | `0` only for a loopback HTTP development run; keep `1` in production |
| `KELP_AUTH_LOCKOUT_ATTEMPTS` | no | `10` | no | Failures per hashed identity before a `429` lockout; `0` disables the lockout |
| `KELP_AUTH_LOCKOUT_SECONDS` | no | `300` | no | Lockout duration; emitted as `Retry-After` |
| `KELP_AUTH_FAILURE_IDLE_SECONDS` | no | `300` | no | Idle-out for an auth-failure record |
| `KELP_AUTH_FAILURE_MAX_KEYS` | no | `4096` | no | Bound on the hashed auth-failure map |
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
| `KELP_PLUGIN_ROOT` | no | unset → `<repo>/var/plugins` | no | Installed plugin packages and `plugins.lock.json`. In the container set `/data/plugins`: the image rootfs is read-only and only `/data` persists |
| `KELP_PLUGINS_ALLOW_UNPINNED` | no | unset (`0`) | no | `1` loads packages that have no pin. Local experiments only; leave unset in production |
| `KELP_SPEND_CAP_DAY_USD` | no | unset → uncapped | no | Refuse new turns once today's persisted spend reaches this amount |
| `KELP_SPEND_CAP_MONTH_USD` | no | unset → uncapped | no | Same, for the calendar month |
| `KELP_SPEND_UNKNOWN_PRICE_MODE` | no | `block` | no | `block` or `count_as`: what an unpriced turn means for the cap |
| `KELP_SPEND_UNKNOWN_PRICE_USD` | no | `0.01` | no | Amount charged per unpriced turn in `count_as` mode |
| `KELP_LOG_LEVEL` | no | `info` | no | `debug` / `info` / `warning` / `error` |
| `KELP_RATE_LIMIT_ENABLED` | no | `1` | no | Token buckets on; `0` disables them (concurrency caps stay on) |
| `KELP_RATE_LIMIT_AUTH_RPM` / `_BURST` | no | `30` / `10` | no | Failed-auth attempts per minute and burst, per peer address |
| `KELP_RATE_LIMIT_CHAT_RPM` / `_BURST` | no | `120` / `30` | no | Thread, turn, and approval calls per client |
| `KELP_RATE_LIMIT_UPLOAD_RPM` / `_BURST` | no | `60` / `20` | no | Attachment uploads per client |
| `KELP_RATE_LIMIT_READ_RPM` / `_BURST` | no | `600` / `200` | no | Read calls per client |
| `KELP_RATE_LIMIT_ADMIN_RPM` / `_BURST` | no | `1200` / `300` | no | Metrics and readiness per client; keep it above the probe rate |
| `KELP_RATE_LIMIT_IDLE_SECONDS` | no | `300` | no | Idle-out before a limiter key starts with a full bucket again |
| `KELP_MAX_CONCURRENT_TURNS` | no | `4` | no | Global cap on turns running at once; `0` disables |
| `KELP_SCHEDULER_ENABLED` | no | `1` | no | Run the cron ticker that starts proactive turns; `0` keeps schedules stored but idle |
| `KELP_PROACTIVE_MAX_CONCURRENT` | no | `1` | no | Proactive turns allowed in flight at once; over the cap is a recorded skip, never a queue |
| `KELP_PROACTIVE_MAX_RUNS_PER_HOUR` | no | `5` | no | Proactive run starts per rolling hour; over the cap is a recorded skip |
| `KELP_PROACTIVE_FAILURE_LIMIT` | no | `3` | no | Consecutive failed proactive turns before that schedule is parked `disabled`; a clean turn resets the count |
| `KELP_RETENTION_AUDIT_DAYS` / `_AUDIT_KEEP` | no | `30` / `40` | no | Audit segment TTL and count cap (§8.1) |
| `KELP_RETENTION_USAGE_DAYS` / `_USAGE_KEEP` | no | `90` / `100` | no | Usage segment TTL and count cap (§8.1) |
| `KELP_RETENTION_ORPHAN_SECONDS` | no | `3600` | no | Grace before an unclaimed upload or unreferenced file is swept |
| `KELP_RETENTION_LOG_MAX_BYTES` | no | `10485760` | no | Audit/usage active-file size that forces a rotation |
| `KELP_RETENTION_LOG_ROTATE_SECONDS` | no | `86400` | no | Maximum segment interval for audit and usage records |
| `KELP_RETENTION_INTERVAL_SECONDS` | no | `86400` | no | Orphan-sweep, share-sweep, and rotation cadence after boot |
| `KELP_MAX_STREAMS_PER_OWNER` | no | `3` | no | Concurrent SSE streams per owner; `0` disables |
| `KELP_MAX_SHARE_STREAMS` | no | `16` | no | Concurrent anonymous share streams process-wide; `0` disables |
| `KELP_SHARE_TTL_SECONDS` | no | `604800` | no | Default share-link lifetime (7 days); minimum `60` |
| `KELP_SHARE_MAX_TTL_SECONDS` | no | `2592000` | no | Hard ceiling for a requested TTL (30 days); minimum `60` |
| `KELP_SHARE_MAX_SNAPSHOT_BYTES` | no | `4194304` | no | Share snapshot cap; a larger thread is refused, not truncated; minimum `1024` |
| `KELP_VERSION` / `KELP_GIT_COMMIT` | no | build args | no | Echoed by `/api/healthz`, `/api/readyz`, `/metrics` |

Rate-limit rejections are `429` JSON with `error.code = "rate_limited"` and a
`Retry-After` header in whole seconds. They are never a `5xx`: an internal
limiter fault fails open. The admin bucket must stay comfortably above the
readiness probe interval, or probes will see `429` instead of `503`. The
`/api/metrics` exposition includes `kelp_limiter_in_use`,
`kelp_limiter_capacity`, and `kelp_limiter_saturation` for the turn, owner
stream, and share stream (`share_streams`) caps.

```bash
cd /opt/kelp
cp deploy/kelp.env.example deploy/kelp.env
chmod 600 deploy/kelp.env
$EDITOR deploy/kelp.env          # set NEBIUS_API_KEY and KELP_AUTH_TOKEN
```

`deploy/kelp.env` is gitignored and excluded from the build context. Never put
these values in `docker-compose.yml`, the `Dockerfile`, a shell history line, or
a ticket.

### 3.1 Deployment variables: `/opt/kelp/.env`

Compose needs one thing the container does not: which image tag to run. That
lives in `/opt/kelp/.env`, which Compose reads for `${…}` interpolation and the
systemd unit reads through `EnvironmentFile=`. It holds no secret and is created
once:

```bash
cd /opt/kelp
sudo install -m 0644 deploy/compose.env.example /opt/kelp/.env
sudo $EDITOR /opt/kelp/.env      # KELP_IMAGE=<tag built in §4>, KELP_PUBLISH_PORT
```

| Variable | Default | Meaning |
|---|---|---|
| `KELP_IMAGE` | **none — required** | The tag to run, e.g. `kelp:1a2b3c4`. `docker-compose.yml` has no fallback and pulling is disabled, so a missing tag fails the start instead of building or fetching something else |
| `KELP_PUBLISH_PORT` | `8765` | Host port for the loopback publish. The container always listens on `8765`; the reverse proxy targets this host port |

Update `KELP_IMAGE` as part of every rollout (§11) and rollback. A tag that is
not on the host is a start failure by design — the unit never builds.

If you insist on another location, change it in **both** places or they will
disagree: `docker compose --env-file /path/.env …` for manual runs and
`EnvironmentFile=/path/.env` in `deploy/kelp.service` for boot. Compose's
`env_file:` directive is unrelated to this and must keep pointing at
`deploy/kelp.env`, which is the container's environment.

### 3.2 Installed plugin packages (KEL-25)

Plugins are optional deploy-time packages, not chat tools. Each package is a
directory or `.zip` with `manifest.json` and the module named by `entry.path`;
`python -m skills.cli install` validates it, pins its name, version, and SHA-256
digest in `<root>/plugins.lock.json`, and the server refuses to boot on a
missing pin or a digest/version mismatch.

The container root filesystem is read-only, so **set the plugin root under
`/data`** in `deploy/kelp.env` before installing anything:

```bash
# deploy/kelp.env (non-secret)
KELP_PLUGIN_ROOT=/data/plugins
# KELP_PLUGINS_ALLOW_UNPINNED is for local experiments only; never set it here.
```

Layout inside the `kelp-data` volume:

```
/data/plugins/
  plugins.lock.json       # {name: {name, version, digest}}
  plugins.echo/           # installed package (or plugins.echo.zip)
    manifest.json
    handler.py
```

Install from the build host with the volume mounted, or run the CLI inside a
one-off container that mounts the same volume, for example:

```bash
cd /opt/kelp
export KELP_IMAGE="$(sed -n 's/^KELP_IMAGE=//p' /opt/kelp/.env)"
docker run --rm -u 10001:10001 \
  -v kelp-data:/data \
  -v "$PWD:/src:ro" \
  --entrypoint python "$KELP_IMAGE" \
  -m skills.cli install /src/path/to/plugin --root /data/plugins
```

`--force` stages the copy beside the destination and swaps it only after the
staged digest matches the validated one, so a failed replacement leaves the
previous package and pin in place. `list --json` reports `pinned` and
`pin_matches` (digest **and** version); `uninstall <name>` removes both the
package and its pin. Restart the service after any change: packages are read at
boot, and a mismatch is a startup failure rather than a degraded mode.

---

## 4. Build

Build once, on the build host, and name it after the commit:

```bash
cd /opt/kelp
export TAG=$(git rev-parse --short HEAD)
docker build \
  --build-arg KELP_VERSION=0.0.0 \
  --build-arg KELP_GIT_COMMIT="$TAG" \
  -t "kelp:$TAG" .
```

Then make that tag the one the deployment runs — this line, not a Compose
build, is what a restart picks up:

```bash
sudo sed -i "s|^KELP_IMAGE=.*|KELP_IMAGE=kelp:$TAG|" /opt/kelp/.env
grep '^KELP_IMAGE=' /opt/kelp/.env        # kelp:<tag>
```

Measured: **6 s** cold on the verification box (`--no-cache`, 30 October 2026),
**194.5 MiB** (`docker image inspect` size), `USER 10001:10001`. The build
context is **656 kB** (`transferring context` on a fresh BuildKit builder, 65
files by the `.dockerignore` filter): `.dockerignore` keeps `tests/`, `evals/`,
`var/`, `docs/`, `.git/`, and every env file out — including the tracked
`payments/.env.example` and `deploy/compose.env.example` templates.

`docker-compose.yml` has no `build:` section, so `docker compose up` can only
start an image that already exists (§5). That is the point: a deploy that
silently rebuilds is a deploy whose tag means nothing.

What the image deliberately does *not* contain:

```bash
docker image inspect "kelp:$TAG" --format '{{range .Config.Env}}{{println .}}{{end}}'
# PATH, LANG, PYTHON*, KELP_HOST/PORT/paths, KELP_VERSION, KELP_GIT_COMMIT — no key, no token
```

That output is only safe to paste after the `grep` in §15 has passed, and even
then it is unnecessary. When reporting a problem, quote the tag and the failing
check code, never the environment.

---

## 5. First deployment

`KELP_IMAGE` is already in `/opt/kelp/.env` from §3.1, and §4 pointed it at the
tag you just built. Nothing here builds:

```bash
cd /opt/kelp
docker compose up -d --no-build
docker compose ps          # wait for STATUS "Up ... (healthy)"
```

Measured: container start to `healthy` in **7 s** from `up -d` (healthcheck
`start_period` is 40 s, so a cold first probe is never counted as a failure).

Reading that status column:

- `Up 3 seconds (health: starting)` means Docker is inside `start_period` — this
  is normal for the first ~40 s and is not a failure.
- `Up 12 seconds (healthy)` is the state you are waiting for; `(unhealthy)` after
  the start period is a real failure (§14).
- `docker compose ps` is a human view and its column layout is not an API. For
  scripts and for the verification record, ask Docker directly:

  ```bash
  docker inspect --format '{{.State.Health.Status}}' kelp-kelp-1   # healthy
  docker inspect --format '{{.State.Health.Status}} {{.RestartCount}}' kelp-kelp-1
  ```

  The container name is `<project>-<service>-1`; the project is pinned to `kelp`
  by `name:` in the Compose file, so the name is `kelp-kelp-1` on every host.

Confirm the surface from the host:

```bash
curl -fsS http://127.0.0.1:8765/api/healthz
# {"ok":true,"status":"ok","version":"0.0.0","commit":"<tag>",
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
`HEALTHCHECK` reports readiness. It reads `KELP_IMAGE` from `/opt/kelp/.env` and
runs `up -d --no-build`, so boot never rebuilds. Two `ExecStartPre` checks run
first: an unset `KELP_IMAGE` fails with `kelp: KELP_IMAGE is unset`, and a tag
that is not on the host fails with `No such image: kelp:<tag>` — a loud failure
instead of a stale container or a surprise rebuild. Logs stay in `docker compose logs`:

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
# readyz ok (version=0.0.0 commit=<tag>)

# same through the public hostname once TLS is up
python3 deploy/healthcheck.py --ready --url https://kelp.example.com
```

`KELP_PORT` is the port *inside* the container. If you changed
`KELP_PUBLISH_PORT` in `/opt/kelp/.env`, probe the host side explicitly:
`set -a; . ./.env; set +a` and then
`--url "http://127.0.0.1:${KELP_PUBLISH_PORT}"` (or the public `https://` name).

The image's `HEALTHCHECK` runs `--live` every 30 s (timeout 5 s, 3 retries,
start period 40 s) using only the standard library — no `curl` in the image.
`docker compose ps` therefore shows `(healthy)`/`(unhealthy)`, and
`(health: starting)` during the 40 s start period: the first three probes after
boot are expected to run before the status settles, so read the column again
before treating it as a failure. That status is Docker's view of one container,
not readiness — `--ready` is the check that exercises SQLite and the provider.

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
| `/data/chat.db-wal` | write-ahead log | Exists while the database is open, and after an unclean exit. A clean close checkpoints it and deletes it, so a stopped server normally shows only `chat.db`; a `SIGKILL` can leave committed pages here, which is why a crash backup must include it |
| `/data/chat.db-shm` | WAL shared-memory index | Exists only while a process has the database open and is deleted by the last close. It holds no committed data, so it is never part of a backup |
| `/data/chat.db.lock` | advisory `flock` held by the live process | Second process exits `3` |
| `/data/workspace/` | skill workspace root (`${WORKSPACE}`) | Skills cannot escape it |
| `/data/workspace/var/artifacts/` | uploaded PDF/CSV bytes and metadata | 20 MiB per upload |
| `/data/workspace/var/index/` | `docs.answer` chunk cache | Created on the first document answer |
| `/data/audit/audit.jsonl` | append-only decisions, approvals, executions | Rotated by the app (daily/size); retained segments are read-transparent |
| `/data/usage/usage.jsonl` | one record per turn: model, tokens, cost | Rotated by the app; spend totals include retained segments |

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

### 8.1 Retention table

One table is the contract: `chat/retention.py` defines the values and
`RETENTION_ROWS`, and this table mirrors it. Boot runs the sweep once; a daemon
thread then runs the same pass every `KELP_RETENTION_INTERVAL_SECONDS` (24 h by
default). The sweep logs counts only — never thread text, file names, or
artifact names.

| Data class | Where | TTL / trigger | Enforced by | Override |
|---|---|---|---|---|
| Chat content | `chat.db` threads, messages, blocks, events, turns, approvals, shares, share_views | Until the owner deletes the thread (immediate hard delete, no `deleted_at` shadow); expired, revoked, and parentless share rows are pruned on the same boot + daily pass | `ChatStore.purge_thread`; `ChatStore.share_sweep` via `RetentionMaintenance.run_once` | none (explicit delete only) |
| Uploaded files | `KELP_ARTIFACT_DIR` (default `workspace/var/artifacts`) | Thread lifetime; unclaimed or parentless uploads after 1 h grace | `ArtifactRegistry.purge` / `sweep_orphans`, boot + daily | `KELP_RETENTION_ORPHAN_SECONDS` |
| Derived index | `workspace/var/index` (`docs.answer` chunk cache) | Removed eagerly when the artifact or thread is deleted; orphan dirs swept | `ArtifactRegistry.purge` → `remove_index`; `sweep_orphans` | none (follows the artifact) |
| Audit records | `KELP_AUDIT_DIR/audit.jsonl` and its segments | 30 days, capped at 40 segments | `RetentionMaintenance.rotate`, boot + daily | `KELP_RETENTION_AUDIT_DAYS`, `KELP_RETENTION_AUDIT_KEEP` |
| Usage records | `KELP_USAGE_DIR/usage.jsonl` and its segments | 90 days, capped at 100 segments | `RetentionMaintenance.rotate`, boot + daily | `KELP_RETENTION_USAGE_DAYS`, `KELP_RETENTION_USAGE_KEEP` |
| Request/correlation logs | container stdout, not the data volume | Size-bounded, not age-bounded: 10 MiB × 3 files per service (≈30 MiB) | `docker-compose.yml` `logging` block (json-file `max-size` / `max-file`) | `docker-compose.yml` `logging.options` (operator config) |
| Temporary artifacts | `/tmp` (16 MiB `noexec` tmpfs) | Container lifetime | container tmpfs, never persisted | `docker-compose.yml` |

Rotation renames the active file to a timestamped segment; it never rewrites a
line, so an append already in flight finishes inside that segment instead of
being dropped. A segment older than its TTL or beyond the count cap is unlinked
whole. Both readers (`AuditLog.entries`, `JsonlUsageStore.records`, and
`SpendGuard`) include the retained segments, so rotation can never reset a
day/month spend window. Size rotation also fires when a file reaches
`KELP_RETENTION_LOG_MAX_BYTES` (10 MiB by default); the interval is
`KELP_RETENTION_LOG_ROTATE_SECONDS` (86,400).

Deleting a thread runs `VACUUM` after the delete transaction commits (and
truncates the WAL) so the file on disk shrinks; the daily orphan sweep does the
same when it removed registry rows. Both run outside an HTTP read path.

On-box files outside the app's own records follow the same clock. The container
log driver in `docker-compose.yml` bounds stdout **by size, not by age**: ten
MiB per file, three files, about 30 MiB per service, with the oldest file
deleted first. Age filtering (`max-age`) is not configured, so the runbook
makes no day-based claim for these logs; change both the `logging` block and
this table together if the operator wants one:

```yaml
logging:
  driver: json-file
  options:
    max-size: "10m"
    max-file: "3"
```

---

## 9. Backup and restore

SQLite is in WAL mode, so copy it with the service stopped. The whole dataset is
small; a stop of a few seconds is cheaper than a torn snapshot.

```bash
cd /opt/kelp
# TAG is re-declared here because §4's shell is gone by the time you back up.
# Prefer the tag the deployment actually runs (KELP_IMAGE in /opt/kelp/.env);
# fall back to the checkout you are on.
export TAG=$(git rev-parse --short HEAD)
set -a; . ./.env 2>/dev/null; set +a
export IMG="${KELP_IMAGE:-kelp:$TAG}"

sudo install -d -m 0700 /srv/kelp/backups
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

docker compose stop kelp
sudo docker run --rm --user 0:0 \
  -v kelp-data:/data:ro \
  -v /srv/kelp/backups:/backup \
  "$IMG" tar -C /data -czf "/backup/kelp-data-$STAMP.tar.gz" .
docker compose start kelp
```

Measured: **1 s** for the archive on the verification dataset (`chat.db` ~76 KB
plus logs). The archive preserves numeric ownership, so uid/gid 10001 survives.
`stop` is not cosmetic: the clean close checkpoints the WAL and removes the
`-wal`/`-shm` sidecars (§8), so the tar is one complete database file rather
than a `chat.db` that still depends on a journal next to it.

Restore into a fresh volume (never untar over a running server):

```bash
cd /opt/kelp
export TAG=$(git rev-parse --short HEAD)
set -a; . ./.env 2>/dev/null; set +a
export IMG="${KELP_IMAGE:-kelp:$TAG}"
ARCHIVE=/srv/kelp/backups/kelp-data-<stamp>.tar.gz

docker compose down
docker volume rm kelp-data
docker volume create kelp-data
sudo docker run --rm --user 0:0 \
  -v kelp-data:/data \
  -v /srv/kelp/backups:/backup:ro \
  "$IMG" tar -C /data --numeric-owner -xzf "/backup/$(basename "$ARCHIVE")"
docker compose up -d --no-build
```

Measured: restore **< 1 s**; the restored volume is `10001:10001` and the
database opens. Verify before trusting it:

```bash
sudo docker run --rm --user 0:0 -v kelp-data:/data:ro --entrypoint python "$IMG" -c "
import sqlite3
c = sqlite3.connect('file:/data/chat.db?mode=ro&immutable=1', uri=True)
print('threads', c.execute('select count(*) from threads').fetchone()[0])
print('turns',   c.execute('select count(*) from turns').fetchone()[0])
"
```

`immutable=1` is a promise, not a mode: it tells SQLite the file will never
change and it can ignore the WAL, which is what makes a read-only open work when
the `-shm` file cannot be created. It is only true here because the archive was
taken from a stopped server — never point `immutable=1` at a live `/data`, or
SQLite will read stale pages and skip locks without complaining. The safe
alternative on a live volume is a `sqlite3` backup API copy
(`.backup`/`VACUUM INTO`), which §9 does not need because it stops the service.
Then finish with the §12 smoke so you have proven the restored service, not just
the file.

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
  `docker compose up -d --no-build` recreates the container in place: stop, then
  start.
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

# 1. build beside the running version — nothing restarts yet, no tag changes
docker build --build-arg KELP_GIT_COMMIT="$TAG" -t "kelp:$TAG" .

# 2. back up /data (§9) before touching the database
# 3. swap: record the new tag, then start it. No --build, no rebuild.
sudo sed -i "s|^KELP_IMAGE=.*|KELP_IMAGE=kelp:$TAG|" /opt/kelp/.env
docker compose up -d --no-build
docker compose ps                       # (healthy); see §5 on reading it
python3 deploy/healthcheck.py --ready    # token from the env file
# 4. §12 smoke: one real turn and the offline eval summary
```

The swap is two statements on purpose: the build and the tag change are separate,
so a failed build cannot leave `/opt/kelp/.env` pointing at an image that does
not exist.

**Rollback**

```bash
cd /opt/kelp
export TAG=<previous-commit>
# If this release changed the database shape, restore the pre-rollout archive
# first (§9). The schema is currently CREATE TABLE IF NOT EXISTS with no
# migrations, so there is no down-migration to run.
sudo sed -i "s|^KELP_IMAGE=.*|KELP_IMAGE=kelp:$TAG|" /opt/kelp/.env
docker compose up -d --no-build
docker compose ps
```

Rollback never builds either: it starts a tag that must still be on the host,
which is why `docker image ls kelp` is a precondition, not a nicety. If the old
image was pruned, the unit's `docker image inspect` guard (and Compose itself)
fail with `No such image: kelp:<tag>`, and the archive restore in §9 is the only
remaining path.

Because there are no migrations yet, a rollback to an older image is safe only
when that image understands the current schema. That is exactly why rollout
starts with a backup: restoring the archive is the guaranteed path, and swapping
tags is the fast path.

Keep at least the previous tag (`docker image ls kelp`). Prune old tags only
after a new release has survived a day.

### 11.1 Owner sessions and credential rotation

The root `KELP_AUTH_TOKEN` stays server-side and is the recovery credential.
Browsers exchange it for an opaque server-side session; the session cookie is
`HttpOnly; Secure; SameSite=Lax`, rolls forward 30 days on each authenticated
response, and never contains the root token. Sessions are stored in the
`sessions` table of the chat database, so several devices survive a restart;
each row carries a short label, timestamps, and the SHA-256 of the session id.
The CSRF half is derived with HMAC-SHA256 keyed by the SHA-256 of
`KELP_SESSION_SECRET` (falling back to `KELP_AUTH_TOKEN`), over the `kelp-csrf:`
prefix plus the stored id hash, so **no session id or CSRF token is written to
the database**. A restart alone no longer logs browsers out; the bearer
token still works and `DELETE /api/auth/sessions` remains the global reset.

**Sign in (the bearer is used only for this exchange and is never the cookie):**

```bash
# umask keeps the transient response files private; read the token from the
# protected env file, never from a command line.
umask 077
set -a; . deploy/kelp.env; set +a
headers=$(mktemp)
body=$(mktemp)
curl -sS -D "$headers" -o "$body" \
  -X POST "https://kelp.example.com/api/auth/session" \
  -H "Authorization: Bearer ${KELP_AUTH_TOKEN}" \
  -H "Content-Type: application/json" \
  --data '{"device_label":"deploy laptop"}'
grep -i '^set-cookie:' "$headers"   # session=...; HttpOnly; Secure; SameSite=Lax
python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["session"]["csrf_token"])' "$body"
rm -f "$headers" "$body"
```

The JSON body returns `session.expires_at`, the readable CSRF half, and the
device's public `id`/`label`. A browser receives both cookies from the same
response; a script that manages cookies itself must send the `csrf` cookie and
the matching `X-CSRF-Token` header on mutations, plus an `Origin` matching the
public host. `SESSION_COOKIE` below is the `session` cookie value and
`CSRF_TOKEN` is the `csrf` cookie value (or the value printed above).

**List devices, then revoke one or every session:**

```bash
# Authenticated list: current:true marks the cookie that made this call.
curl -sS "https://kelp.example.com/api/auth/devices" \
  -H "Cookie: session=${SESSION_COOKIE}" \
  -H "Origin: https://kelp.example.com"

# Revoke one device by its short id; the caller keeps its own session.
curl -sS -X DELETE "https://kelp.example.com/api/auth/devices/${DEVICE_ID_SHORT}" \
  -H "Origin: https://kelp.example.com" \
  -H "X-CSRF-Token: ${CSRF_TOKEN}" \
  -H "Cookie: session=${SESSION_COOKIE}; csrf=${CSRF_TOKEN}"

# Presented session: requires the csrf cookie + header and a same-origin Origin.
curl -sS -X DELETE "https://kelp.example.com/api/auth/session" \
  -H "Origin: https://kelp.example.com" \
  -H "X-CSRF-Token: ${CSRF_TOKEN}" \
  -H "Cookie: session=${SESSION_COOKIE}; csrf=${CSRF_TOKEN}"

# Every session (bearer path; no cookie needed).
curl -sS -X DELETE "https://kelp.example.com/api/auth/sessions" \
  -H "Authorization: Bearer ${KELP_AUTH_TOKEN}"
```

`DELETE /api/auth/session` and `DELETE /api/auth/sessions` always answer with
both cookies cleared (`Max-Age=0`). `DELETE /api/auth/devices/${DEVICE_ID_SHORT}`
clears them only when the id is the caller's own device; revoking another device
leaves the caller's cookies alone, and the bearer call above sends none at all.
Every revoke takes effect immediately, including for an open SSE stream on the
next auth check. Revoking a device marks its row `revoked_at`, so its next
request (and its next stream heartbeat) answers `401`.

**Rotate `KELP_AUTH_TOKEN`, `KELP_SESSION_SECRET`, or `KELP_CSRF_TOKEN`:**

1. Generate a new random value on the host; do not paste it into a ticket or a
   shell history line:
   `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'`.
2. Revoke every existing session **before** changing the secret, using the
   current value from `deploy/kelp.env`:
   `curl -sS -X DELETE .../api/auth/sessions -H "Authorization: Bearer ${KELP_AUTH_TOKEN}"`.
3. Put the new value in `deploy/kelp.env` (`KELP_AUTH_TOKEN=...`,
   `KELP_SESSION_SECRET=...`) and keep mode `0600`. Never commit the file.
4. Restart the one container: `docker compose up -d --no-build` (or
   `docker compose restart kelp`). A restart alone keeps sessions, but the
   explicit revoke makes the intent auditable and covers a rolling process that
   has not restarted yet.
5. Confirm readiness with the new token:
   `python3 deploy/healthcheck.py --ready` (reads `deploy/kelp.env`).
6. Confirm the old token now gets `401 unauthorized`, then remove the old value
   from any password manager entry and keep only the new one.

Rotating `KELP_SESSION_SECRET` on its own invalidates every derived CSRF pair
without deleting the device rows; each device re-receives a fresh `csrf` cookie
on its next authenticated response. Rotating `KELP_CSRF_TOKEN` has the same
steps as the root token; because session cookies use the per-session derived
pair, an old static token cannot mutate a cookie-authenticated request in any
case.

**If a session is suspected stolen:** call `DELETE /api/auth/sessions` with the
bearer token, restart if the bearer itself may be exposed, then rotate both
secrets. `auth_failed` lines (event `auth_failed`, hashed `identity_hash`,
`outcome`, `count`) are the signal to inspect; they never contain the token,
cookie, or peer address.

**Local HTTP only:** set `KELP_SESSION_COOKIE_SECURE=0` for a loopback
`http://127.0.0.1` development run. Never set it on the public deployment;
without `Secure`, a cookie can cross a plaintext hop.

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

# The owner token goes in a 0600 header file, never on the command line: `-H
# "Authorization: ..."` would put it in `ps` and in shell history. curl 8.2+
# reads headers from a file with `-H @file`.
umask 077
HDRS=$(mktemp)
printf 'Authorization: Bearer %s\n' "$KELP_AUTH_TOKEN" > "$HDRS"
trap 'rm -f "$HDRS"' EXIT

THREAD=$(curl -fsS -X POST "$BASE/api/threads" \
  -H @"$HDRS" -H 'Content-Type: application/json' \
  -d '{"title":"deploy smoke"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["thread"]["id"])')
echo "thread=$THREAD"

curl -fsS -X POST "$BASE/api/threads/$THREAD/turns" \
  -H @"$HDRS" -H 'Content-Type: application/json' \
  -d "{\"client_turn_id\":\"smoke-$(date +%s)\",\"text\":\"Reply with the single word: ok\"}"

# Replay from the start; the stream tails until idle, so cap it.
timeout 60 curl -N -sS "$BASE/api/threads/$THREAD/stream?after=0" \
  -H @"$HDRS" > /tmp/kelp-smoke.sse || true

grep -c '^event: text' /tmp/kelp-smoke.sse
grep -A1 '^event: turn_finished' /tmp/kelp-smoke.sse | grep -o '"reason":"[a-z]*"'
grep -A1 '^event: turn_finished' /tmp/kelp-smoke.sse | grep -o '"estimated_cost_usd":[^,}]*'
```

The `trap` deletes the header file when the shell exits, including on `Ctrl-C`.
If your curl predates 8.2, use `--config` with a `header = "Authorization: …"`
line in the same 0600 file instead of `-H @`; do not fall back to argv.

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

Run it **on the build host, outside the container**: the image excludes `tests/`
and ships no pytest (§2). One venv, once:

```bash
cd /opt/kelp
python3 -m venv .venv                       # needs python3-venv (§2)
.venv/bin/pip install -q pytest httpx jsonschema
.venv/bin/python -m pytest tests/test_docs_answer.py tests/test_docs_answer_index.py | tail -1
.venv/bin/python -m pytest | tail -1
```

`.venv/` is gitignored and excluded from the build context, so it changes
nothing about the image. Run the same two commands on the VM after a deploy only
if the VM is also the checkout you tested; otherwise record the numbers from the
build host next to the commit, and treat the VM as proven by §12.1 and §12.2.

Measured on the verification host: **68 passed in 0.74 s** for the
retrieval/answer suites and **712 passed in 12.82 s** for the full suite on the
`kel-16` branch; `main` after the merge reports **734 passed in 15.02 s**,
because it also carries KEL-15's eval tests. Record both numbers next to the
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

### 13.1 Spend guardrail

Two ceilings, both optional and read at boot:

```bash
KELP_SPEND_CAP_DAY_USD=2.00      # refuse new turns after $2 today (UTC)
KELP_SPEND_CAP_MONTH_USD=40.00   # refuse new turns after $40 this month (UTC)
KELP_SPEND_UNKNOWN_PRICE_MODE=block   # block | count_as
KELP_SPEND_UNKNOWN_PRICE_USD=0.01     # count_as only
```

The totals are recomputed from `/data/usage/usage.jsonl` plus its retained
segments on every check, so a rotation mid-window does not reset the counter and
a restart neither forgets the total nor the warning already sent. Behaviour:

- **80% of a window** — one structured `spend_cap_warning` log line and audit
  entry, plus the `spend_used_usd`, `spend_cap_usd`, `spend_cap_ratio`, and
  `spend_unknown_price_records` gauges. Once per window, not per turn.
- **100% of a window** — `POST /api/threads/{id}/turns` answers **402** with
  `code: "spend_cap_reached"` and `retryable: false` before the turn row, the
  messages, or any provider call exist. The audit trail gets a
  `spend_cap_blocked` entry. A blocked retry behaves the same way.
- **Unknown prices** — a model with no list price records
  `estimated_cost_usd: null`. `block` (the default) refuses new turns while such
  a record sits in a capped window, because an unmeasurable total is not a cap.
  `count_as` instead charges each such record `KELP_SPEND_UNKNOWN_PRICE_USD`, and
  reports the count in the gauge and the error details.
- **Unreadable usage** — if `usage.jsonl` cannot be read while a cap is set, new
  turns are refused with reason `usage_unavailable` (fail closed), not allowed
  through at an unknown spend.

A malformed cap value fails startup with `invalid_config`; it never silently
means "unlimited".

### 13.2 Deploy checklist: cap spend at the provider too

The app cap protects the product; the provider cap protects the account. Do
both, in this order:

- [ ] Set a hard monthly spend limit in the Token Factory console for the key
      this deployment uses, at or slightly above `KELP_SPEND_CAP_MONTH_USD`.
- [ ] Confirm `KELP_SPEND_CAP_DAY_USD`/`_MONTH_USD` are set in
      `deploy/kelp.env` (the container environment; no secret belongs there but
      these are plain config) and that the file is still `0600`.
- [ ] After the first real turn (§12.2), check `/metrics`:
      `spend_used_usd{window="day"}` is non-zero and
      `spend_unknown_price_records{window="day"}` is `0`. A non-zero unknown
      count with a cap configured means the configured model has no price in
      `kernel/config.py`; add one or choose `count_as`.
- [ ] Verify the refusal path once, deliberately: set
      `KELP_SPEND_CAP_DAY_USD=0`, restart, and confirm a send returns `402`
      `spend_cap_reached` **without** a provider request in the logs. Restore
      the real value and restart.

Cap spend at the provider, not just here. A missing provider cap is the only way
this gets expensive.

---

## 14. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `up` fails: `KELP_IMAGE is required` / `set KELP_IMAGE to a built tag` | No `/opt/kelp/.env`, or the variable is blank | Create it from `deploy/compose.env.example` and set the tag (§3.1) |
| `up` fails: `image kelp:<tag> is not on this host` / `pull access denied` | The tag was never built here, or was pruned. Pulling is disabled on purpose | Build it (§4) or restore the previous tag; never re-add a `build:` section to make the message go away |
| Container restarts, logs `missing_api_key` / `missing_auth_token`, exit 2 | Env file not read | `docker compose config -q` validates the project without printing values; then check `deploy/kelp.env` exists, is mode `0600`, and the names have no typos (never paste the bare form's output — it contains the tokens) |
| Exit 3, `database_locked` | Second replica, or an orphan process still holds the lock | `docker compose ps -a`; stop the extra container. Never delete the lock file |
| `(unhealthy)`, `curl /api/healthz` fails | Process wedged or port not bound | `docker compose logs kelp`; `docker compose restart kelp` |
| `(health: starting)` for the first minute | Normal: 40 s `start_period` plus a 30 s interval | Wait, then re-read `docker inspect --format '{{.State.Health.Status}}' kelp-kelp-1` |
| Healthy, but `readyz` 503 `provider=timeout` | Egress blocked, DNS broken, or key rejected | Probe the provider from the same network with the key in a 0600 header file (`-H @file`, §12.2), not on the command line |
| `readyz` 503 `store=unavailable` | `/data` not writable by 10001 | Recreate the volume/dir with owner `10001:10001` |
| `401 unauthorized` on `/api/*` | Token mismatch, or the proxy strips `Authorization` | Send `Authorization: Bearer …`; check the proxy passes request headers untouched |
| `403 csrf_failed` on a browser mutation | `Origin` and `Host` disagree behind the proxy | Forward the original `Host`; or set `KELP_CSRF_TOKEN` and send `X-CSRF-Token` |
| Upload fails `413` | Proxy limit below the 20 MiB app cap | Raise `client_max_body_size` / Caddy `max_size` to ~24 MB |
| Answers stall mid-stream | Proxy buffering or a read timeout under 300 s | `proxy_buffering off`, `proxy_read_timeout 300s` |
| `touch: Read-only file system` inside the container | Something writes outside `/data` | Expected. Check `KELP_SKILL_ROOT` and any custom skill paths |
| Restore verification: `unable to open database file` | Read-only WAL database without a shm file | Add `immutable=1` to the SQLite URI — only for a stopped copy (§9) |

---

## 15. Secret-handling checklist

Run these before you call a deployment done.

```bash
cd /opt/kelp
export TAG=$(git rev-parse --short HEAD)   # or the tag in /opt/kelp/.env

# 0. The project resolves without printing its environment. `-q` is the whole
#    point: the bare form echoes every secret it interpolates.
docker compose config -q && echo "config ok"

# 1. No secret in the image environment
docker image inspect "kelp:$TAG" --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | grep -Ei 'api_key|auth_token|csrf' && echo "LEAK" || echo "clean"

# 2. No secret in the layers
docker history --no-trunc "kelp:$TAG" | grep -Ei 'api_key|auth_token|secret' && echo "LEAK" || echo "clean"

# 3. No secret in the logs (one turn's worth)
docker compose logs --no-color kelp | grep -Ei 'bearer |api_key|auth_token' || echo "clean"

# 4. Readiness carries no paths or values (token via a 0600 header file)
set -a; . deploy/kelp.env; set +a
umask 077
HDRS=$(mktemp)
printf 'Authorization: Bearer %s\n' "$KELP_AUTH_TOKEN" > "$HDRS"
curl -sS -H @"$HDRS" http://127.0.0.1:8765/api/readyz \
  | grep -Eo '"detail":"[a-z_]+"' | sort -u
rm -f "$HDRS"
```

**Never paste plain `compose config` output (the form without `-q`) or
`docker inspect` output into a ticket, a chat, or a PR.** Both print the merged
environment, and `KELP_AUTH_TOKEN` / `NEBIUS_API_KEY` are in it in clear text.
`config -q` only validates; when something is wrong, report the tag, the failing
check code (`provider=timeout`), and whether the file exists — not its contents.

On the verification run: image env clean, layer history clean, container logs
clean, and readiness details were `not_configured`, `timeout`, or `unavailable`
only. The env file lives outside the build context and is gitignored; the probe
sends the token as a header and never prints it.

---

## 16. What this runbook does not do

- **No HA.** One replica, one writer (see §10).
- **No automatic TLS.** Caddy/nginx does it; the image speaks HTTP only.
- **No dedicated eval run in the image.** §12.3 runs the offline quality suites
  on the build host; the repeatable eval harness is KEL-15, outside the container.
- **No schema migrations.** `CREATE TABLE IF NOT EXISTS` only, so rollback safety
  depends on the backup, not a down-migration.
- **No remote state.** SQLite is a local file by design; do not put it on NFS.
- **Nothing was deployed.** All timings above were measured locally on 30
  October 2026 against this branch, image `kelp:9b1c863`, and a disposable
  `kelp-data` volume; the volume, network, and containers were removed
  afterwards.

---

## 17. Verification record

| Check | Command | Result | Time |
|---|---|---|---|
| Image build | `docker build --no-cache … -t kelp:<tag> .` | 194.5 MiB, non-root `10001:10001`, 656 kB context | 6 s |
| Compose resolves | `docker compose config -q` (with `KELP_IMAGE`) | exit 0, no output | < 1 s |
| Missing tag | `docker compose config -q` without `KELP_IMAGE` | exit 1, `required variable KELP_IMAGE is missing a value` | < 1 s |
| Missing image | `KELP_IMAGE=kelp:nope docker compose up -d --no-build` | exit 1, `No such image`, no container created | < 1 s |
| Unit guard | the two `ExecStartPre` checks, run for `""` / missing / present tag | `KELP_IMAGE is unset` / `No such image` / exit 0 | — |
| Unit verifies | `systemd-analyze verify deploy/kelp.service` | no output, exit 0 | — |
| Boot to healthy | `docker compose up -d --no-build`, then poll `State.Health` | `healthy`, 0 restarts | 7 s |
| Liveness | `GET /api/healthz` | 200, `{"ok":true,...,"commit":"9b1c863"}` | 1.3 ms |
| Readiness without token | `GET /api/readyz` | 401 `unauthorized` | — |
| Readiness with token | `healthcheck.py --ready` | `provider=unavailable` (dummy upstream), all other checks ok | — |
| Read-only rootfs | `touch /app/nope` in container | `Read-only file system` | — |
| Non-root | `id` in container | `uid=10001(kelp) gid=10001(kelp)` | — |
| State layout | `ls -ln /data` | dirs `0700`, db/lock `0600`, `-wal`/`-shm` present while running, owner `kelp:kelp` | — |
| Single replica | second instance against the volume | exit `3`, `database_locked` | — |
| Restart persistence | stop + `up -d`, count rows | 2 threads / 2 turns before and after | — |
| Backup | tar of `/data` into a host backup dir | archive with numeric owners | 1 s |
| Restore | fresh volume + `tar --numeric-owner` | 2 threads / 2 turns, uid 10001 | < 1 s |
| Offline eval | `pytest tests/test_docs_answer*.py` | 68 passed | 0.74 s |
| Full suite | `python -m pytest` | 712 passed on the branch; 734 on `main` after the merge | 12.82 s / 15.02 s |
| Asset tests | `pytest tests/test_deploy_assets.py` | 40 passed | 0.12 s |
| Probe tests | `pytest tests/test_deploy_healthcheck.py` | 25 passed | 4.08 s |

Re-run on 30 October 2026 for the `--no-build` change: build, compose
resolution, both failure paths, the unit guard, boot-to-healthy, the container's
ids and read-only rootfs, the `/data` layout, and the second-replica exit code
were executed again against `kelp:9b1c863` and a disposable volume (removed
afterwards). The backup/restore rows were measured on the original pass; the
`/data` layout they depend on is unchanged.

Idle footprint: ~50 MiB RSS, ~2% CPU, no GPU, no swap.

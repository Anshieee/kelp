# Local supervisor channel (v1)

Status: Phase 1, stage 1. This is a dedicated Linux-only sidecar entrypoint,
not a default user entrypoint and not a desktop/UI path. Do not ship a normal
user build on top of it until the Local trust checks described below exist.

## Entrypoint

```sh
python -I -B -m chat.local_serve
```

There is no environment selector and no fallback that reads a normal desktop
configuration. `-I` deliberately removes the current directory from
`sys.path`, so the command expects the package to be importable from the
interpreter's installed environment (an installed wheel, or a test environment
whose site-packages names the checkout) rather than relying on `PYTHONPATH`.

The parent process must supply a connected `AF_UNIX` / `SOCK_STREAM` socket as
the child's fd 0.

The parent owns the other end of the socketpair. It must close the child end
after spawning and keep the paired end for the whole child lifecycle. The
parent must **never** parse the child's stdout or stderr for control or
readiness; the status frame is the only readiness signal.

## Bootstrap

Immediately before importing `chat.server`, the child:

1. verifies that fd 0 is a socket (`S_ISSOCK`);
2. verifies `SO_FAMILY == AF_UNIX` and `SO_TYPE == SOCK_STREAM`;
3. verifies the socket is connected (`getpeername()` succeeds);
4. duplicates fd 0 with `os.dup` and explicitly keeps the duplicate
   non-inheritable (`os.set_inheritable(fd, False)`);
5. replaces fd 0 with `/dev/null`, so a later accidental stdin read cannot
   consume protocol bytes or block a routine.

If bootstrap fails, the child writes only the fixed line
`kelp-local: invalid_channel` to stderr and exits non-zero.

## Framing

Every frame is:

```text
u32 big-endian unsigned N | N bytes of UTF-8 JSON
```

* Config frame: `N` in `1..65536` (`CONFIG_MAX_BYTES`).
* Status frame: `N` in `1..4096` (`STATUS_MAX_BYTES`).
* Exactly one config frame is read and at most one status frame is written.
* Reads use one absolute 30-second deadline for the whole config frame.
* Status writes use a short bounded deadline (2 seconds).
* Empty, oversized, truncated, timed-out, or EOF reads fail with a safe code.

The child treats every byte after the config frame, and EOF on the channel, as
a request to shut down. Before readiness those are protocol violations: the
child refuses to bind or announce readiness, writes at most one `bad_frame`
error (skipped when the peer is already gone), and exits `2`. After readiness
the same conditions are a clean shutdown with exit `0`.

Relevant bounds:

* the whole config frame read shares one 30-second absolute deadline;
* each status write has a 2-second absolute deadline;
* the watcher polls with a 0.1-second select and then blocks in `recv` for at
  most 0.1 seconds;
* watcher shutdown is joined for at most 2 seconds;
* the server loop observes a stop request on its 0.1-second poll interval.

## Config frame

```json
{
  "v": 1,
  "type": "config",
  "nonce": "0123456789abcdef0123456789abcdef",
  "secrets": {
    "KELP_AUTH_TOKEN": "...",
    "KELP_PROVIDER_API_KEY": "...",
    "KELP_CSRF_TOKEN": "...",
    "KELP_SESSION_SECRET": "..."
  }
}
```

Required keys: `v`, `type`, `nonce`, `secrets`.
Required secrets: `KELP_AUTH_TOKEN`, `KELP_PROVIDER_API_KEY`.
Optional secrets: `KELP_CSRF_TOKEN`, `KELP_SESSION_SECRET`.

Validation rules:

* duplicate JSON keys anywhere in the frame are rejected;
* unknown top-level keys and unknown secret names are rejected;
* `v` must be the integer `1`, not a boolean, float, or string;
* `type` must be `"config"`;
* `nonce` must be exactly 32 lowercase hexadecimal characters;
* each secret value must be a UTF-8 string of 1..1024 bytes;
* secret values containing Unicode `C*` control/format/private-use characters
  are rejected;
* invalid UTF-8, malformed JSON, and non-object roots are rejected.

No free-form error text crosses the channel. The only failure codes are fixed
constants: `bad_frame`, `bad_length`, `duplicate_key`, `inherited_secret`,
`internal_error`, `invalid_config`, `invalid_json`, `invalid_nonce`,
`invalid_secret`, `invalid_state_dir`, `invalid_utf8`, `missing_secret`,
`oversize_secret`, `startup_failed`, `timeout`, `unknown_key`,
`unknown_secret`, and `unsupported_inherited_config`.

## Status frames

Ready, after the application is built and the listener is bound:

```json
{
  "v": 1,
  "type": "ready",
  "nonce": "0123456789abcdef0123456789abcdef",
  "host": "127.0.0.1",
  "port": 43210
}
```

Failure, possibly before readiness:

```json
{
  "v": 1,
  "type": "error",
  "nonce": "0123456789abcdef0123456789abcdef",
  "code": "invalid_config"
}
```

`nonce` echoes the validated config nonce when it was parsed. It is `null` for
failures that happened before a valid nonce could be established, including
duplicate keys, invalid UTF-8/JSON, and invalid nonces.

If an unready startup failure occurs after a valid config frame, the child
emits one fixed error code. It never includes a traceback, environment value,
path, or secret in the frame.

Readiness and errors share one frame slot. A ready frame starts at most one
write; if that write cannot complete, the child does not attempt a second
frame. A listener whose actual bound host/port is not loopback/`1..65535` is
reported as `startup_failed` (a server fault), not `bad_frame`.

Exit status:

* `0` only after a complete ready frame was written and the server then shut
  down cleanly (EOF, unexpected byte, or `SIGTERM`/`SIGINT`);
* `1` for startup or readiness-delivery failure (`startup_failed`);
* `2` for a bad channel/config (`bad_frame`, `invalid_*`, `missing_secret`,
  `unsupported_inherited_config`, `inherited_secret`, and the other fixed
  validation codes), and for any refused pre-ready EOF/extra byte.

## Inherited process environment

The child reads the parent's environment but never mutates it. Only these
nonsecret values may cross from the inherited environment into the server
mapping:

* `KELP_PROVIDER_BASE_URL` (usually an unserved or explicitly selected
  loopback-compatible endpoint);
* `KELP_PRIMARY_MODEL`.

`KELP_LOCAL_STATE_DIR` is required in the inherited environment. It must be an
absolute path and must not be the filesystem root. All state paths are derived
from it:

```text
<state>/db/chat.db
<state>/artifacts
<state>/audit
<state>/usage
<state>/workspace
<state>/plugins
<state>/skill-store
```

These are forced regardless of any other inherited value:

```text
KELP_HOST=127.0.0.1
KELP_PORT=0
KELP_SCHEDULER_ENABLED=0
KELP_CREDENTIALS_ENABLED=0
```

The process refuses these inherited names instead of silently ignoring them:

* credential material: `KELP_AUTH_TOKEN`, `KELP_PROVIDER_API_KEY`,
  `KELP_CSRF_TOKEN`, `KELP_SESSION_SECRET`;
* bypass/worker configuration: `KELP_TENANT_SECRETS_FILE`,
  `KELP_CREDENTIALS_ENABLED`, `KELP_VAULT_KEY`,
  `KELP_BROWSER_WORKER_TOKEN`, `KELP_BROWSER_ALLOWED_ORIGINS`,
  `KELP_CREDENTIAL_SIGNING_KEY`;
* plugin pin bypass: `KELP_PLUGINS_ALLOW_UNPINNED` (the plural name read by
  `skills/loader.py` for `_load_plugins`).

Other inherited `KELP_*` names are not copied into the server mapping. In
particular, arbitrary database/plugin/host/port paths cannot override the
state-root derivation.

The composed mapping is passed to `ServerConfig.from_env` and `serve`; it is
not installed into `os.environ`.

## Application and HTTP surface

The child uses the existing hosted application and API. This stage does **not**
add local trust policy. Current behavior is therefore:

* `/api/health` remains owner-authenticated: a missing/wrong bearer token or
  session is `401`, and the configured frame `KELP_AUTH_TOKEN` is accepted.
* `/api/readyz` is not used as the readiness signal for this channel. The
  channel's own `ready` frame means the process built the app and bound the
  listener.
* No turn or provider request is made during readiness or health checks.

Not yet installed (Local trust phase):

* public `healthz`/static exclusions;
* local cookie policy;
* any desktop/browser trust boundary beyond the existing hosted auth surface.

Do not claim an L1 local-trust posture until those checks land.

## Plugin loading

Installed plugin discovery is pinned to `<state>/plugins` through the composed
`KELP_PLUGIN_ROOT`, and the store is `<state>/skill-store` through
`KELP_PLUGIN_STORE`. `build_app` passes its explicit environment mapping to
`SkillRegistry.load(..., environ=source)`, so a parent's unrelated plugin root
and pin bypass cannot leak into the local sidecar.

`SkillRegistry.reload()` (used after an approved plugin install) calls `load`
again without an environment mapping, so that latent path reads the real
`os.environ` for `KELP_PLUGINS_ALLOW_UNPINNED`; it keeps the registry's
already-resolved `plugin_root`. Because the local entrypoint refuses that
inherited name at startup, there is no supported way to set it for the reload
path. No code captures secrets or rewrites `os.environ` to close this gap.

The sidecar does not search outside the state root for installed plugins.

## Lifecycle and shutdown

* The application is built and `ThreadingHTTPServer` binds before the ready
  frame is written.
* The ready/error callback runs inside `serve`'s `try/finally`, so a callback
  failure still closes the listener, application, database, and lock.
* A watcher reads the channel after the config frame. Parent EOF or any
  unexpected byte requests cooperative shutdown. It reads through its own
  duplicate of the channel with an explicit short timeout, so the
  config-read and status-write deadlines cannot pin it.
* Immediately before readiness the child synchronously re-checks the channel
  (zero-timeout select), so a trailing byte that arrived with the config frame
  deterministically prevents the ready frame.
* Shutdown is requested through the server's normal event loop; the watcher
  never calls `shutdown()` before `serve_forever` can observe it.
* In Local mode, `SIGTERM`/`SIGINT` only record stop intent, without acquiring
  a lock or dispatching from the signal handler. The server loop consumes
  that intent and any channel stop once, then starts a helper thread for
  `shutdown()`. The hosted signal path is unchanged.
* Watcher joining and channel closing are bounded. Startup failures before the
  listener is bound send one fixed error code and never create state or bind a
  port.
* A later launch can reuse the same isolated state root: the database lock,
  listener, and watcher are released before exit (verified after both
  `SIGTERM` and channel EOF).

## Limitations in this stage

* Linux only.
* No credential worker or vault. Credential configuration is deliberately
  refused; the frame secrets are the only credentials.
* No UI, no default desktop entrypoint, and no normal user shipping path.
* No local static/healthz exclusions or cookie-policy work yet.
* Parent must implement framing and lifecycle exactly; stdout/stderr are not a
  control channel and may be discarded.

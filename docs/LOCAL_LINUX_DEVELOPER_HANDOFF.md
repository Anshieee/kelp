# Linux Local sidecar — developer handoff

**Date:** 2026-11-27. **Scope:** the non-default Linux developer fixture on
`feat/local-sidecar-lifecycle`, not a release or rollout. Runtime checks use
synthetic credentials, disposable state and loopback only, without starting
a live desktop. The precursor Python evidence at `d92a45d` is distinct from the
integrated desktop evidence in §7.

## 1. Scope
This records verified precursor evidence, the private fd 0 protocol, what the
Local guard proves, and a reproducible headless fixture for fresh verification
without a live desktop. The private Sway smoke test in §7 does not validate
the user's desktop, other compositors, or any remote/cloud contract.

## 2. Verified precursor evidence
From `~/Projects/kelp-research/PROGRESS-2026-11-24.md` and this committed state:

| Commit | Content | Paths |
| --- | --- | --- |
| `11b9745` | Guarded sidecar activation (chat-only profile) | `chat/local_serve.py`, `chat/server.py`, `desktop/src-tauri/src/local_supervisor/tests.rs`, `docs/LOCAL_SUPERVISOR.md`, `tests/test_local_sidecar.py` |
| `d92a45d` | Isolated Rust loopback transport + guard probe (test-only) | `desktop/src-tauri/src/local_transport.rs`, `…/local_transport/tests.rs`, `desktop/src-tauri/src/main.rs` |

Verified at `d92a45d`:
- **Python:** 1,827 passed, 4 skipped, 1 xfailed, with the opt-in wheel package gate (`tests/test_wheel_package_data.py`, `KELP_TEST_WHEEL`) enabled; Ruff passed.
- **Rust (offline, loopback-isolated):** 30 passed / 1 ignored, plus **1 separately exercised real installed-wheel test** — `installed_wheel_health_and_stop` in `desktop/src-tauri/src/local_supervisor/tests.rs`.
- **Hosted default:** `python -I -B -m chat.server --check` exits 0 with the required fake provider/auth/base-URL environment set.
- **Static:** `mypy==2.4.1` over `chat kernel openshell skills payments evals tools` — 106 files, no issues; Ruff clean; Gitleaks `f231a96..d92a45d` (11 commits) no leaks; `git diff --check` clean.

## 3. Private fd 0 protocol
Parent spawns the child with a connected `AF_UNIX`/`SOCK_STREAM` socket as **fd 0**. The child duplicates it (`os.dup`, non-inheritable), replaces fd 0 with `/dev/null`. The **parent** must never parse the child's stdout/stderr for control or readiness; the status frame is the only readiness signal. Framing: `u32` big-endian length prefix + UTF-8 JSON.
- **Config** (parent→child): `{v:1, type:"config", nonce, secrets}`. Required `KELP_AUTH_TOKEN`, `KELP_PROVIDER_API_KEY`; optional `KELP_CSRF_TOKEN`, `KELP_SESSION_SECRET`. Unknown/duplicate keys, bad nonce, Unicode `C*` control chars are rejected.
- **Status** (child→parent): success `ready {v:1, type:"ready", nonce, host, port}` after bind; failure `error {v:1, type:"error", nonce, code}` — the error frame also carries `v`/`type`/`nonce` (nonce `null` only if failure occurred before a valid nonce existed). One frame slot.
- **Exit:** `0` clean shutdown after ready (EOF/extra byte/`SIGTERM`/`SIGINT`); `1` startup/readiness failure; `2` bad channel/config or refused pre-ready EOF/extra byte.
- **State creation:** config validation and an already observed EOF/trailing
  byte can reject before state creation. `build_app` runs before bind, so
  later startup or readiness-delivery failures can leave disposable state.

Inherited nonsecret env is allowed (`KELP_PROVIDER_BASE_URL`, `KELP_PRIMARY_MODEL`); `KELP_LOCAL_STATE_DIR` is required (absolute, non-root). Credential and bypass names are **refused** if inherited (incl. `KELP_PLUGINS_ALLOW_UNPINNED`); `KELP_HOST=127.0.0.1`, `KELP_PORT=0`, `KELP_SCHEDULER_ENABLED=0`, `KELP_CREDENTIALS_ENABLED=0` are forced.

## 4. Local profile — no tools, no credential worker
`chat.local_serve::_run` is the only **production** caller of `chat.server.serve(..., local_only=True)`. Local mode builds `build_app(local_only=True)` with `registry=None`, `tools=()`, `TurnBudget(max_tool_calls=0)`: **no skill/plugin load, no tool registry, no scheduler, wallet, payments, credential broker, proactive runner, or `/api/readyz`.** Only chat + health REST/SSE targets remain after the L1 guard. The frame secrets are the only credentials; the provider transport uses `trust_env=False` (no ambient proxy/CA). The provider is OpenAI-compatible via inherited `KELP_PROVIDER_BASE_URL`; nothing contacts a real provider in the fixture.

## 5. What the Local guard tripwire proves / does not
L1 (`chat/local_http.py` / `chat/api.py` `make_server(local_only=True)`) rejects **before** body read, dispatch, or `100 Continue`: missing/wrong `Host` → `400 invalid_host`; missing/wrong `Origin` → `403 invalid_origin`; missing/wrong bearer → `401 unauthorized`. It also refuses `HTTPMessage.defects` partial parses and two-word HTTP/0.9. Local accepts only exact loopback Host/Origin singletons plus the bearer, on the listed chat and health targets, with no cookie fallback. This is a request-level boundary, not confinement or a production installer.

`LocalTransport::verify_guard` samples four bounded, read-only requests:
missing Origin and missing bearer each fail on `/api/healthz`; authenticated
`/api/health` succeeds; authenticated `/api/auth/devices` returns `404`.
It detects an older or incorrectly wired listener; it is not cryptographic
peer authentication or a substitute for the broader L1 tests.

## 6. Crash behavior and confinement boundary
`SIGTERM`/`SIGINT` only record stop intent (no lock acquisition or dispatch
inside the signal handler); the server loop consumes it. Startup failure
sends at most one fixed error frame. On graceful signal or channel EOF, the
listener, DB lock and watcher are released, so later launches can reuse the
state root. The Rust supervisor owns the child and bounds graceful waiting
before escalating and reaping.

The Rust transport uses a dedicated reqwest client (no cookie jar, proxy or
redirects; explicit Origin). Tests run in an isolated user/network/PID
namespace with loopback only. That test isolation is **not** shipped
confinement: state-root ancestors and the interpreter are trusted, there is
no OS sandbox, and the probe does not prevent a same-user process from
rebinding the port after the sidecar exits.

## 7. Integrated desktop verification
The non-default `local-dev` feature and explicit `KELP_DESKTOP_BACKEND=local`
are separate gates. The fixture takes `KELP_LOCAL_PYTHON` (absolute trusted
interpreter with the guarded wheel), `KELP_LOCAL_STATE_DIR` (disposable path,
created or required at mode `0700`), and `KELP_LOCAL_PROVIDER_BASE_URL`
(explicit `http://127.0.0.1:<port>/v1` for a test provider). `KELP_LOCAL_PRIMARY_MODEL`
is optional. Use trusted parent directories and do not point the fixture at
existing user data or a real local model service. The fixture uses a fixed
synthetic provider key, never a real key in an environment variable. An
explicit Local startup failure cannot select hosted.

**Window close:** an ordinary main-window close exits the whole Local app
(reusing the `RunEvent::Exit` sidecar teardown); Hosted keeps close-to-hide.

| Command or check | Observed result |
| --- | --- |
| `npm test` and `npm run build` (`desktop/`) | 140 passed, zero skipped; TypeScript/Vite build passed. |
| `cargo test --locked --offline`, with and without `--features local-dev` | 48 passed, 3 fixture tests ignored per configuration. Covers bounded SSE header wait without a whole-stream deadline, refusal of redirects, and ownership/cleanup of replaced Local subscriptions. |
| `installed_wheel_health_and_stop` and `installed_wheel_local_runtime_lifecycle` selected separately with `--ignored --exact` | Each passed against a guarded installed wheel. The latter submits a synthetic provider turn through the Rust Local transport, reads SSE text and replay, checks no tool schemas/events, and verifies child reap and port closure. |
| `unguarded_control_interpreter_is_refused_and_reaped` selected with `--ignored --exact` | Passed against an installed `8114aa0` wheel that completes the v1 handshake but lacks the Local HTTP guard. |
| `cargo build --locked --offline`, with and without `--features local-dev` | Both Linux binaries built; the feature build reported three dead-code warnings in `local_supervisor.rs`. |
| Compiled-binary probes, without a display or session bus | Feature disabled, missing interpreter, and older wheel each produced only its fixed stderr diagnostic and exit status `2`. No hosted fallback or window creation. |
| Private Sway GUI main-window Close | Local exited `0`, the owned Python process was gone, and its loopback port was closed. Hosted still hid the main window and kept the pet and app alive without starting a sidecar. |

The window check ran in a disposable headless Sway session, with a private
Wayland display, session bus, and loopback-only network. It is not validation
on the user's desktop or across Linux compositors. The two installed wheel
SHA-256 values used for these checks were
`3a9bf11763e890af323eabd324ad8a89bf111429b9d91148803079011e05fcee`
(guarded, `11b9745`) and
`04dd9b75b3ee3c7421c7ffde117c32b05dd4a4aabe68c132e94cf4830a96de5d`
(negative control, `8114aa0`). These identify the tested artifacts, not
required hashes for locally rebuilt wheels.

## 8. Reproducible headless fixture
Run from the integrated commit with Bash, `uv`, Node/npm, Rust/Cargo on
`PATH`, Tauri's Linux build dependencies, and `unshare`/`ip`. Dependency
acquisition may use public registries; the tests and process probes run
loopback-only. The Ubuntu/Debian prerequisites in `desktop/README.md` do
not enable unprivileged namespaces on systems that disable them. Use an
isolated Linux environment that permits `unshare -Urnp` rather than
weakening your desktop's policy. If using Rustup, put the installed
toolchain's `bin` directory on `PATH` rather than relying only on proxies
that look for `~/.rustup` under the disposable `HOME`.

```bash
set -euo pipefail
repo=$(git rev-parse --show-toplevel)
scratch=$(mktemp -d "${TMPDIR:-/tmp}/kelp-local-handoff-XXXXXX")
src="$scratch/src"
old_src="$scratch/old-src"
mkdir "$src" "$old_src" "$scratch/home" "$scratch/tmp" \
  "$scratch/wheels" "$scratch/old-wheels"
mkdir -m 700 "$scratch/displayless-runtime"
git -C "$repo" archive HEAD | tar -xf - -C "$src"
git -C "$repo" archive 8114aa0 | tar -xf - -C "$old_src"
uv --no-config build --wheel --out-dir "$scratch/wheels" "$src"
uv --no-config build --wheel --out-dir "$scratch/old-wheels" "$old_src"
wheel=$(find "$scratch/wheels" -maxdepth 1 -name '*.whl' -print)
old_wheel=$(find "$scratch/old-wheels" -maxdepth 1 -name '*.whl' -print)
test -f "$wheel" && test -f "$old_wheel"
uv --no-config venv --python 3.12 "$scratch/venv"
uv --no-config venv --python 3.12 "$scratch/old-venv"
py="$scratch/venv/bin/python"
old_py="$scratch/old-venv/bin/python"
uv --no-config pip install --python "$py" "$wheel" pytest==9.2.0
uv --no-config pip install --python "$old_py" "$old_wheel"
(cd "$src/desktop"; npm ci)
cargo fetch --locked --manifest-path "$src/desktop/src-tauri/Cargo.toml"

unshare -Urnp --fork --kill-child=SIGKILL --mount-proc bash -euc '
  scratch=$1 src=$2 wheel=$3 py=$4 old_py=$5 cargo_home=$6
  clean() {
    env -i PATH="$PATH" HOME="$scratch/home" TMPDIR="$scratch/tmp" \
      LANG=C.UTF-8 CARGO_HOME="$cargo_home" CARGO_TARGET_DIR="$scratch/target" "$@"
  }
  preflight() {
    local expected=$1 binary=$2 status=0
    shift 2
    clean timeout 15s env GDK_BACKEND=wayland \
      XDG_RUNTIME_DIR="$scratch/displayless-runtime" \
      DBUS_SESSION_BUS_ADDRESS="unix:path=$scratch/no-session-bus" \
      KELP_DESKTOP_BACKEND=local KELP_SERVER_URL=http://127.0.0.1:9 \
      "$@" "$binary" >"$scratch/refusal.out" 2>"$scratch/refusal.err" || status=$?
    test "$status" -eq 2
    test ! -s "$scratch/refusal.out"
    test "$(cat "$scratch/refusal.err")" = "kelp desktop: $expected"
  }
  ip link set lo up
  cd "$src"
  clean env KELP_TEST_WHEEL="$wheel" "$py" -B -m pytest
  cd "$scratch/home"
  clean env KELP_PROVIDER_BASE_URL=http://127.0.0.1:9/v1 \
    KELP_PROVIDER_API_KEY=synthetic-check-provider KELP_AUTH_TOKEN=synthetic-check-owner \
    "$py" -I -B -m chat.server --check
  cd "$src/desktop"
  clean npm test
  clean npm run build
  cd src-tauri
  clean cargo test --locked --offline
  clean cargo test --locked --offline --features local-dev
  clean env KELP_TEST_INSTALLED_PYTHON="$py" cargo test --locked --offline \
    --features local-dev local_supervisor::tests::installed_wheel_health_and_stop -- --ignored --exact
  clean env KELP_TEST_INSTALLED_PYTHON="$py" cargo test --locked --offline \
    --features local-dev local_runtime::tests::installed_wheel_local_runtime_lifecycle -- --ignored --exact
  clean env KELP_TEST_UNGUARDED_PYTHON="$old_py" cargo test --locked --offline \
    --features local-dev local_runtime::tests::unguarded_control_interpreter_is_refused_and_reaped -- --ignored --exact
  clean cargo build --locked --offline
  preflight "local backend requires a Linux build with the local-dev feature" "$scratch/target/debug/kelp-desktop"
  clean cargo build --locked --offline --features local-dev
  preflight "KELP_LOCAL_PYTHON must name an absolute interpreter" "$scratch/target/debug/kelp-desktop"
  preflight "local sidecar did not pass guard verification" "$scratch/target/debug/kelp-desktop" \
    KELP_LOCAL_PYTHON="$old_py" KELP_LOCAL_STATE_DIR="$scratch/tmp/negative-state" \
    KELP_LOCAL_PROVIDER_BASE_URL=http://127.0.0.1:9/v1
' sh "$scratch" "$src" "$wheel" "$py" "$old_py" "${CARGO_HOME:-$HOME/.cargo}"
```

`KELP_TEST_WHEEL` inspects packaged data; it does not replace source-tree
manifests. Both interpreter variables name their venv launchers, not an
editable checkout or a resolved symlink to a system Python. The older wheel
is only a negative control, never a supported Local runtime. Keep or remove
the scratch directory after collecting evidence.

## 9. Known limitations and next gates

1. **Linux release setup:** validate the real desktop separately, then package
   a pinned relocatable runtime and implement verified, remembered setup and
   Rust-side credential storage. The test fixture is not the user setup flow.
   Plan onboarding must not require a terminal, a config edit, or a pasted
   API key. Hosted remains the default.
2. **Confinement and desktop features:** OS confinement precedes network-capable
   skills, commands and silent installs. Screen release still needs real-app
   background-input and occluded-window capture evidence, including unchanged
   focus, pointer and workspace. These capabilities and approvals are not
   enabled by this slice.
3. **Other platforms and external dependencies:** macOS and Windows validation
   and signing (plus macOS notarisation) are deferred, not waived. Boat
   provisioning and paid-plan Boat compute still need the measured duration/
   cost pilot or an explicit waiver. Native phone still needs relay ownership
   and publisher/device-key coordination; see
   `docs/NATIVE_PHONE_FOUNDATION_STATUS.md`. No phone or Telegram feature ships
   with the fixture.

## 10. References
- `docs/LOCAL_SUPERVISOR.md` — channel/protocol and profile detail.
- `docs/NATIVE_PHONE_FOUNDATION_STATUS.md` — native-phone status.
- `~/Projects/kelp-research/PROGRESS-2026-11-24.md` — evidence source for §2.

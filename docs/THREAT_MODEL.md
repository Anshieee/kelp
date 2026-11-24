# Threat model — what the server process trusts, and what it does not (KEL-29)

- **Status:** written review, 2026-11-02
- **Branch:** `kel-29-threat-model`
- **Scope:** the server process started by `python -m chat.server` — `kernel/`, `skills/`, `openshell/`, and `chat/`
  and, as planned rules, the desktop companion (screen, voice, self-setup, local brain)
- **Companion sections:** added 2026-11-24, see [`docs/PRODUCT_BRIEF.md`](PRODUCT_BRIEF.md) and
  [ADR 0004](adr/0004-desktop-companion.md). Updated 2026-11-24 with the
  owner's answers 1 to 12 (brief §17, including the Q11 and Q12 answers): L4 to
  L7 in §6, S3 and S8 in §7 and T2 in §9. Updated again 2026-11-24 with the
  later answers 13 to 15 (brief §17): the background-control model in S8 (§7),
  the remote-channel rule in L7 (§6), and T4, T5, T7 and T8 in §9. Revised
  after review 1 (2026-11-24): the T8 allowlist grammar and the code gaps it
  needs filled, L3 before commands, the S8 and S4 target and classifier rules,
  where the permission mode lives (T7) and the phone key gate (L6)
- **Companions:** the recipient table ([`docs/PRIVACY.md`](PRIVACY.md) §2), the principal table and
  share-link threat model ([ADR 0002](adr/0002-sharing-and-identity.md) §4 and §10), the policy gate
  ([`docs/OPENSHELL.md`](OPENSHELL.md)), and the skill contract ([`docs/SKILL_SPEC.md`](SKILL_SPEC.md))
- **Enforced by:** `kernel/loop.py` (the turn loop), `skills/policy.py` (the invocation guard),
  `openshell/policy.py` (the policy engine), `openshell/audit.py` (the trail), and
  `tests/test_privacy.py` plus the kernel, policy, and audit test suites

This document answers a different question from `docs/PRIVACY.md`. The privacy review
asks **who receives data**; this one asks **which parts of the system are trusted to
enforce the rules, and what each boundary actually stops**. Every claim names the
file and function that enforces it, so a reader can check the claim against the code
instead of trusting the prose.

Sections 1 to 5 follow that rule. Sections 6 to 9 are planned rules: they name
the component that will enforce them, they are marked as unenforced, and no code
backs them yet.

---

## 1. Trust boundaries

Three boundaries carry the whole model. Each is named by the module and function
that enforces it.

### 1.1 Process boundary: the server process is trusted; the model provider is not

The server process (`chat/server.py::build_app`) holds the credentials, the
database, the audit trail, and the policy engine. Everything inside it is trusted
to enforce this document. The model provider is **not** trusted for privacy: it
receives the full transcript on every turn by design (`docs/PRIVACY.md` §2 and §6 —
"the provider is trusted with the transcript" is listed under what the review does
*not* promise). TLS protects the hop, not the recipient. Nothing in this repository
can answer a question without the provider seeing the conversation, so the provider
is treated as a known recipient, not as an adversary inside the trust boundary.

What the process *does* assume about the provider: it may fail (every failure is
converted to a typed `ProviderFailure` by `kernel/provider.py::classify_status` and
`ProviderClient.stream`, never an exception), it may return malformed
streaming chunks (also a typed failure, `provider_protocol_error`), and a
deployment that never names an endpoint fails typed (`missing_base_url`,
`missing_api_key`) rather than calling somewhere nobody named. It is never
trusted to have seen less than the whole request.

### 1.2 Loop-to-tool boundary: the loop never executes a call itself

`AgentLoop.run_turn` (`kernel/loop.py`) streams text, assembles `ToolCall` objects
from provider deltas, and hands each one to an **injected executor**
(`ToolExecutor` protocol; `RegistryToolExecutor` in production). The loop has no
skill names in it and no I/O of its own beyond the provider stream and the usage
store: it cannot read a file, open a socket, or run a command. If no executor is
configured, a call returns `no_tool_executor` (`AgentLoop._execute`).

The executor hands the call to `SkillRegistry.invoke` (`skills/loader.py`), which
is where the real gate lives. Every invocation builds an `InvocationPolicy` from
the loaded manifest (`skills/policy.py::InvocationPolicy.from_skill`) and checks
the concrete arguments against it **before the handler runs**
(`InvocationPolicy.evaluate_arguments`); a policy `deny` is terminal and never
becomes an approval request. While a handler runs, the process-wide socket guard
installed by `skills/policy.py::install_network_guard` (installed at import time)
refuses direct `socket` connects, datagram sends, and DNS lookups from inside an
invocation — a handler that ignores the chokepoint and reaches for `socket` or
`urllib` fails closed. The guard's known limits are stated in that module's
docstring: it covers the Python-level `socket` module and threads started or
submitted during the invocation, not a raw `_socket` object, a child process, or a
C extension calling `connect(2)` directly. Those are the deployment's boundaries,
not this module's.

### 1.3 Network boundary: one chokepoint per skill, one ledger row per call

A skill handler can only egress through `EgressClient`, obtained from
`GuardedSkillContext.egress()` (`skills/policy.py`) — the `context.egress()`
chokepoint. Before the transport is touched, `EgressClient.request` calls
`InvocationPolicy.check_url`, which enforces: http/https only, no embedded
credentials, ports 80/443 only, the host must match the manifest's declared
`hosts` allow-list (`InvocationPolicy.check_host`, backed by
`openshell/policy.py::host_allowed`), the deployment's `allowed_hosts` can narrow
but never widen the manifest, and an IP literal is refused even if it would match.
Redirects are refused outright (`_NoRedirect`), so a 302 cannot move egress
outside the allow-list.

Every outbound call — allowed, denied, or failed — writes one content-free
`egress` row through `EgressClient._ledger_record` into
`openshell/egress_ledger.py::EgressLedger`: recipient id, purpose token, byte
counts, method, host, outcome, and nothing else. A denied attempt writes a row
with zero response bytes, so a refusal is visible without pretending traffic
happened. The ledger's own invariants (closed recipient set, hostname-grammar
host field, no parameter that can carry content) are documented in
`docs/PRIVACY.md` §4.

The model client is a separate egress path with the same accounting:
`ProviderClient.stream` (`kernel/provider.py`) writes one ledger row per call
via `_record_egress`, resolving the recipient from the actual URL so an
operator-supplied `KELP_PROVIDER_BASE_URL` pointing anywhere other than the
documented `openrouter.ai` endpoint is recorded as `unlisted` rather than
silently attributed to `openrouter`.

---

## 2. What reaches the model

The full `ChatRequest` (`kernel/provider.py`, the frozen dataclass at the top of
the module) is:

| Field | Content |
|---|---|
| `model` | The model name (`KernelConfig.primary_model` by default) |
| `messages` | The transcript: user text, assistant text, and every tool result appended by the loop (`kernel/loop.py` appends `role: "tool"` rows after each call) |
| `tools` | The tool schemas derived from the skill manifests (`chat/server.py::_tool_specs`) |
| `temperature` | Sampling temperature (default `0.2`) |
| `max_tokens` | Per-step output cap, derived from `TurnBudget.max_tokens_per_step` and the remaining budget |

`ProviderClient.stream` serializes exactly these into the request payload
(`model`, `messages`, `stream`, `stream_options`, `temperature`, and — when
present — `max_tokens`, `tools`, and `tool_choice: "auto"`), so the provider sees
the whole conversation context every turn. Tool results are bounded before they
enter the transcript: `kernel/loop.py::_tool_content` truncates a result to
`MAX_TOOL_CONTENT_CHARS` (20,000 characters).

The consequence, stated in `docs/PRIVACY.md` §2 and §6 and repeated here because
it is the sharpest edge in the model: **excerpt text quoted by a document tool
travels inside the next model request.** `docs.answer` reads artifact bytes
locally and sends nothing itself, but the excerpts and citations it returns become
`role: "tool"` transcript rows, and the next `ChatRequest` carries them to the
provider. If a document must not leave the machine, it must not be attached.

---

## 3. What stays server-side

Each item names where the guarantee lives.

- **API keys.** Read from the environment at construction time
  (`kernel/config.py::KernelConfig.from_env`, `KELP_PROVIDER_API_KEY`). The `api_key`
  field is `repr=False`, and both `KernelConfig.__repr__` and
  `ProviderClient.__repr__` expose only `api_key_configured` — the key is
  never in an event, an exception, or a `repr` (`kernel/provider.py` module
  docstring states this as a design rule). The key is sent only in the
  `Authorization` header to its own recipient.
- **Skill secrets.** Handed to handlers only through `SkillContext`, and only the
  names the manifest declares: `SkillRegistry.invoke` builds the context from
  `skill.declared_secrets` (`skills/loader.py`). Every string written to the audit
  log passes `openshell/audit.py::SecretScrubber` — exact replacement of known
  secret values plus `Bearer …`, `sk_live_…`/`sk_test_…`, and `token=…`/
  `password: …` patterns — before serialization (`AuditLog.record` scrubs every
  field). The same scrubber source backs process logs and metrics labels
  (`chat/server.py::build_app` builds one `secret_values` tuple for
  `configure_logging`, `Metrics`, and `AuditLog`).
- **Approval contents.** The audit line keeps `argument_hash` — a SHA-256 over the
  canonical request — and never the raw arguments
  (`openshell/approvals.py::ApprovalStore.create` computes it;
  `chat/service.py::_request_skill_approval` passes it to `AuditLog.record`).
  The persisted approval card (`chat/store.py::save_approval`) carries the digest
  as `arguments_digest` and its `arguments` field only after
  `chat/cards.py::redact_arguments` has removed secret-keyed values and scrubbed
  known secret strings, with a 4 KB cap that truncates to `{"truncated": true}`
  rather than cut through a secret.
- **Artifact bytes.** Read locally by the document tools; only the excerpts a tool
  quotes back enter the model request (§2 above). Artifact bytes are never placed
  in an event, a ledger row, or an audit line.
- **Egress ledger and usage records.** Counts and fixed labels only.
  `EgressLedger.record` has no parameter that can hold a body, query, path,
  header, or prompt (`openshell/egress_ledger.py`). `UsageRecord`
  (`kernel/usage.py`) carries token counts, estimated cost, step and tool counts,
  duration, and an outcome token — no text.
- **The audit log.** Content-free by construction: `AuditLog.record`
  (`openshell/audit.py`) stores a timestamp, event name, ids, a scrubbed command
  list, `argument_hash`, and outcome metadata — never environment values,
  stdout/stderr bodies, or secret material. There is no rewrite or delete API;
  rotation is whole-segment (`chat/retention.py`).

- **Credential-handoff worker tokens (only with `KELP_CREDENTIALS_ENABLED=1`).**
  `KELP_BROWSER_WORKER_TOKEN` is accepted solely by `POST /api/credential-worker`
  (`chat/routers/credentials.py::credentials`). `get`, `claim` and `finish`
  require the HMAC-SHA256 token from `CredentialBroker.issue_worker_token`, signed
  with `KELP_CREDENTIAL_SIGNING_KEY` (32+ characters, distinct from the vault key,
  the worker token and the owner token, enforced by `credentials.validate_env`).
  `worker_token_scope` verifies the signature in constant time before parsing, then
  matches request ID, device, target origin, browser binding, session, operation
  and deadline against the stored row. A leaked scoped token lets its holder poll
  one request and, until the deadline, claim or finish it once; it cannot reach
  another request, owner routes or the vault. A compromised server or worker still
  sees plaintext (`docs/CREDENTIAL_HANDOFF.md`).

---

## 4. What the loop is allowed to do

`AgentLoop.run_turn` (`kernel/loop.py`) is an async generator with a short,
closed capability list:

1. **Stream text** — yield `TokenDelta` events as provider chunks arrive.
2. **Request tool calls** — assemble `ToolCall` objects from provider deltas and
   hand them to the injected executor (§1.2). It never executes a call itself.
3. **Enforce `TurnBudget`** — hard limits on model steps (`max_steps`, default 6),
   output tokens (`max_output_tokens`, 4096), tokens per step (1024), tool calls
   (`max_tool_calls`, 8), and wall-clock seconds (120, excluding approval wait
   time). Exceeding any ends the turn with a typed `turn_budget_exceeded` error.
4. **Emit typed events** — exactly the seven in `kernel/events.py`:
   `TurnStarted`, `TokenDelta`, `ToolCallStarted`, `ToolCallFinished`,
   `UsageUpdated`, `Error`, `TurnFinished`. `ToolCallStarted` carries the call's
   arguments as the model produced them; `ToolCallFinished` carries the result
   bounded by `_bounded` to the same 20,000-character cap. These go to local
   surfaces (dashboard, CLI); none of them is a network recipient.
5. **Write one `UsageRecord` per turn** — to the injected `UsageStore`, and
   nothing else.
6. **Honor `CancelToken`** — checked between provider chunks and between steps;
   cancellation is never an error (`reason = "cancelled"`).

What it is never allowed to do: **run a command.** No v1.0 skill manifest may
declare the `execute` effect — `skills/loader.py::_check_semantics` rejects it
outright ("the 'execute' effect is forbidden in v1.0"), so no loaded skill can
request it and `PolicyEngine._execute` (`openshell/policy.py`) denies shell
execution by default (`allow_exec=False`). `chat/cards.py::EXEC_AVAILABLE` is
`False`: `chat/server.py::build_app` never constructs `openshell.ExecGate`, and
`tests/test_privacy.py` fails if the flag and the wiring drift apart. The full
decision and its reversal procedure are in `docs/PRIVACY.md` §5.

---

## 5. What this model does not cover

- **The provider's own retention.** Out of scope, as `docs/PRIVACY.md` §6 states;
  nothing in this repository can see or bound it.
- **The socket guard's blind spots.** Named in `skills/policy.py`'s docstring and
  §1.2: a raw `_socket` object, a child process, a C extension calling
  `connect(2)`, or a pre-existing thread fed work through a queue. The guard is
  cooperative at the Python layer; the deployment boundary (no exec, container
  egress rules) is what backs it. The no-exec part of that boundary ends when
  commands ship (T8, planned). OS-level confinement (L3) must be in place
  before that.
- **A compromised process.** Everything in this document is enforced *by* the
  server process. An attacker with code execution inside it can read the
  environment, the database, and the audit trail directly. The boundaries above
  constrain what the *model* and *skills* can cause, not what a compromised
  process can do. In Local mode of the desktop companion the "process" is a
  sidecar running as the user on the user's own machine, so this boundary moves
  to that machine. The planned rules for it are in
  [§6 Local brain boundary](#6-local-brain-boundary-local-mode).
- **The share surface.** The first unauthenticated read path has its own threat
  model — token leakage, enumeration, revocation latency, cross-read — in
  [ADR 0002 §10](adr/0002-sharing-and-identity.md). This document does not repeat
  it; the two are consistent by construction (a share read makes no outbound call,
  so it adds no recipient and no egress row).

---

## 6. Local brain boundary (Local mode)

Status: planned. No code enforces this yet. Must land before the code it governs.

Sections 1 to 5 describe the server process on a host the operator controls. In
Local mode of the desktop companion ([ADR 0004](adr/0004-desktop-companion.md)
decision 2) the same brain runs as a sidecar on the user's own machine, and Boat
mode runs it in a sandbox the user owns. The rules below cover both. They will be
enforced in the desktop shell (the Rust bridge and the sidecar launcher), in the
sidecar's HTTP startup, and in an OS-level confinement layer that has not been
designed.

- **L1. Loopback only, bearer on every request.** The sidecar binds `127.0.0.1`
  only, on a random port chosen at each launch, requires the owner bearer on every
  request, and rejects a request whose Host or Origin is not the expected loopback
  value (ADR 0004 §2, item 7). Any local process, and any web page open in a
  browser, can reach a loopback port. The bearer keeps the brain private. The
  address does not. The checks below will be enforced in the sidecar's HTTP
  startup and in the desktop shell's sidecar launcher:
  - The Host header must equal `127.0.0.1:<port>` or `localhost:<port>` exactly.
    This also defeats DNS rebinding, where a hostile page makes its own name
    resolve to `127.0.0.1`.
  - There is no CORS wildcard.
  - The same checks apply to the SSE stream.
  - The bearer reaches the sidecar over stdin or an inherited file descriptor,
    never through argv or the environment, because other processes running as the
    same user can read both (on Linux through `/proc/<pid>/cmdline` and
    `/proc/<pid>/environ`).
- **L2. The bearer stays on the Rust side.** The owner bearer is kept by the Rust
  side (`bridge_sign_in`). The webview holds it only in memory while the sign-in
  dialog is open, and the app stores it nowhere (`desktop/README.md`). Any future
  at-rest storage goes on the Rust side as well. Today the app stores nothing at
  rest. Where local secrets live is decided in L5 below (planned, not built).
  That decision sets how private Local mode is, because any process that can
  read the chosen location can drive the brain.
- **L3. OS-level confinement before network-capable skills and commands.** The
  skill egress guard (`skills/policy.py`) is cooperative at the Python layer,
  and §5 relies on a deployment boundary behind it (no exec, container egress
  rules). A sidecar running as the user has neither. The no-exec assumption
  also ends when commands ship (T8, planned), so L3 must be in place before T8
  is enabled in Local mode. Before any network-capable skill or command runs in
  Local mode, the sidecar must run under OS-level confinement with three
  properties:
  - no process execution outside `openshell`,
  - outbound network limited to the hosts the skill manifests declare and the
    model providers the user configured, plus exactly three more: the push relay
    (L6), the Telegram Bot API host (`api.telegram.org`, L7) and, for plan
    users, the plan model gateway. Nothing else,
  - filesystem access limited to Kelp's data directory plus folders the user
    approved, and never the desktop app's device-key directory (L5, T7).

  The mechanism on each OS is open. This section does not pick one. Until
  confinement exists, Local mode does not enable network-capable skills or
  commands.
- **L4. Boat mode transport: an SSH port forward (planned).** The owner chose
  this on 2026-11-24 and asked for best practice. The Boat behaviour below was
  read from Boat's documentation (docs.boat.dev) on 2026-11-24 and can change.
  None of it is built. It needs new code in the desktop shell's Rust side.
  - The brain inside the sandbox binds `127.0.0.1` only, like the local sidecar
    (L1, ADR 0004 §2, item 7). There is no public port and no Boat public URL for
    the brain.
  - The desktop app reaches it through an SSH local port forward to the sandbox's
    loopback port. Boat documents this as `boat forward`, which binds `127.0.0.1`
    on the user's computer by default and makes nothing public
    ([Boat SSH access](https://docs.boat.dev/ssh-access.md)).
  - The forward's local end binds `127.0.0.1` explicitly, on an ephemeral port.
    L1's Host, Origin and bearer checks apply to the Boat brain too.
  - The desktop generates its own ed25519 SSH key per device, authorises it on
    the sandbox through the Boat API, and pins the host key from the Boat API
    response. Boat documents that the machine, IP address and host key change
    after each resume and fork. The app therefore refreshes the endpoint and
    host key from the Boat API after every resume or fork. It never uses
    trust-on-first-use, and it never pins the brain by IP address. If the Boat
    API response carries no host key, the app refuses to connect.
  - Removing a device in the desktop app deletes its key from the sandbox
    through the Boat API.
  - Where possible, the desktop's authorised key is restricted to forwarding the
    one loopback port, with the OpenSSH options
    `restrict,port-forwarding,permitopen="127.0.0.1:PORT"`. Whether Boat lets
    the app set `authorized_keys` options is unverified. It is an ADR 0004
    follow-up.
  - The owner bearer is still required on every request inside the tunnel. The
    tunnel is a transport and does not replace it. The Boat brain's bearer is
    persistent (L5).
  - Provider keys, the vault key and credentials stay inside the sandbox (ADR
    0004 §2, item 8).
  - **Why not the Boat public URL (`boat host`).** Boat terminates TLS for
    `on.boat.dev` and proxies to the sandbox
    ([how hosting works](https://docs.boat.dev/hosting/how-it-works.md)), so Boat
    would see the plaintext chat and the owner bearer. The default protection is
    a `_token` query parameter, so anyone with the full URL can open it
    ([hosting](https://docs.boat.dev/hosting.md)). Query strings end up in logs
    and history.
  - **Why not a mesh VPN (WireGuard or a hosted mesh).** It adds an account or
    key exchange step for every user, which breaks zero setup. It can be an
    advanced option later.
  - **The Boat API key.** The desktop needs a Boat API key to resume the sandbox
    and refresh the host key. The same key can authorise new SSH keys on the
    sandbox, so it is the highest-value secret on the desktop. It follows L5
    (OS credential store, Rust side) and never uses the plain-file fallback. It
    uses the narrowest Boat API key preset that can resume and read that
    sandbox. Whether Boat offers a per-sandbox key is unverified.
  - The phone app and the messaging bridge do not use this tunnel (L6, L7).
- **L5. Local secrets (planned).** The owner chose this on 2026-11-24 and asked
  for best practice. Enforced in the desktop shell's Rust side and the sidecar
  launcher. Every rule below needs new code there. None of it is built.
  - The Rust side of the desktop app generates all secrets on first run from the
    OS random source. The user never sees, types or copies one.
  - Persistent secrets are stored in the OS credential store, from Rust only:
    macOS Keychain, Windows Credential Manager, or Linux Secret Service. In
    Local mode they are `KELP_VAULT_KEY`, `KELP_CREDENTIAL_SIGNING_KEY`, the
    desktop app's device signing key (created at first run, used to sign a
    permission-mode change to "Approve for me", T7), and once L6 and L7 exist,
    the brain's per-brain relay credential, its long-term end-to-end private
    key, the paired phones' public keys and device records,
    and the Telegram bot token. The webview never reads them.
    This matches `desktop/README.md`: future at-rest storage belongs in
    `bridge_sign_in` on the Rust side.
  - The per-launch set is generated fresh on every launch, held in memory,
    passed to the sidecar over stdin or an inherited descriptor (L1), and never
    stored. It is the owner bearer (`KELP_AUTH_TOKEN`), `KELP_CSRF_TOKEN`,
    `KELP_SESSION_SECRET` and `KELP_BROWSER_WORKER_TOKEN` for the local brain.
    - **Session secret.** `KELP_SESSION_SECRET` (`.env.example:65`,
      `chat/api.py:180`) falls back to `KELP_AUTH_TOKEN` (`chat/server.py:813`),
      and device sessions persist across restarts (`.env.example:56`). Once the
      secret is per-launch, device sessions in Local mode are not expected to
      survive a restart. The desktop body signs in again automatically on each
      launch, with no user step.
    - **Worker token.** The browser worker reads `KELP_BROWSER_WORKER_TOKEN` from
      `os.environ` today (`chat/credential_browser.py:218`). Local mode needs new
      code so the worker gets it over stdin or an inherited descriptor, like the
      bearer (L1).
    - **Scope.** Only the desktop body talks to the local brain directly, so
      nothing else needs the bearer, the CSRF secret or the session secret. The
      worker token is the exception above, because the browser worker uses it.
    - A credential request left pending across a restart cannot be claimed with
      the old tokens and has to be requested again.
      [`docs/CREDENTIAL_HANDOFF.md`](CREDENTIAL_HANDOFF.md) describes that
      outcome for a rotated signing key or worker token.
  - **Lost credential-store entry.** The current code aborts startup when the
    vault key does not match an existing store: `chat/credentials.py:154-156`
    raises `ValueError('Vault key does not unlock the existing credential
    store.')`. Planned rule, which needs new code in the sidecar launcher: when
    the vault key is missing from the OS credential store but `credentials.db`
    exists, the launcher renames `credentials.db` aside with a timestamp (it is
    never deleted silently), generates a new key, and starts. Kelp then asks for
    each login again as new access (one card, T1). An encrypted export of the
    vault key can live in Settings for users who want it. It is never part of
    first run.
  - **Linux fallback.** On a machine with no Secret Service daemon (common on
    minimal Wayland compositors), a 0600 file stands in for the credential
    store. It covers only the vault key and the credential signing key. It is
    created with `O_EXCL` and mode 0600 inside `$XDG_DATA_HOME/kelp/secrets/`
    (mode 0700), a directory separate from the database directory, and Kelp's
    own backups exclude it.
    - The desktop device key (T7) never goes in that file, because the brain
      may read Kelp's data directory. On the fallback it gets its own 0600
      file in the desktop app's config directory
      (`$XDG_CONFIG_HOME/kelp-desktop/`, mode 0700), which only the Rust side
      reads and which L3 denies to the sidecar. Any other process running as
      the user can still read it, so on the fallback "Approve for me" is only
      as safe as a same-user file. It stays unavailable there until L3
      confinement exists. When a Secret Service later becomes available, Kelp
    moves these values into it on first use and then removes the file, so the
    lost-entry rule above never discards saved logins because of the move. Kelp
    shows a one-time notice in Settings, not a setup step. The fallback is
    weaker than a credential store.
    - The SSH private key, the Boat API key, the Boat brain's bearer, the
      Telegram bot token and the L6 relay credential, end-to-end key and device
      records never use the file fallback.
    - Kelp offers to set up a Secret Service through the resolver (a catalog
      entry, one card, T1). Until then Boat mode and the Telegram bridge are
      unavailable.
  - **Boat mode.** Provider keys, the vault key and credentials stay inside the
    sandbox (L4), and so do the brain's L6 relay credential, end-to-end key,
    device records and the Telegram bot token, which the desktop sends into the
    sandbox over the SSH channel when the user pairs a channel. The desktop
    stores at least three secrets in the OS credential store: its SSH private
    key, the Boat API key and the Boat brain's owner bearer. The bearer cannot be
    per-launch, because a Boat sandbox persists across resumes. To rotate it,
    the desktop writes a new bearer into the sandbox over the SSH channel and
    restarts the brain. A second desktop gets the bearer the same way: once its
    own SSH key is authorised (L4), it reads the bearer over the SSH channel, and
    a rotation from any desktop means the others read it again on their next
    connection.
- **L6. Push relay for remote approvals (planned).** The owner chose Ketock's
  native mobile app as the primary channel on 2026-11-24 (design in
  [brief §4.6](PRODUCT_BRIEF.md#46-remote-approvals-planned)). The relay is a
  hosted component, so it sits outside the brain's trust boundary. Every rule
  below needs new code in the brain, the desktop app, the phone app and the
  relay. None of it is built.
  - A relay is required because Apple and Google push services accept only pushes
    signed with the app publisher's credentials. A self-hosted brain cannot push
    to the store app directly.
  - The brain keeps an outbound connection to the relay. It works behind home
    NAT and opens no inbound port in Local mode or in Boat mode.
  - The push carries no content: only that a card is waiting, and an opaque id.
  - The app fetches the card details and sends the decision back through the
    relay. Both directions are end-to-end encrypted between the paired phone and
    the brain, and the decision is signed with a device key created at pairing.
    The relay sees only ciphertext and timing.
  - Pairing is one QR scan from the desktop app. The QR secret is short-lived and
    single-use, and the desktop shows it only after a local user action.
  - The relay is open source, so self-hosters can run their own.
  - The relay never receives screen frames or audio (S3).
  - **Unpair and revoke.** The user can unpair and revoke a phone from the
    desktop app. A lost phone's device key stops working at once.
  - **Decision binding.** Each decision is bound to the card id, the
    `argument_hash` and the expiry. The brain rejects replays and mismatches.
    Today's approvals enforce expiry and single use in `openshell/approvals.py`
    (600 s, `APPROVAL_TTL_SECONDS`) and the `argument_hash` match in
    `chat/service.py`. A signed remote decision over (card id, `argument_hash`,
    expiry) is new code.
  - **Decision key gate.** The phone's decision key is usable only after the
    device biometric or passcode. This is required at least for approving the
    start of screen control on the user's own desktop (S8). A phone that is
    stolen while unlocked remains a risk (S8).
  - **Relay authentication.** The relay authenticates the brain's outbound
    connection with a per-brain credential, so one brain cannot receive another
    brain's traffic. It is stored as in L5.
  - **Structured rendering.** The phone renders each card from structured fields
    (action type, target, expiry, as in T4), never from model text.
  - **Failure mode.** If a relay message is dropped or arrives late, the card
    expires and the action does not run.
  - The remote-channel rule in L7 applies to the phone too.
- **L7. Telegram bridge (planned).** The secondary channel. The bridge is a bot
  that the brain reaches by long polling, which is outbound only. Every rule
  below needs new code in the brain. None of it is built.
  - Telegram bot messages are not end-to-end encrypted, so Telegram receives the
    plaintext of every bridge message: the user's turns and Kelp's replies. A
    Telegram card therefore shows only the action type and the target. Full
    details stay in the desktop or phone app.
  - **What never goes over the bridge.** Content read through the credential
    flow, content from blocked or private apps (the S4 and V4 rules), screen
    frames and audio, and the details of T1 new-access cards.
  - **What Telegram may approve.** A card is approvable from Telegram only when
    the fields Telegram shows fully determine the action, and the card is not in
    a T4 category (money, anything another person sees, irreversible acts, terms
    of service) and is not new access (T1). It is also never a screen-control
    card (S8). Every other card shows "open in the app" in Telegram. Whether to
    widen this is a follow-up.
  - **Inbound messages.** The bridge accepts messages only from the paired
    Telegram user id and refuses group chats. Pairing uses a one-time code that
    the desktop app shows. The bot token is a persistent secret, stored per L5
    and never in the file fallback.
  - **Remote-channel rule (applies to L6 and L7).** A message from the bridge is
    a user turn. Remote channels (Telegram and the phone) can never create a
    standing approval, can never approve a T1 new-access card, and can never
    switch the permission mode to "Approve for me" (T7). Telegram can never
    start or approve screen control (S8). The paired phone may approve starting
    control, on the user's own desktop or on a Boat sandbox desktop, with a
    signed decision (L6). Changed 2026-11-24: the earlier text let only the
    desktop app approve control of the user's own desktop, because the user had
    to be able to take over. Kelp now never takes over (S8).
  - Like L6, it opens no inbound port and does not use the Boat tunnel (L4).

| Open decision | Where it is tracked |
|---|---|
| Confinement mechanism per OS | Open, no owner decision yet |
| Narrowest Boat API key that can resume and read one sandbox (L4) | [ADR 0004](adr/0004-desktop-companion.md) follow-ups |
| Whether Boat lets the app set `authorized_keys` options on the sandbox (L4) | [ADR 0004](adr/0004-desktop-companion.md) follow-ups |
| Push relay design: pairing, key exchange, per-brain credential, hosting (L6) | [ADR 0004](adr/0004-desktop-companion.md) follow-ups |
| Whether to widen which cards may be approved from Telegram (L7) | [ADR 0004](adr/0004-desktop-companion.md) follow-ups |
| Background-input spike on each OS: tier 1 drives an unfocused window without changing the user's pointer, keyboard focus, active window, z-order, workspace or clipboard, the action reads back as landed, and per-window capture of an occluded window works. Tier 2 nested-compositor test on wlroots. A global stop shortcut on each OS (S8). Screen control does not ship on an OS where tier 1 and tier 2 both fail the spike, and Kelp offers Boat there | [ADR 0004](adr/0004-desktop-companion.md) follow-ups (background-input spike) |
| How Kelp detects that the user focused or typed in the window it is acting in, on each OS (S8, yield rule) | [ADR 0004](adr/0004-desktop-companion.md) follow-ups (background-input spike) |
| Update [`docs/PRIVACY.md`](PRIVACY.md) §2 and the telemetry and recipient statements here (§1.1, §10) before a hosted plan or the Telegram bridge ships (S3, L7) | [ADR 0004](adr/0004-desktop-companion.md) follow-ups |

Decided on 2026-11-24 and no longer open: where the owner bearer and vault key
live (L5, brief §17 question 4) and how the app reaches a Boat brain (L4, brief
§17 question 3).

---

## 7. Screen

Status: planned. No code enforces this yet. Must land before the code it governs.

Screen frames come from the desktop capture module, which is part of the local
body (ADR 0004 §2, item 1). The rules below will be enforced there, before a
frame is taken, and never delegated to the OS prompt. The capture spike
([`docs/spikes/2026-11-screen-capture.md`](spikes/2026-11-screen-capture.md)) found
that on a wlroots compositor (MangoWC) any process can capture the screen with no
prompt, so Kelp's own code is the only consent gate there. GNOME, KDE, macOS and
Windows were not tested.

- **S1. Watch scope.** First run shows one screen. It lists app categories with
  suggested defaults already ticked (editor, terminal, most of the browser) next
  to the fixed block list (S4). The user changes the list in that one place, once.
  Kelp captures only apps on the allowed list, and S4 scopes each capture to the
  focused window. This stays opt-in per app, chosen on a single screen.
- **S2. Retention.** Frames are never written to disk. They live in memory only.
  The capture module must not use file-producing APIs, or must delete the file
  immediately and never write it to a shared directory. The reason is measured:
  on the wlroots test machine the xdg-desktop-portal Screenshot call left
  `/tmp/out.png` with mode 0644, readable by every user on the machine. Kelp may
  keep short text observations (for example "tests failing in VS Code") for at
  most 24 hours. Anything kept longer enters memory only after the user okays it.
  Observations are stored as untrusted data and are never promoted to memory or to
  a rule without the user's confirmation. The 24 hour expiry will be enforced in
  the brain's store, not in the capture module.
- **S3. Where frames go.** The only Kelp-run server that may receive screen
  frames is the plan model gateway ([product brief](PRODUCT_BRIEF.md) §15),
  for plan users (owner decision 2026-11-24, owner answer 11). No Kelp-run
  server receives audio. The push relay (L6) never receives frames or audio and
  sees only ciphertext and timing. The Telegram bridge (L7), when the user turns
  it on, receives the plaintext of bridge messages but never frames or audio.
  The plan gateway receives conversation text, because it forwards the user's
  turns to a model, downscaled frames for apps marked "may send to cloud",
  reaction metadata (app id and window title) and text observations. The "may
  send to cloud" tick is off for every app by default and is never part of the
  suggested defaults in S1. The gateway forwards each frame to the model and
  keeps nothing itself: no frame is written to disk, a log, a queue that
  outlives the request, or a training set. A self-hoster's own gateway follows
  the same rule. The gateway's upstream provider keeps whatever its terms
  allow. Before frames are enabled for plan users, the project names that
  provider and its retention terms in [`docs/PRIVACY.md`](PRIVACY.md) and on
  the plan page. The gateway enforces its own per-account rate cap and maximum
  frame size, and rejects anything larger. "Cloud" for frames means the
  plan gateway, a provider the user configured with their own key, or the user's
  own Boat VM. What leaves the machine depends on the run mode:

  | Run mode | What leaves the machine | Rule |
  |---|---|---|
  | Default reaction signals, any mode | Metadata only: the focused app id (11 ms to read on wlroots in the spike), idle time, and the window title, read only after the S4 check passes and never for blocked or private windows | No pixels leave the machine by default. |
  | Local mode, local vision model | Nothing. Frames stay on the machine. | Used only if a hardware check passes. The spikes did not measure a local vision model, so it is unproven until that check exists. |
  | Local mode, cloud provider | A downscaled frame, to the provider the user configured | Only for apps the user marked "may send to cloud". That is a separate tick from "may watch". Never for blocked apps. Rate-limited. The indicator shows the destination (for example "watching: sent to <provider>"). Provider retention is out of scope (§5), and the setting says so plainly. The brain enforces the rate limit, because it makes the model call. The desktop app holds the per-app ticks and draws the indicator. |
  | Plan user, plan gateway | A downscaled frame, to the plan gateway, which forwards it to the plan's model | The same "may send to cloud" tick, block list and rate limit as the row above. The indicator names the destination ("watching: sent to Kelp plan"). The gateway stores no frames. |
  | Boat mode | When the user has enabled frame sending ([product brief](PRODUCT_BRIEF.md) §5), frames go to the user's own VM, on demand only, over the SSH port forward, with the owner bearer (L4) | The VM counts as the user's machine for retention, and the same no-disk rule (S2) applies inside the VM. If the VM's model is a third-party provider or the plan gateway, the "may send to cloud" tick applies. The indicator names the final destination. |
- **S4. Block list.** Two tiers, checked against the focused app before any frame
  is taken:
  - Always blocked, cannot be lifted: password managers, private or incognito
    browser windows, Kelp's own credential entry screen
    ([`docs/CREDENTIAL_HANDOFF.md`](CREDENTIAL_HANDOFF.md) already refuses
    screenshots during private entry), and 2FA or authenticator apps.
  - Also always blocked, cannot be lifted (planned for screen control, S8): OS
    permission prompts, authentication prompts (polkit, Keychain, UAC, Windows
    Hello) and system settings panes. Kelp never clicks "Allow" in them.
    Granting an OS permission stays a user act (T1).
  - Blocked by default, unlockable one site at a time: banking and payment sites.
    The unlock happens only in Settings, never from a setup card and never from
    chat.

  When a blocked app has focus, capture stops before the frame is taken. The
  focused-app check runs first. Reading the focused app is compositor-specific
  (the spike used MangoWC's IPC); other compositors need
  `wlr-foreign-toplevel-management` or their own mechanism.

  Capture scope and failure mode, for both tiers:
  - A frame covers only the focused allowed window. A full-screen frame would
    include every visible window, notifications and other monitors. If a blocked
    window is visible, the frame is dropped. The spike captured the full screen,
    so window-scoped capture is untested. (A frame for screen control comes from
    the window Kelp is acting in instead, see S8.)
  - The focus check is repeated after the frame is taken. If the focus changed,
    the frame is discarded.
  - If the focused app, window or site cannot be determined, no frame is taken.
    The detection method for private windows and per-site blocks is open.
  - **Text read from an app counts as frame content.** For S1 to S4, text read
    from an accessibility tree, and text read back after an action (S8), is
    treated like a frame. The watch list, the "may send to cloud" tick and the
    block list decide whether it may be read and sent. The body never
    enumerates or reads the accessibility tree or the window list of a blocked
    app (planned).
- **S5. Auto-pause during sharing and calls.** Capture pauses while the screen is
  being shared or a call is active. The same condition mutes speech (V5). The
  detection method on each OS is open.
- **S6. Screen text is data.** Text read from the screen can never approve an
  action and can never start a setup step ([product brief](PRODUCT_BRIEF.md)
  §3). Window titles are untrusted text, the same as screen text. The reaction
  loop that watches the screen has no tool access (ADR 0004 §2, item 5). If Kelp
  sees text that looks like instructions aimed at it, it tells the user once per
  page or window ("this page tried to give me instructions; I ignored them") and
  does nothing else.
- **S7. Indicator.** Kelp draws its own watching and listening indicator. On
  wlroots the OS shows nothing during capture (spike).
- **S8. Screen control (planned, not built).** Screen control means Kelp acting
  in apps on a desktop, with its own pointer and keyboard input: the user's own
  desktop, or a Boat sandbox desktop. The owner made it a core capability in
  every mode on 2026-11-24. The same day the owner replaced the shared-input
  model: Kelp never takes over. It has input devices of its own and works in
  apps that are not focused, so the user keeps working at the same time (brief
  §17, answer 13). No input was sent in any spike (brief §12).
  - **Kelp never uses the user's input seat.** Kelp never moves the user's
    pointer, never types on the user's keyboard focus, never raises or focuses a
    window and never switches the user's workspace. Any mechanism that injects
    into the user's own input seat is never used on the user's desktop: uinput
    or `ydotool`, `zwlr_virtual_pointer_v1` and `zwp_virtual_keyboard_v1` on the
    user's seat, Windows `SendInput`, and macOS HID-level `CGEventPost`. If a
    task cannot be done without one of them, Kelp does not do it on the user's
    desktop. It offers tier 2 or Boat, or asks the user.
  - **Tier 1: act on the target window directly, in the background.** Kelp
    addresses every action to an approved window or UI element handle, never to
    whatever has focus. The planned mechanisms are all untested: macOS
    Accessibility (`AXUIElement` actions such as AXPress, and setting AXValue)
    on an unfocused app; Windows UI Automation patterns (Invoke, Value,
    Selection) and, for native controls, window messages; Linux AT-SPI (the
    Action, EditableText and Value interfaces). Kelp reads the result back from
    the same API and reports done, failed or unknown. It does not assume
    success. Tier 1 calls that can steal focus are not used on the user's
    desktop: `AXRaise` on macOS, UI Automation `SetFocus` on Windows and AT-SPI
    `grab_focus` on Linux. Launching an app that is not running is not done on
    the user's desktop either, because the launch can take focus. Kelp uses
    tier 2 for it. Precedents: OpenAI's Computer Use runs "a scoped task in the
    background while you keep working elsewhere" on macOS, but on Windows it
    "can't operate in the background while you keep using the same Windows
    session" and suggests a VM
    ([OpenAI](https://developers.openai.com/codex/app/computer-use)). A Windows
    developer reported the order window messages, then UI Automation, then
    refuse, and never the real cursor
    ([dev.to](https://dev.to/keyaburi/i-wanted-to-game-while-my-ai-agent-ran-tests-so-i-built-it-a-cursor-of-its-own-m22)).
  - **Tier 2: Kelp's own workspace.** For apps tier 1 cannot drive (canvas apps,
    games, apps with no accessibility tree), Kelp opens the app in a workspace
    with its own input seat. Its pointer and keyboard are real, but separate
    from the user's. All of it is untested.
    - Linux: a nested headless wlroots compositor run by the body (headless
      sway or a similar compositor; sway is installed on the owner's machine).
      Kelp uses the virtual-pointer and virtual-keyboard protocols on that
      nested compositor only.
    - Windows: a VM or the Boat sandbox. A second Windows desktop object is
      untested.
    - macOS: tier 1 first. Tier 2 is open.
    - Boat: the sandbox desktop is already Kelp's own workspace.

    The user can watch the workspace in a pane without it taking focus. The
    existing view-only Computer pane (`desktop/src/ComputerPane.tsx`) is a
    starting point. Apps in the workspace are separate instances. A browser
    profile that the user has open cannot be opened twice, so the workspace uses
    Kelp's own browser profile, and logins go through the credential handoff
    ([`docs/CREDENTIAL_HANDOFF.md`](CREDENTIAL_HANDOFF.md)). S4 blocked apps are
    not opened or acted in there either.
  - **Kelp's pointer is visible.** On the user's desktop Kelp draws its own
    click-through cursor and a `Kelp is working in <app>` label over the window
    it acts in, as an overlay (for example `wlr-layer-shell` on wlroots), so the
    user always sees where it works. A Stop button on the label and a global
    stop shortcut end the task. The stop shortcut on each OS is open. Screen
    control does not ship on an OS where Kelp cannot offer this stop control.
  - **Yield, do not fight.** If the user focuses or types in the window Kelp is
    acting in, Kelp pauses its actions in that window until the user leaves it,
    then resumes. It never takes the window back. How Kelp detects this on each
    OS is open.
  - **Started only by an approved task.** Control starts only inside a user
    turn, after a card rendered by the desktop app from structured fields:
    target app id or site origin, a step cap, and an expiry. It is never
    rendered from model-written text. The approval is a new single-use kind
    bound to the hash of those fields. Each action is checked against that hash.
    A standing approval can never cover screen control. The card for starting a
    task is shown in both permission modes (T7), as this section already
    required before the modes existed. In "Ask before doing anything" the
    approval for screen control is that start card plus a card at each T4 step.
    It is not a card per click. Every T4 category still gets its own card at its
    step.
  - **Who may approve starting control.** The desktop app, or the paired phone
    app with a signed decision (L6). On the phone the decision key must be gated
    by the device biometric or passcode (L6). A phone that is stolen while
    unlocked can still approve starting control. That risk remains. The step
    cap, the target binding and the T4 step cards limit what the approval can
    do. Telegram never. A scheduled watch can never start control. This is a
    change made on 2026-11-24. The earlier rule let only the desktop app
    approve control of the user's own desktop, because the user had to be able
    to take over. That reason no longer holds, because Kelp
    never takes over.
  - **Boat sandbox desktop.** The target-window checks, the yield rule and the
    S1 to S5 app lists below apply to the user's own desktop only. For a sandbox
    desktop the card is rendered from the same structured fields and approved
    as above. The brain in the sandbox enforces the target-site, T4 step and
    terminal checks, and the "Kelp is working" label names the sandbox. S4
    blocks and S1 to S5 still apply to any of the user's own windows Kelp is
    shown, never to the sandbox screen.
  - **Enforced in the desktop app (user's own desktop).** The body applies S4,
    S7 and S8 to every action, whatever brain sent it, including a Boat brain.
    Every action is addressed to the approved target window. Before each action
    the body re-reads that window's app, title and, for browsers, origin. If
    they cannot be determined, or do not match the card or the task, no action
    is sent.
  - **Blocked apps are never acted in.** S4 blocked apps are checked by target,
    not by focus. Kelp stops if the target becomes a blocked app or window or a
    private browser window. A window or dialog spawned by an action (a popup, a
    file picker, an OS prompt) is not the approved target. Kelp sends it no
    action unless a card names it, and an OS permission or authentication prompt
    is always blocked (S4). Passwords and one-time codes are never typed from
    model output. In a browser, a login goes through the credential handoff
    ([`docs/CREDENTIAL_HANDOFF.md`](CREDENTIAL_HANDOFF.md)), which is
    origin-bound and needs a fresh owner entry each time. In a native app Kelp
    stops and asks the user to enter it. While private entry is active Kelp
    sends no action to that page.
  - **Frames come from the target window (user's own desktop).** Frames for
    control are captured from the target window, not the focused one. The
    planned per-window capture is ScreenCaptureKit on macOS, Windows Graphics
    Capture on Windows, and
    `ext_foreign_toplevel_image_capture_source_manager_v1` with
    `ext_image_copy_capture_manager_v1` on wlroots, which the compositor on the
    owner's machine advertises (brief §12). Capture of an occluded window is
    untested. S1 to S5 still decide which apps may be watched and sent to the
    cloud: the control loop sees only apps on the allowed list and sends frames
    to a cloud model only for apps ticked "may send to cloud". A task for any
    other app uses a local vision model or does not run. Control pauses while S5
    is active. Text read from the accessibility tree or read back after an
    action counts as frame content for these rules (S4). The body never
    enumerates or reads a blocked app's tree or windows.
  - **Screen text cannot widen the task.** The control loop reads the screen
    (S6), so what it reads is untrusted. It may choose clicks and keystrokes
    within the approved task and target app or site. A step that would leave
    that app or site, or start something the card did not name, stops and asks.
  - **Each T4 step gets a card.** Each step in any T4 category gets its own
    card at that step, in both permission modes. The desktop app classifies the
    step, and the model does not. A step it cannot classify is treated as T4
    and shows a card. The classifier combines the element's role, its structure
    and its label:
    - Labels and other page- or app-supplied text are untrusted input. A label
      can add a T4 signal (Send, Post, Submit, Pay, Buy, Delete, Accept, Enter
      in a form). A label can never remove one.
    - Structure rules: a submit-type element, a button that submits a form, and
      any element that triggers a payment, a file upload, or navigation to a
      different origin are T4.
    - In "Approve for me" in a browser, routine means scroll, read, focus within
      the target, typing into non-password fields, and links within the approved
      origin. Anything else is unknown, and unknown is T4.
    - Residual risk: a malicious site that the user approved as the target can
      still disguise a consequential control. Origin binding and the T4 money
      and submission rules limit this. They do not remove it.
  - **Typing into a terminal is command execution.** Input to a terminal
    emulator, shell window, IDE terminal, remote-shell session, REPL, `sudo`
    prompt, or any field that launches a program or runs a command (Run dialog,
    launcher, command palette, task runner) is treated as running a command. The
    desktop app classifies the target as it does in the T4 step rule, and a
    text target it cannot classify is treated as one of these. The rule applies
    unchanged in a Boat sandbox desktop. Commands are allowed behind approvals
    (planned, owner decision 2026-11-24, brief §17 answer 14). Keystrokes into
    any of these targets always show a card with the exact text Kelp is about
    to send, in both modes, rendered from the keystrokes and never from model
    prose. The T8 allowlist does not apply to keystrokes: a keystroke stream
    cannot be checked against an argument grammar, so only commands run through
    `openshell/exec_gate.py` can skip a card. The exec boundary in
    [`docs/PRIVACY.md`](PRIVACY.md) §5 stays in place until the wiring and the
    extra work listed there and in T8 land.
  - **Ships only after the spike.** Screen control does not ship on an OS where
    tier 1 and tier 2 both fail the background-input spike (brief §12, and the
    open decision table in §6). The spike also checks that the user's active
    window, z-order, workspace and clipboard did not change. Kelp then offers
    Boat.
  - **Logged without frames.** Each control step writes an audit row with the
    approval id, step index, step type (click, key, scroll, text), target app
    id and an `argument_hash`. It holds no task text, frame or typed text.
  - In Boat mode, long or risky tasks run in the sandbox's own desktop by
    default, so they do not touch the user's desktop (brief §5).

Prior art: Windows Recall filters apps and private browsing
([Microsoft support](https://support.microsoft.com/en-us/windows/ai/ai-features/filtering-apps-websites-and-sensitive-information-in-recall));
screenpipe records continuously with an exclusion list
([screenpipe](https://github.com/screenpipe/screenpipe)). Kelp differs: it
stores no frames.

---

## 8. Voice

Status: planned. No code enforces this yet. Must land before the code it governs.

Microphone capture, speech-to-text and text-to-speech run in the local body, in
the desktop app. The rules below will be enforced there. The voice spike
([`docs/spikes/2026-11-voice-latency.md`](spikes/2026-11-voice-latency.md))
measured the default pipeline and left speech-to-speech untested.

- **V1. Push-to-talk by default.** An optional wake word ("Hey Kelp") is detected
  by an on-device model. Audio from before the wake word, or from outside
  push-to-talk, never leaves the machine and is not kept.
- **V2. Audio is deleted once transcribed.** Only the text is kept, the same as a
  chat message. The desktop app's voice module holds the audio and deletes it. The
  brain only ever receives the text.
- **V3. Speech-to-speech.** What leaves the machine depends on the run mode:

  | Run mode or option | What leaves the machine | Rule |
  |---|---|---|
  | Default pipeline, every mode | Text only, and only to wherever the brain's model runs | Local speech-to-text, text to the brain, text back, local text-to-speech. Measured: 3.1 to 3.3 s from end of speech to first audio, with a cloud text model on a CPU-only laptop (voice spike). This excludes endpointing and audio device latency. |
  | Speech-to-speech realtime API | Raw audio, to a third party | Off by default. Available only as an explicit "fast voice" opt-in with the user's own provider key. The destination is named on the setting and on the listening indicator. Never used for wake-word listening. |
  | Boat mode | Text only, to the VM | Speech-to-text still runs on the user's machine, because of latency. |

  A self-hoster needs no speech-to-speech provider. The default pipeline works
  with local models or any text model.
- **V4. Blocked and private content is not read aloud.** Content from a blocked
  or private app (S4), and anything read through the credential flow, is shown as
  text and never spoken. S4 stops capture of such content, so this covers content
  that reaches Kelp another way, such as a tool result or text the user pastes.
- **V5. Speech is muted during calls and screen sharing.** The condition is S5.
  The one-key quiet mode ([product brief](PRODUCT_BRIEF.md) §3) mutes it too.

---

## 9. Self-setup (the capability resolver)

Status: planned. No code enforces this yet. Must land before the code it governs.

The resolver is described in [product brief](PRODUCT_BRIEF.md) §4 and
[ADR 0004](adr/0004-desktop-companion.md) §2, item 4. It does not exist yet. The
rules below will be enforced in the resolver, in the approval gate
(`openshell/`), and in the desktop app, which renders the confirmations. T7 and
T8 below also cover the permission modes and commands, which use the same
approval gate.

- **T1. What installs without a card.** Silent install applies only to catalog
  entries whose pinned catalog metadata sets `grants_new_access: false`. Because
  the flag is part of the pinned entry, the model cannot change it. Silent
  install is capped by size and count per turn, and every silent install is shown
  afterwards in an Installed list the user can open. An install over a cap needs a
  card. The default caps are open, and the user can set them. Large downloads
  under the caps show progress and no card. A card appears whenever access grows:
  a new account or login, a new folder, a new network host, a new OS permission,
  or spending money. This refines the single approval card in brief §4.2: the
  card is shown when the plan grants new access, and the plan is still logged.
  Further conditions:
  - Silent install is disabled in a turn whose context includes screen or web
    text, until the user confirms.
  - In "Ask before doing anything" (T7), silent install is off: every install
    shows a card.
  - In Local mode, silent install is off until the OS confinement in L3 exists.
    Until then every install shows a card. In Boat mode silent installs run inside
    the sandbox.
- **T2. Sources.** Installs come only from the pinned catalog (digest pinning in
  `skills/loader.py`). Kelp's own release-pinned entries ship first (Phase 2
  voice models). Publisher signing is a spike (`skills/signing.py`) and is wired
  in before the catalog accepts any entry from outside Kelp's own release, and
  before the full resolver ships in Phase 2 (owner decision, 2026-11-24).
  Kelp may propose a new catalog entry. It cannot install an arbitrary package,
  and it never asks the user to paste a command into a terminal. Prior art: a Koi
  Security audit of 2,857 OpenClaw skills, reported by NeuralTrust, found 341
  malicious ([NeuralTrust](https://neuraltrust.ai/blog/openclaw-skills)), and a fake skill
  persuaded users to paste an install command
  ([Snyk](https://snyk.io/fr/blog/clawhub-malicious-google-skill-openclaw-malware/)).
- **T3. Logins.** Logins are stored encrypted through the existing credential
  handoff and vault ([`docs/CREDENTIAL_HANDOFF.md`](CREDENTIAL_HANDOFF.md)), scoped
  to one site each, and revocable. Disconnecting a source deletes its login and
  everything Kelp derived from that source: memory entries, observations and
  cached files. That requires every derived item to be tagged with its source.
  The tagging is planned and not designed yet.
- **T4. What always needs a card.** In both permission modes, "Ask before doing
  anything" and "Approve for me" (T7), these always show a card. So do new
  access (T1), destructive commands (T8) and starting a screen-control task
  (S8).
  - Anything another person will see: messages, posts, emails, form or assignment
    submissions, pull request comments.
  - Anything that costs money. The card states the exact amount.
  - Anything that cannot be undone or is destructive: delete, cancel, account
    changes, and a command that deletes or overwrites data (T8).
  - Accepting terms of service or any other agreement. Kelp always shows this as a
    card.

  Standing rules keep cards rare. They come in two kinds:
  - **Preferences** only shape or restrict how Kelp works: where logins are
    stored, when the morning report arrives, tone. An example is "every new login
    goes into the vault". A preference never waives a card.
  - **Standing approvals** widen what runs without a card. A standing approval is
    scoped to one action type and one target, is time-limited (default 30 days),
    and is listed and revocable in Settings. It can never cover spending money,
    deletion or any other irreversible act, anything another person will see, or
    accepting terms, or starting screen control (S8). It also can never cover
    a command outside the T8 allowlist or any T8-destructive command. A standing
    approval for commands binds to an exact `argv` pattern, never to a command
    name alone. It cannot waive a T1 card for new access (accounts and logins,
    folders, hosts, OS permissions).

  Both kinds are created only from a confirmation that the desktop app renders
  from structured fields (action type, target, expiry), never from model-written
  text. Neither kind can be created from a turn in which screen, web or
  tool-result text is the source of the request, and neither can be created by a
  scheduled watch. Every action that runs under a standing approval is logged
  with the approval id.

  Today's approvals (`openshell/approvals.py`) are single-use, bound to the
  command and its `argument_hash`, with a 600 s TTL. Standing approvals, and the
  plan-bound kind from brief §4.2, are new approval kinds. Both are planned. Prior
  art: OpenAI's agent asks before "actions of consequence"
  ([OpenAI](https://openai.com/index/introducing-chatgpt-agent/)).
- **T5. No AI reviewer in version 1.** No AI reviewer approves actions. The
  "Approve for me" mode (T7) keeps this: a deterministic classifier decides, and
  no model does. Revisit with the card approval rate (the "Card approval rate"
  row in brief §14.1 metrics). Prior art: Claude Code's auto mode replaces most
  per-action prompts with a classifier, after finding that users approved 93% of
  manual prompts
  ([Anthropic](https://www.anthropic.com/engineering/claude-code-auto-mode)).
- **T6. Setup starts only inside a user turn.** A setup plan can start only inside
  a turn the user started. The card text comes from catalog metadata only (brief
  §4.2). Resuming after a blocked one-tap user action continues an already
  approved plan. It never starts a new setup step.
- **T7. Permission modes (planned, not built).** The owner decided on
  2026-11-24 (brief §17, answer 14) that a global permission mode sets how often
  a card appears. It is modelled on Kiro IDE, which has Supervised, Accept file
  changes and Approve for me, where permission rules apply first and a model
  judges the rest ([Kiro](https://kiro.dev/docs/ide/chat/autopilot/)). Kelp
  differs on purpose: no model decides. The mode is chosen in the desktop app
  settings and from the chat input. It will be enforced in the desktop app,
  which classifies each action, and in the approval gate (`openshell/`).
  - **Ask before doing anything.** Every action shows a card with an Approve
    button: each skill call with side effects, each command, the start of each
    screen-control task and each install. A standing approval the user created
    skips the card for the action it covers. It never covers the start of a
    screen-control task (S8) or an install that grants new access (T1), and
    T4 limits what else it may cover. For screen control the card is the
    start card plus the T4 step cards (S8). It is not a card per click. In
    this mode the silent-install exception in T1 is off.
  - **Approve for me.** Routine actions run without a card. Each is logged and
    listed, and the action carries an "Approved for you" mark, as in Kiro. These
    always show a card in both modes: everything in T4, new access (T1) and
    destructive commands (T8). Starting a screen-control task also shows a card
    in both modes, because S8 required it before the modes existed.
  - **Routine is a positive list.** An action is routine only if its catalog
    effect class is on a pinned routine list that ships in the release. An
    action with an unknown effect class gets a card. The body classifies from
    pinned catalog metadata. It never trusts a "routine" label that the brain
    supplies, and that includes a Boat brain, as S8 already requires for the
    S4, S7 and S8 checks.
  - **No model decides.** The desktop app and the catalog classify each action
    deterministically. An action they cannot classify gets a card. This keeps
    T5.
  - **Standing approvals stay.** The mode is the coarse setting and standing
    approvals (T4) are the fine-grained rules. A standing approval the user
    created applies in both modes, so it skips the card in "Ask before doing
    anything" too. Nothing in this item changes what a standing approval can
    never cover.
  - **Where the mode lives and who may widen it.** The brain stores the mode.
    It accepts a change to "Approve for me" only when the change is signed with
    the desktop app's device key, which is created at first run and kept per
    L5. The desktop app registers the device public key with the brain at
    first run (Local) or when it pairs with a Boat brain over the SSH channel
    (L4). The brain accepts a registration only when no key is registered and
    the request arrives on the desktop's authenticated loopback or SSH
    channel, or, to replace a key, when the request is signed by the key
    already registered. It never accepts one from the phone, Telegram, a watch
    or model text. If the device key is lost, the user pairs the desktop again
    and the mode resets to "Ask before doing anything".
    - The desktop app signs only from a confirmation it renders itself. The
      signed payload covers the target mode, the brain id, a nonce and an
      expiry (the 600 s approval TTL, `APPROVAL_TTL_SECONDS`). Each change is
      single-use, and the brain keeps used nonces across restarts, so a
      captured change cannot be replayed, and a change made for a Local brain
      cannot be replayed to a Boat brain.
    - The desktop app keeps its own copy of the signed mode and uses it when
      it decides whether a routine step on the user's desktop skips a card. It
      adopts a brain-reported "Ask before doing anything" at once, because
      narrowing is always safe, and never adopts a reported "Approve for me"
      unless it holds the matching signed change.
    - A change is never accepted from model text or a scheduled watch, and the
      phone and Telegram routes cannot reach that setter. Switching to "Ask
      before doing anything" is accepted from any authenticated channel,
      including Telegram.
  - **Scheduled watches.** A scheduled watch auto-runs only the action set that
    the user approved when creating the watch in the desktop app. Any other
    action in a watch shows a card, which is pushed to the phone (L6). A T4, T1
    or destructive-command step always waits for a card.
  - **Injection.** In a turn whose context includes screen, web or tool-result
    text, "Approve for me" still applies only to actions the classifier marks
    routine, and a command needs a card unless it is in the action set of the
    watch or standing approval the turn runs under. Silent installs stay off in
    such a turn (T1). No T6 rule is weakened.
  - **Default on a new install.** Not decided (brief §17, question 16). The
    recommendation is "Approve for me", because T4, T1 and destructive commands
    always ask anyway, and it matches the zero repeated setup goal.
- **T8. Commands (planned, not built).** The rule that typing into a terminal
  counts as running a command stays (S8). The owner decided on 2026-11-24 that
  commands become allowed, each approved with a button (brief §17, answer 14).
  - **Ask before doing anything.** Every command shows a card with the exact
    command text (`argv`), rendered from what will run and never from model
    prose. A standing approval the user created (T4) can skip that card only
    for an allowlisted command, and it binds to an exact `argv` pattern.
  - **Approve for me.** A command runs without a card only if it is on the
    catalog allowlist of read-only commands. Everything else shows a card, also
    in this mode. No standing approval covers a command outside the allowlist
    (T4). A
    command that deletes, overwrites, moves, changes permissions, installs,
    pushes, sends, kills processes or uses `sudo` is destructive for this rule
    and always shows a card in both modes (T4). Unknown means card.
  - **Allowlist grammar.** An entry is per command and per argument grammar,
    not per command name. Examples are `git status`, `git log`, `ls`, and `cat`
    limited to approved folders. An entry only matches when every argument fits
    its grammar.
    - **Never on the allowlist:** interpreters and shells (`python -c`,
      `node -e`, `sh -c`, `bash`), `env`, `xargs`, `find -exec`, the exec
      features of `awk` and `sed`, build and test runners (`make`, `npm test`,
      `pytest`), and anything that runs code from the workspace. `git` itself
      can run repo-configured code (`core.fsmonitor`, filter drivers), so git
      entries run with those disabled or show a card.
    - **Resolved command.** `argv[0]` is resolved to an absolute path from the
      catalog entry, not from `PATH` at run time.
    - **Path arguments.** Each must be inside a folder the user approved (T1)
      and outside a secret-path denylist, for example `~/.ssh`, browser
      profiles, keyrings, `.env` files, Kelp's own credential store, the
      `$XDG_DATA_HOME/kelp/secrets/` fallback directory and the desktop app's
      device-key directory (L5).
    - **Who can change it.** The allowlist ships in the pinned release. It
      cannot be edited from chat, the phone, Telegram or a watch.
  - **Tainted turns and watches.** In a turn whose context includes screen, web
    or tool-result text, a command needs a card unless it is in the action set
    of the watch or standing approval the turn runs under (T7). A watch
    auto-runs only the commands the user approved when creating it.
  - **Route.** Commands run through `openshell/exec_gate.py` (argv only,
    allowlisted `argv[0]`, no `shell=True`, single-use approval, scrubbed
    output). Keystrokes into a terminal, remote shell, REPL, `sudo` prompt or
    launcher are not commands run through the gate. They always show a card
    with the exact text, in both modes (S8). The allowlist applies only to
    commands run through the gate.
  - **What the code does today (read 2026-11-24, at 0d468d1).** "Approve for
    me" for commands needs new gate work, because the gate cannot express it:
    - `openshell/policy.py:229-235` (`PolicyEngine._execute`) allowlists
      `argv[0]` only. It compares `str(argv[0])` with
      `PolicyConfig.allowed_commands` (`policy.py:60`, `:230`) and never reads
      `argv[1:]`, so there is no argument check and no path check.
    - `openshell/policy.py:244-248` returns `REQUIRE_APPROVAL` for every
      command that passes, and no branch returns `ALLOW`. The module docstring
      says so (`policy.py:4-5`).
    - `openshell/exec_gate.py:133-140` (`ExecGate.request`) creates an approval
      for every non-denied command. `exec_gate.py:196` (`ExecGate.execute`)
      always calls `self._approvals.consume(request_id)` and returns an
      `exec_blocked` result if that fails (`exec_gate.py:197-211`). There is no
      path that runs a command without a consumed approval.
    - `ExecGate.approve` (`exec_gate.py:154-168`) writes one `approval_granted`
      event with the actor. Nothing records an approval the user did not give.
    - `PolicyEngine._execute` also requires a working directory inside the
      workspace (`policy.py:236-243`).
  - **Before any command runs, in either permission mode.** In Local mode,
    OS-level confinement (L3) must be in place first. In Boat mode the sandbox
    is the boundary (L4). The cooperative egress guard is justified by "no
    exec" (§5), and that assumption ends when T8 ships. A command approved by
    a card still starts a process.
  - **Extra work before "Approve for me" can run a command.** This is more than
    the wiring steps in [`docs/PRIVACY.md`](PRIVACY.md) §5:
    - an argument-aware allowlist, with the grammar and the path checks above,
      that can return `ALLOW` for a matching command,
    - a logged "Approved for you" approval path in `ExecGate`, which records
      the rule that matched instead of a user decision.
  - **The exec boundary stays for now.** [`docs/PRIVACY.md`](PRIVACY.md) §5 and
    `tests/test_privacy.py` stay as they are until the three wiring steps listed
    in §5, L3 confinement and, for "Approve for me", the extra work above
    land. The wiring steps alone are not enough for either mode.

---

## 10. Cross-references

- **Recipient table** — who receives data, and what is sent:
  [`docs/PRIVACY.md`](PRIVACY.md) §2. The `unlisted` recipient and the
  base-URL override are documented there (§2, consequence 2) and enforced by
  `openshell/egress_ledger.py::recipient_for_host`.
- **What is not a recipient** — no analytics, no telemetry, no third-party assets:
  [`docs/PRIVACY.md`](PRIVACY.md) §2.2.
- **Principal table** — the owner, device sessions, share viewers, the scheduled
  actor, and plugin handlers, with who pays and who approves:
  [ADR 0002 §4](adr/0002-sharing-and-identity.md). The single-owner invariants
  this threat model sits inside are in ADR 0002 §1.
- **Share-link threat model** — token leakage, enumeration, revocation latency,
  CSRF, cross-read: [ADR 0002 §10](adr/0002-sharing-and-identity.md).
- **Exec boundary decision** — why no command runs in this build, and how to
  reverse it deliberately: [`docs/PRIVACY.md`](PRIVACY.md) §5. The owner decided
  on 2026-11-24 to allow commands behind approvals (planned, T8). The boundary
  stays until the wiring steps there and the extra work in T8 (argument
  grammar, an "Approved for you" path, L3 confinement) land.
- **Policy gate and audit log** — [`docs/OPENSHELL.md`](OPENSHELL.md).
- **Skill contract** — manifest rules including the `execute` prohibition:
  [`docs/SKILL_SPEC.md`](SKILL_SPEC.md).
- **Companion direction**: the product brief and decision record that the
  planned sections 6 to 9 come from:
  [`docs/PRODUCT_BRIEF.md`](PRODUCT_BRIEF.md) and
  [ADR 0004](adr/0004-desktop-companion.md).
- **Owner answers**: the answers that set L4 to L7, S3, S8, T2, T4, T7 and T8
  (answers 1 to 12, including Q11 and Q12, and the later answers 13 to 15):
  [`docs/PRODUCT_BRIEF.md`](PRODUCT_BRIEF.md#owner-answers-2026-11-24) and
  [later answers](PRODUCT_BRIEF.md#later-answers-2026-11-24). The
  remote approval design is in
  [brief §4.6](PRODUCT_BRIEF.md#46-remote-approvals-planned).
- **Tests** — [`tests/test_privacy.py`](../tests/test_privacy.py),
  [`tests/test_kernel_loop.py`](../tests/test_kernel_loop.py),
  [`tests/test_policy_enforcement.py`](../tests/test_policy_enforcement.py),
  [`tests/test_openshell_audit.py`](../tests/test_openshell_audit.py).

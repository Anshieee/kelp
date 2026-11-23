# Threat model — what the server process trusts, and what it does not (KEL-29)

- **Status:** written review, 2026-11-02
- **Branch:** `kel-29-threat-model`
- **Scope:** the server process started by `python -m chat.server` — `kernel/`, `skills/`, `openshell/`, and `chat/`
  and, as planned rules, the desktop companion (screen, voice, self-setup, local brain)
- **Companion sections:** added 2026-11-24, see [`docs/PRODUCT_BRIEF.md`](PRODUCT_BRIEF.md) and
  [ADR 0004](adr/0004-desktop-companion.md)
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
  egress rules) is what backs it.
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
  only, on a random per-install port, requires the owner bearer on every request,
  and rejects a request whose Host or Origin is not the expected loopback value
  (ADR 0004 §2, item 7). Any local process, and any web page open in a browser,
  can reach a loopback port. The bearer keeps the brain private. The address does
  not.
- **L2. The bearer stays on the Rust side.** The owner bearer is held by the
  desktop app's Rust side (`bridge_sign_in`) and never by the webview
  (`desktop/README.md`). Today the app stores nothing at rest, so the storage
  location is undecided and is open question 4 in the
  [product brief](PRODUCT_BRIEF.md) §17. That decision sets how private Local mode
  is, because any process that can read the chosen location can drive the brain.
- **L3. OS-level confinement before network-capable skills.** The skill egress
  guard (`skills/policy.py`) is cooperative at the Python layer, and §5 relies on
  a deployment boundary behind it (no exec, container egress rules). A sidecar
  running as the user has neither. Before any network-capable skill runs in Local
  mode, the sidecar must run under OS-level confinement with three properties:
  - no process execution outside `openshell`,
  - outbound network limited to the hosts the skill manifests declare,
  - filesystem access limited to Kelp's data directory plus folders the user
    approved.

  The mechanism on each OS is open. This section does not pick one. Until
  confinement exists, Local mode does not enable network-capable skills.
- **L4. Boat mode transport.** TLS only, the owner bearer always required, no
  pinning by IP address (it changes on resume), and provider keys, the vault key
  and credentials kept only inside the sandbox (ADR 0004 §2, item 8). Whether the
  app reaches the sandbox through an exposed port or a tunnel is open question 3
  in the brief and blocks Boat mode.

| Open decision | Where it is tracked |
|---|---|
| Where the owner bearer and vault key live in Local mode | Brief §17, question 4 |
| Confinement mechanism per OS | Open, no owner decision yet |
| Boat transport (exposed port or tunnel) | Brief §17, question 3 |

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
  Kelp captures only apps on the allowed list. This stays opt-in per app, chosen
  on a single screen.
- **S2. Retention.** Frames are never written to disk. They live in memory only.
  The capture module must not use file-producing APIs, or must delete the file
  immediately and never write it to a shared directory. The reason is measured:
  on the wlroots test machine the xdg-desktop-portal Screenshot call left
  `/tmp/out.png` with mode 0644, readable by every user on the machine. Kelp may
  keep short text observations (for example "tests failing in VS Code") for at
  most 24 hours. Anything kept longer enters memory only after the user okays it.
- **S3. Where frames go.** Kelp the project runs no server that receives frames,
  audio, transcripts or telemetry. "Cloud" always means a provider the user
  configured with their own key, or the user's own Boat VM. What leaves the
  machine depends on the run mode:

  | Run mode | What leaves the machine | Rule |
  |---|---|---|
  | Default reaction signals, any mode | Metadata only: the focused app id (11 ms to read on wlroots in the spike), idle time, and the window title for allowed apps | No pixels leave the machine by default. |
  | Local mode, local vision model | Nothing. Frames stay on the machine. | Used only if a hardware check passes. The spikes did not measure a local vision model, so it is unproven until that check exists. |
  | Local mode, cloud provider | A downscaled frame, to the provider the user configured | Only for apps the user marked "may send to cloud". That is a separate tick from "may watch". Never for blocked apps. Rate-limited. The indicator shows the destination (for example "watching: sent to <provider>"). Provider retention is out of scope (§5), and the setting says so plainly. |
  | Boat mode | Frames go to the user's own VM over the TLS and bearer link (L4) | The VM counts as the user's machine for retention, and the same no-disk rule (S2) applies inside the VM. If the VM's model is a third-party provider, the "may send to cloud" tick applies. |
- **S4. Block list.** Two tiers, checked against the focused app before any frame
  is taken:
  - Always blocked, cannot be lifted: password managers, private or incognito
    browser windows, Kelp's own credential entry screen
    ([`docs/CREDENTIAL_HANDOFF.md`](CREDENTIAL_HANDOFF.md) already refuses
    screenshots during private entry), and 2FA or authenticator apps.
  - Blocked by default, unlockable one site at a time: banking and payment sites.
    The unlock happens only in Settings, never from a setup card and never from
    chat.

  When a blocked app has focus, capture stops before the frame is taken. The
  focused-app check runs first. Reading the focused app is compositor-specific
  (the spike used MangoWC's IPC); other compositors need
  `wlr-foreign-toplevel-management` or their own mechanism.
- **S5. Auto-pause during sharing and calls.** Capture pauses while the screen is
  being shared or a call is active. The same condition mutes speech (V5). The
  detection method on each OS is open.
- **S6. Screen text is data.** Text read from the screen can never approve an
  action and can never start a setup step ([product brief](PRODUCT_BRIEF.md) §3).
  The reaction loop that watches the screen has no tool access (ADR 0004 §2,
  item 5). If Kelp sees text that looks like instructions aimed at it, it tells
  the user once per page or window ("this page tried to give me instructions; I
  ignored them") and does nothing else.
- **S7. Indicator.** Kelp draws its own watching and listening indicator. On
  wlroots the OS shows nothing during capture (spike).

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
  chat message.
- **V3. Speech-to-speech.** What leaves the machine depends on the run mode:

  | Run mode or option | What leaves the machine | Rule |
  |---|---|---|
  | Default pipeline, every mode | Text only, and only to wherever the brain's model runs | Local speech-to-text, text to the brain, text back, local text-to-speech. Measured: 3.1 to 3.3 s from end of speech to first audio, with a cloud text model on a CPU-only laptop (voice spike). |
  | Speech-to-speech realtime API | Raw audio, to a third party | Off by default. Available only as an explicit "fast voice" opt-in with the user's own provider key. The destination is named on the setting and on the listening indicator. Never used for wake-word listening. |
  | Boat mode | Text only, to the VM | Speech-to-text still runs on the user's machine, because of latency. |

  A self-hoster needs no speech-to-speech provider. The default pipeline works
  with local models or any text model.
- **V4. Blocked and private content is not read aloud.** Kelp never speaks
  content from a blocked or private app (S4). It shows that content as text
  instead.
- **V5. Speech is muted during calls and screen sharing.** The condition is S5.
  The one-key quiet mode ([product brief](PRODUCT_BRIEF.md) §3) mutes it too.

---

## 9. Self-setup (the capability resolver)

Status: planned. No code enforces this yet. Must land before the code it governs.

The resolver is described in [product brief](PRODUCT_BRIEF.md) §4 and
[ADR 0004](adr/0004-desktop-companion.md) §2, item 4. It does not exist yet. The
rules below will be enforced in the resolver and in the approval gate
(`openshell/`).

- **T1. What installs without a card.** A catalog entry that grants no new access
  installs silently, inside the sandbox. A card appears only when access grows: a
  new account or login, a new folder, a new network host, a new OS permission, or
  spending money. This refines the single approval card in brief §4.2: the card is
  shown when the plan grants new access, and the plan is still logged. For large
  downloads Kelp shows progress and no card, unless the download is over a size
  the user set. The default size is open.
- **T2. Sources.** Installs come only from the pinned catalog (digest pinning in
  `skills/loader.py`; publisher signing is a spike, open question 6 in the brief).
  Kelp may propose a new catalog entry. It cannot install an arbitrary package,
  and it never asks the user to paste a command into a terminal. Prior art: an
  audit of 2,857 OpenClaw skills found 341 malicious
  ([NeuralTrust](https://neuraltrust.ai/blog/openclaw-skills)), and a fake skill
  persuaded users to paste an install command
  ([Snyk](https://snyk.io/fr/blog/clawhub-malicious-google-skill-openclaw-malware/)).
- **T3. Logins.** Logins are stored encrypted through the existing credential
  handoff and vault ([`docs/CREDENTIAL_HANDOFF.md`](CREDENTIAL_HANDOFF.md)), scoped
  to one site each, and revocable. Disconnecting a source deletes its login and
  everything Kelp derived from that source: memory entries, observations and
  cached files. That requires every derived item to be tagged with its source.
  The tagging is planned and not designed yet.
- **T4. What always needs a card.**
  - Anything another person will see: messages, posts, emails, form or assignment
    submissions, pull request comments.
  - Anything that costs money. The card states the exact amount.
  - Anything that cannot be undone: delete, cancel, account changes.

  Standing approvals keep cards rare. A standing approval is created by the user,
  scoped to one action type and one target, time-limited (default 30 days), and
  listed and revocable in Settings. Today's approvals (`openshell/approvals.py`)
  are single-use, bound to the command and its `argument_hash`, with a 600 s TTL.
  Standing approvals, and the plan-bound kind from brief §4.2, are new approval
  kinds. Both are planned. Prior art: OpenAI's agent asks before "actions of
  consequence"
  ([OpenAI](https://openai.com/index/introducing-chatgpt-agent/)), and Claude
  Code's auto mode asks once about boundaries
  ([Anthropic](https://www.anthropic.com/engineering/claude-code-auto-mode)).
- **T5. No AI reviewer in version 1.** No AI reviewer approves actions. Revisit
  with approval-rate data (brief §14.1 metrics).
- **T6. Setup starts only inside a user turn.** A setup plan can start only inside
  a turn the user started. The card text comes from catalog metadata only (brief
  §4.2).

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
  reverse it deliberately: [`docs/PRIVACY.md`](PRIVACY.md) §5.
- **Policy gate and audit log** — [`docs/OPENSHELL.md`](OPENSHELL.md).
- **Skill contract** — manifest rules including the `execute` prohibition:
  [`docs/SKILL_SPEC.md`](SKILL_SPEC.md).
- **Companion direction**: the product brief and decision record that the
  planned sections 6 to 9 come from:
  [`docs/PRODUCT_BRIEF.md`](PRODUCT_BRIEF.md) and
  [ADR 0004](adr/0004-desktop-companion.md).
- **Tests** — [`tests/test_privacy.py`](../tests/test_privacy.py),
  [`tests/test_kernel_loop.py`](../tests/test_kernel_loop.py),
  [`tests/test_policy_enforcement.py`](../tests/test_policy_enforcement.py),
  [`tests/test_openshell_audit.py`](../tests/test_openshell_audit.py).

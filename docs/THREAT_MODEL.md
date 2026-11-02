# Threat model — what the server process trusts, and what it does not (KEL-29)

- **Status:** written review, 2026-11-02
- **Branch:** `kel-29-threat-model`
- **Scope:** the server process started by `python -m chat.server` — `kernel/`, `skills/`, `openshell/`, and `chat/`
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
  process can do.
- **The share surface.** The first unauthenticated read path has its own threat
  model — token leakage, enumeration, revocation latency, cross-read — in
  [ADR 0002 §10](adr/0002-sharing-and-identity.md). This document does not repeat
  it; the two are consistent by construction (a share read makes no outbound call,
  so it adds no recipient and no egress row).

---

## 6. Cross-references

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
- **Tests** — [`tests/test_privacy.py`](../tests/test_privacy.py),
  [`tests/test_kernel_loop.py`](../tests/test_kernel_loop.py),
  [`tests/test_policy_enforcement.py`](../tests/test_policy_enforcement.py),
  [`tests/test_openshell_audit.py`](../tests/test_openshell_audit.py).

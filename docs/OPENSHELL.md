# OpenShell: approvals, audit, and gated exec (KEL-7 / P0-3)

- **Status:** scaffold, 2026-10-28
- **Branch:** `kel-7`
- **Contract:** consumes `docs/SKILL_SPEC.md` manifests; adds the policy gate the spec defers to P0-3
- **Tests:** `tests/test_openshell_policy.py`, `tests/test_openshell_approvals.py`,
  `tests/test_openshell_exec_gate.py`, `tests/test_openshell_audit.py`, `tests/test_payments.py`

The gate sits between a tool call and its side effect. Default deny. Anything not
explicitly allowed becomes a single-use approval request that expires after ten
minutes. Every decision and execution is appended to an audit log with secrets
redacted before they reach disk.

## 1. Policy

`PolicyEngine.evaluate(capability, target=…, host=…, argv=…, cwd=…)` returns
`allow`, `require_approval`, or `deny`. The worst result across a capability's
effects wins.

| Effect | Rule |
|---|---|
| `read` | allowed only inside the declared `${WORKSPACE}/…` scope; symlinks are resolved and `..` cannot escape |
| `write` | inside scope → **approval**; outside scope → **deny** (opt-in escalation to approval, never outside the workspace) |
| `network` | allowed only for exact hosts or a leading `*.` wildcard; no scheme, port, path, or IP |
| `execute` | **deny** unless the deployment sets `allow_exec=True`, the exact `argv[0]` is allowlisted, and cwd is inside the workspace; even then it always requires approval |
| unknown / empty | deny |

`execute` is intentionally **not** accepted in a KEL-5 skill manifest. The loader
still rejects it (v1.0). This gate is the separate, explicitly enabled capability
P0-3 owns; a future spec revision can bridge them.

### 1a. Invoke-time skill enforcement (KEL-20)

`SkillRegistry.invoke` builds an `InvocationPolicy` from the loaded manifest on
every call. Concrete path, host, and repo targets in the arguments are checked
against the declared scopes after schema validation and **before** approval.
Arguments are typed by the skill: only an `input_schema` property declaring
`format: kelp-path` (read), `kelp-write-path`, `kelp-host`, or `kelp-repo` is
interpreted, and a typed target with no declared scope is denied. An unknown
`kelp-*` format fails the manifest at load. A handler can also assert its own
intent through the context:

| Context call | Enforced |
|---|---|
| `read_path` / `write_path` | resolved (symlink-aware) target inside `${WORKSPACE}` and a declared prefix |
| `check_repo` | exact declared `owner/name`, or a manifest `*` |
| `require_effect` | effect is declared in the manifest |
| `egress()` | host in the declared allowlist and method compatible with `read`/`write` |

The egress client is the single network chokepoint. It refuses redirects,
embedded credentials, IP literals, and non-80/443 ports, and caps the response
body. A ContextVar-scoped socket guard blocks direct `socket`, `urllib`, and
HTTP-library connections while a handler runs — DNS (`getaddrinfo`,
`gethostbyname*`, `gethostbyaddr`), `create_connection`,
`connect`/`connect_ex`/`sendto`/`sendmsg` — and follows the handler into threads
and executor tasks started during the call. The guard is cooperative: raw
`_socket` objects, child processes, C extensions, and pre-existing threads fed
through a queue rather than `submit` are outside it. Importing egress code
directly in `skills/builtin` also fails the policy test, including
`from`-imports, aliases, `__import__`, and `importlib`.

A policy `deny` is terminal: `policy_denied` has no approval block and the
approval hook is never invoked. Only `requires_approval: true` in the manifest
creates a prompt. Every invocation writes one `skill_invoked` audit line with
skill/version, effects, scope decision, outcome, duration, and approval hash —
never raw arguments or content.

## 2. Approvals

`ApprovalStore` transitions a request through `pending → approved → consumed`, or
`denied` / `expired`.

| Field | Value |
|---|---|
| TTL | 600 s (10 min), configurable |
| `single_use` | always `true` |
| `argument_hash` | `sha256:` over canonical JSON of tool, command, metadata |
| `blast_radius` | effect-labelled targets (`fs:write:…`, `net:…`, `exec:…`, `cwd:…`) |
| replay | `consume` is atomic under a lock; second call → `already_used` |
| expired | cannot be approved, denied, or consumed → `expired` |
| `to_dict(include_command=False)` | model-facing copy; command and metadata omitted |

Denials leave no side effect. The decision itself is audited.

## 3. Exec gate

```python
request = gate.request(["echo", "hi"], turn_id="turn_1")   # policy check + approval object
gate.approve(request.id, turn_id="turn_1")                  # human decision
result = await gate.execute(request.id, turn_id="turn_1")   # atomic consume + spawn
```

- `argv` is a sequence; a shell string is rejected. There is no `shell=True` path.
- The child gets a minimal environment (`PATH`, `HOME`, `LANG`, `LC_ALL`, `TERM`,
  `TMPDIR`) and **no** variable whose name looks secret-like (`*TOKEN*`, `*KEY*`,
  `*SECRET*`, `*PASSWORD*`, `*CREDENTIAL*`, `*AUTH*`, `*PRIVATE*`).
- `stdin` is `/dev/null`, the process gets its own session, and a timeout kills
  the whole process group.
- stdout/stderr are capped, scrubbed, then returned. The audit stores lengths and
  SHA-256 hashes, never the bodies.
- Policy is re-evaluated at execution time; an approval cannot outlive a policy
  change.

## 4. Audit log

JSONL, append-only, mode `0600`:

```json
{"argument_hash":"sha256:…","command":["echo","hi"],"event":"approval_requested",
 "extra":{"expires_at":"…","single_use":true},"request_id":"apr_…","ts":"…",
 "turn_id":"turn_1","tool":"shell.exec"}
```

Events for one turn: `exec_requested`, `approval_requested`, `approval_granted`
(or `approval_denied`), `exec_started`, `exec_finished`; blocked attempts add
`exec_blocked` with `outcome` (`not_approved`, `denied`, `expired`,
`already_used`, `policy_denied`). Skill calls add `skill_invoked`
(`decision`, `outcome`, `duration_ms`, and content-free `extra` with skill,
version, effects, and scope decision) and egress adds `skill_egress` (host,
method, decision, outcome). Filter with `audit.entries(turn_id=…)`.

Redaction:

- exact values of secret-looking environment variables are replaced with
  `[redacted]` before serialization (the gate adds them automatically);
- `Bearer …`, `sk_live_…` / `sk_test_…`, and `token=…` / `password: …` patterns
  are redacted even when the value was not declared;
- commands, actor names, and nested `extra` fields all pass through the scrubber.

The log never contains stdout/stderr bodies, environment dumps, or raw secrets.

## 5. Payments placeholder

`payments/stripe_wallet.py` reads `STRIPE_SECRET_KEY` from the server environment
only. It has no network calls and no SDK yet; the account owner is providing a
test key. `repr`, `str`, and `status()` expose only the mode (`test` / `live` /
`unknown`), never the key. `payments/.env.example` holds an empty value.

## 6. Not in this slice

- Approval UI / inbox rendering (no UI build).
- Policy-editor configuration surface; policy is process configuration.
- Container or OS-level sandboxing (M3).
- Stripe API calls, saved cards, and webhooks (awaiting the test key).

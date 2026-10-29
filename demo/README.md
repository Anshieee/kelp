# KEL-9 — demo choreography

One command plays six short scenes in order. It uses the real chat service,
kernel loop, skill runtime, approval gate, artifact registry, and HTTP API; the
model and the network are replaced by recorded fixtures, so the run is
deterministic, offline, and needs no keys.

```bash
make demo                 # or: python -m demo
python -m demo --json     # one JSON summary, no progress lines
python -m demo --state /tmp/demo-run
```

Exit status is `0` only if all six beats pass. A failed check names the beat and
points at the report.

## The six beats

| # | Beat | What it proves |
|---|---|---|
| 1 | Ask a question that needs search | `web.search` runs, the answer streams, source chips carry recorded URLs |
| 2 | Summarise a PDF, then question a CSV | uploads become artifacts, `docs.answer` reads them, page and row citations, PDF served intact, table preview |
| 3 | Look up a GitHub issue | `github.read` is read-only: no approval card, `read`/`network` effects only, issue URL becomes a chip |
| 4 | Approve a write | deny first — nothing is written, and the decision is audited at decision time as `denied`; then approve — the write runs once and the audit log shows the decision, the single use, and the execution. The audited note carries a planted fake key and must land as `[redacted]` |
| 5 | Schedule a task | approval, then the task is on the scheduler list with a real cron and next run time |
| 6 | Kill the server mid-answer | a real child process serving `ChatAPI` is `SIGKILL`ed mid-stream; a **new** process boots with `--recover`, closes the interrupted turn, and serves the snapshot, SSE replay, retry, and idempotent resend over its own socket, with no duplicate user message and no third turn |

Each beat writes `receipts/beat-N.json` (checks, timings, ids) and the run writes
`receipts/demo-report.md` with a timing table. Screenshot capture belongs to the
dashboard slice; these receipts are the review artefacts for this backend-only
run.

## What is real, what is recorded

Real: `chat.ChatService`, `chat.api.ChatAPI` (auth, routing, SSE framing,
multipart upload), `kernel.AgentLoop`, `skills.SkillRegistry` (manifest
validation, approval gate, schema checks), `openshell.AuditLog`, and
`chat.ArtifactRegistry`. The skill approval gate is the KEL-8 path in
`chat.service` over the chat store; `openshell.ApprovalStore` is only wired for
exec approvals, which this demo does not exercise.

Recorded: the provider (`demo/fixtures/provider.py` streams scripted tool calls
and text) and the four handlers (`demo/handlers/`). The registry is built from
the **real** `skills/builtin/*/manifest.json` files; only `entry.module` is
rewritten to the fixture handler, so schemas, effects, scopes, approval flags,
and timeouts stay exactly what the product declares.

Fixture inputs live in `demo/fixtures/files/`: a two-page PDF, a three-row CSV,
a search result set, and GitHub payloads. The PDF and CSV are uploaded through
the real `POST /api/attachments` path, then claimed by a turn.

## Beat 6 mechanics

`demo/server.py` is the process under test. In turn mode it opens the run's
database, serves `ChatAPI` on an ephemeral loopback port, starts one slow turn,
and prints a single JSON ready line. The demo kills it with `SIGKILL` while
tokens are still arriving, then starts it again with `--recover`: that boot step
calls `ChatService.recover_interrupted()` before printing its ready line, so the
turn is already `finished`/`interrupted` when the parent can observe it. The
parent then talks to that restarted process over `http.client` — snapshot,
`GET /stream?after=0`, retry, and resend — so recovery is proven across a
process boundary rather than by calling a method in the same process.

## State

Default state is `var/demo` (git-ignored); `--state` overrides it. Each run
leaves:

```
var/demo/
  chat.db                      durable threads, events, turns, approvals, artifacts
  var/artifacts/               uploaded PDF and CSV bytes
  var/scheduler/tasks.jsonl    tasks created after approval
  audit.jsonl                  decisions and executions (secret-scrubbed)
  skills/<name>/manifest.json  generated fixture registry
  receipts/beat-N.json         per-beat checks and timings
  receipts/demo-report.md      timing table and every check
```

Re-running against an existing state is safe: threads are new each run and the
checks are relative to what the run created.

## Adding a beat

1. Append the turn's script to `TURN_SCRIPTS` in `demo/runner.py` — one list of
   provider events per model step, in turn order. Beat 6's `RESUME_SCRIPT` is
   replayed by the restarted process, so keep it last and append new beats
   before it.
2. Add the beat method and include it in `Demo.run`. Use `self.check(...)` for
   every claim; an unchecked beat is a beat that cannot catch a regression.
3. If the beat needs a new skill, add the handler under `demo/handlers/` and map
   its manifest name in `demo/fixtures/registry.py`. A builtin manifest without a
   handler fails the run at start-up.

## Tests

`tests/test_demo.py` runs the whole thing in a subprocess with API keys stripped
from the environment, proves the CLI exits `1` on a failed beat, compares each
fixture manifest against the raw builtin JSON (everything except `entry`), and
pins the handlers, cron evaluation (including Sunday as `7`), CSV/PDF citation
locators, fail-closed behaviour, the audited denial, and the secret-scrubbed
audit field.

# KEL-15 evaluation harness

`make eval` runs a versioned, deterministic evaluation set against the real
retrieval, chat, approval, and recovery code. The default run is offline: it
blocks outbound sockets, replays recorded provider transcripts, and finishes in
well under a minute.

```bash
make eval                 # offline, 3 repeats, gated
make eval EVAL_ARGS="--live"   # opt-in recorded/live provider run, labelled
python -m evals --list    # dataset inventory
```

Reports are written to `var/evals/<timestamp>-<mode>/report.json` and
`report.md` (`var/` is gitignored).

## Dataset

`evals/datasets/v1/tasks.json` is the versioned set. It must contain 20-30
tasks across five categories:

| Category | What it measures |
| --- | --- |
| `retrieval` | `docs.answer` answer terms and `page N` / `row N` citation locators |
| `tool` | selected tool names and arguments for a recorded model turn |
| `approval` | card creation, approval/denial, single use, expiry, secret redaction |
| `restart_resume` | boot recovery closes turns, fails tools, expires approvals, retry reuses the user message |
| `multi_turn` | thread history append, idempotent client turn ids, thread isolation, stable auto title |

A task declares a `scenario`, an optional local `doc`, a recorded fixture id,
and explicit `expect` fields. Retrieval documents are generated under
`var/artifacts` from the dataset content, so the dataset is the single source of
truth. Nothing in the dataset is executed directly.

## Recorded provider fixtures

`evals/fixtures/recorded/provider_v1.json` is a hand-maintained, deterministic
replay set. It is labelled `recorded://synthetic` in every report: it is **not**
captured production traffic, and it contains no credentials. Each turn is a
list of kernel provider events (`text`, `tool_call`, `usage`, `failure`,
`stream_end`). `{{artifact}}` placeholders are replaced with the task's
generated handle.

The offline run wraps the whole suite in `evals.offline.block_network`, which
replaces connect and name-resolution paths with a tripwire. A task that tries
to reach the network fails the run; it cannot silently pass.

## What offline tool and multi-turn accuracy means

In the offline run the model is a recording. Tool selection, tool arguments and
the final answer of a `tool`, `approval`, `restart_resume` or `multi_turn` task
are replayed from `provider_v1.json`, so a pass there measures **replay
fidelity** (the real chat service, loop, approval and recovery code handles the
recorded events correctly). It says nothing about whether a live model would
choose those tools. Only `--live` measures that.

Replay alone cannot see a regression in the *request* the service builds, so
every provider request is also recorded (snapshotted when sent, because the
agent loop keeps appending to the same transcript list) and the gate checks
its shape. These are ordinary per-task checks in `report.json`; any failure
fails the task:

| Check | Tasks | Passes when |
| --- | --- | --- |
| `request_tool_specs` | tasks with `expect.tool_calls` (`tool`, `approval`) | every request carries a tool spec for each expected tool name |
| `request_tool_results` | same | the last request has a `role: "tool"` message for each executed call's `tool_call_id` |
| `followup_sent_last` | `multi_turn_two` | the second request ends with the follow-up as a `user` message |
| `retry_request_prompt` | `resume_retry` | the retried request carries the original user prompt |
| `history_sent` | `multi_turn_two` with `expect.history_in_request: true` | the second request contains the first user prompt and the first assistant answer |

`history_sent` is **off in the shipped dataset**. `ChatService` currently sends
only the current user text to the model on every turn, and prompt assembly from
stored history is explicitly a later slice (`docs/CHAT_SPEC.md` section 1), so
the check would fail today. The check exists and is tested; turn it on in
`EV-023` when the service starts sending history. Until then the gate does not
claim that earlier turns reach the model.

## Metrics and gates

`evals/metrics.py` computes:

- pass rate;
- citation precision over returned retrieval citations (and recall);
- tool accuracy: matched expected calls divided by the larger of expected and
  observed calls, averaged over tool/approval tasks;
- approval correctness: approval-task pass rate;
- p50/p95 task latency (nearest-rank, over every repeat);
- input/output/total tokens and estimated USD cost from real `UsageRecord`s;
- error rate: tasks whose sample raised or ended in a harness error;
- per-category pass rates and a global determinism flag.

`evals/thresholds.json` holds the release gates. Offline runs exit non-zero if
a metric misses its bound, the dataset falls outside 20-30 tasks, the run takes
longer than 60 seconds, repeats are not identical, or a gate metric regressed
against the most recent offline report for the same dataset version. Live mode
is opt-in and labelled; it reports the same metrics but does not gate the
offline result.

## Live mode

`--live` is the only path that creates a `ProviderClient`. It requires both
`KELP_PROVIDER_BASE_URL` and `KELP_PROVIDER_API_KEY`; there is no default host,
and the harness never prints, stores, or reports the key. Tool
execution is still constrained: `docs.answer` is the real local handler, and the
other tool outputs are deterministic recording stubs, so live mode measures
model tool selection without granting third-party egress.

## Determinism

Offline mode repeats every task three times and hashes the observation
(checks, citations, tool calls, outputs, tokens, cost) while excluding wall
time. All three hashes must match. The report records `deterministic: true` and
the gate fails if it is false.

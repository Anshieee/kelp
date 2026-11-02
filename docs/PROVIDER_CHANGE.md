# Change the model provider (provider swap runbook)

- **Status:** written runbook, 2 November 2026
- **Branch:** `kel-29-provider-change`
- **Scope:** documentation only. No code changed.
- **Verified against:** `kernel/config.py`, `kernel/provider.py`, `kernel/loop.py`,
  `kernel/usage.py`, `chat/server.py`, `chat/service.py`, `chat/errors.py`,
  `openshell/egress_ledger.py`, `evals/README.md`, `evals/cli.py`,
  `evals/thresholds.json`, `evals/scenarios.py`, `docs/PRIVACY.md`,
  `docs/DEPLOY.md` §3, §8.1, §11–§13, `docs/KERNEL.md` §1
- **Measured on this branch:** `python -m evals` offline gates pass (26 tasks × 3
  repeats, deterministic, 404 ms); `tests/test_kernel_loop.py`,
  `tests/test_kernel_provider.py`, `tests/test_spend_caps.py` — 71 passed.

Kelp talks to any OpenAI-compatible chat endpoint through one vendor-neutral
client (`ProviderClient` in `kernel/provider.py`). This repository documents
OpenRouter (`https://openrouter.ai/api/v1`); the operator chooses the endpoint.
There are two swap kinds, and this recipe covers both:

- **(a) Model swap** — a different model on the documented OpenRouter endpoint.
  The endpoint does not change; the model ids and the pricing table do.
- **(b) Endpoint swap** — a different provider entirely. The base URL and key
  change, and the privacy ledger must be checked against the new host.

Either way, three things must move together: the model configuration, a
pricing-table entry, and a green offline eval run. Skip the pricing entry and
the spend guardrail stops trusting your cost accounting — see §2.

---

## 1. The environment switches

Four environment variables configure the provider, read by
`KernelConfig.from_env` in `kernel/config.py`:

| Variable | Constant | Default | Role |
|---|---|---|---|
| `KELP_PROVIDER_BASE_URL` | `BASE_URL_ENV` | **none** | The OpenAI-compatible chat endpoint |
| `KELP_PROVIDER_API_KEY` | `API_KEY_ENV` | **none** | Bearer key for that endpoint |
| `KELP_PRIMARY_MODEL` | `PRIMARY_MODEL_ENV` | `nvidia/nemotron-3.5-super-vl` (`DEFAULT_PRIMARY_MODEL`) | Conversational turns |
| `KELP_BACKGROUND_MODEL` | `BACKGROUND_MODEL_ENV` | `nvidia/nemotron-3.5-lightning` (`DEFAULT_BACKGROUND_MODEL`) | Titles, summaries, compaction |

**The base URL has no default, on purpose.** This process sends the whole
transcript to whatever endpoint the operator names, so the endpoint is a
required, explicit choice rather than a baked-in third party
(`kernel/config.py` module docstring). `from_env` returns `base_url=None` when
the variable is unset or blank. What happens then, verified in the code:

- **Server paths refuse to start.** `ServerConfig.from_env` (`chat/server.py`)
  raises `ConfigError("missing_base_url", ...)` before anything binds — the
  process exits without ever having a provider to call. `docs/DEPLOY.md` §3
  lists `KELP_PROVIDER_BASE_URL` among the three values with no default (the
  other two are `KELP_PROVIDER_API_KEY` and `KELP_AUTH_TOKEN`); the container
  exits `2` without all three.
- **Non-server paths fail typed, on first use.** `ProviderClient.stream`
  (`kernel/provider.py`) yields `ProviderFailure("missing_base_url", ...)`,
  non-retryable, and sends nothing — a misconfigured process cannot fall back
  to an inherited host.

**The API key behaves the same way, twice over.** The server refuses to start
without it too (`ConfigError("missing_api_key", ...)` in `chat/server.py`), and
`ProviderClient.stream` yields the typed `missing_api_key` failure on first use
for callers that construct a client directly (CLI, tests, the eval harness).
The key is sent only as the bearer header to the configured endpoint and is
never echoed (`kernel/provider.py` design rules; `docs/PRIVACY.md` §1).

**The model ids are sent verbatim.** `from_env` falls back to the defaults when
a model variable is unset or blank (`source.get(PRIMARY_MODEL_ENV) or
DEFAULT_PRIMARY_MODEL`). The loop resolves a role to an id with
`KernelConfig.model_for` (`ModelRole.PRIMARY` / `ModelRole.BACKGROUND`), and
`AgentLoop` puts it into `ChatRequest(model=...)` (`kernel/loop.py`);
`ProviderClient.stream` sends it as `"model"` in the payload. A typo in the id
is not caught locally — the endpoint answers HTTP 404, which `classify_status`
maps to `provider_model_not_found` ("The model provider does not expose this
model"), non-retryable.

To override without touching code, set the variables in the deployment
environment (`deploy/kelp.env`; `deploy/kelp.env.example` lists all four, and
`docs/DEPLOY.md` §3 is the contract table). To change the defaults themselves,
edit `DEFAULT_PRIMARY_MODEL` / `DEFAULT_BACKGROUND_MODEL` in `kernel/config.py`
— that is step 2 of the model-swap recipe below.

## 2. The pricing table update (required)

Every model id that can appear in a turn needs a `ModelPricing` entry in
`DEFAULT_PRICING` in `kernel/config.py` — USD per one million tokens, list
price. What the repo ships by default is exactly the two documented slugs, at
the documented provider's published list prices:

```python
DEFAULT_PRICING: Mapping[str, ModelPricing] = {
    "nvidia/nemotron-3.5-super-vl": ModelPricing(0.15, 0.50),
    "nvidia/nemotron-3.5-lightning": ModelPricing(0.07, 0.20),
}
```

These numbers are OpenRouter's list prices from its public models API
(`https://openrouter.ai/api/v1/models`, read 2026-11-02): the API's
`pricing.prompt` / `pricing.completion` are per-token values
(`0.00000015` / `0.0000005` for Super VL, `0.00000007` / `0.0000002` for
Lightning), i.e. $0.15/$0.50 and $0.07/$0.20 per million. When you add a row
for a new model, read the same `pricing` object and multiply by one million.
(OpenRouter's marketing model pages also show per-host routing prices that can
be lower; the models-API pricing object is the canonical list the repo quotes.)

`ModelPricing.estimate` computes `(input/1M) × input_per_million + (output/1M)
× output_per_million`. The loop calls `estimate_cost(model, ...)`
(`kernel/config.py`) when it builds the turn's `UsageRecord`
(`kernel/loop.py`); for a model with no entry, `pricing_for` returns `None`, so
the record carries `estimated_cost_usd=None` and `cost_known=False`. The kernel
never invents a price (`docs/KERNEL.md` §3).

What that costs you, verified in `kernel/usage.py`: `SpendGuard.evaluate`
counts every record whose `estimated_cost_usd` is not a usable non-negative
number as an *unknown-price record* for the day and month windows. When a cap
is configured (`KELP_SPEND_CAP_DAY_USD` / `KELP_SPEND_CAP_MONTH_USD`) and the
unknown-price mode is `block` — the default (`UNKNOWN_PRICE_BLOCK`,
`SpendCaps.from_env`) — the very first unpriced record in a capped window sets
`blocked=True` with `block_reason="unknown_price"`, before the cap amount is
even reached. The chat layer turns that into a `402 spend_cap_reached` refusal
before the turn row, the messages, or any provider call exist
(`ChatService._refuse_if_over_spend` in `chat/service.py`; the code-to-status
mapping is in `chat/errors.py`). So a missing pricing row does not charge $0 —
it blocks new turns outright. The alternative is `count_as`
(`KELP_SPEND_UNKNOWN_PRICE_MODE=count_as`), which charges each unpriced record
`KELP_SPEND_UNKNOWN_PRICE_USD` (default `0.01`) instead; that is an operator
escape hatch, not a substitute for the table entry.

Without any cap configured the unknown price is recorded but nothing blocks —
the gap then shows up in `/api/metrics` as
`spend_unknown_price_records{window="day"}` > 0, which `docs/DEPLOY.md` §13.2
tells the operator to treat as "the configured model has no price in
`kernel/config.py`".

One caveat that matters for endpoint swaps: `DEFAULT_PRICING` is keyed by model
id alone, not by (endpoint, model id). If two providers expose the same model
id at different prices, one row serves both — pick the price of the endpoint
this deployment actually calls, and re-check the row when you switch endpoints.

## 3. The exact steps

### (a) Swapping the model on the documented OpenRouter endpoint

1. **Pick the new model ids** from the provider's model list
   (`https://openrouter.ai/api/v1/models`). You need one conversational model
   and one cheap background model; they may be the same id.
2. **Set the ids** — either edit the defaults `DEFAULT_PRIMARY_MODEL` and
   `DEFAULT_BACKGROUND_MODEL` in `kernel/config.py`, or set
   `KELP_PRIMARY_MODEL` / `KELP_BACKGROUND_MODEL` in `deploy/kelp.env`
   (`docs/DEPLOY.md` §3). Keep the id exactly as the provider exposes it — it
   is sent verbatim as `"model"` in the request (`kernel/provider.py`).
3. **Add the pricing entry** to `DEFAULT_PRICING` in `kernel/config.py`, one
   `ModelPricing(input_per_million, output_per_million)` row per new id, from
   the provider's models-API `pricing` object (§2 above). Update the comment
   above the table with the date you took the prices. If a role keeps its old
   model, its existing row stays.
4. **Run the offline evals** and confirm every gate passes (§4 and §5):

   ```bash
   python -m evals
   ```

5. **Run the owned test suites**:

   ```bash
   python -m pytest tests/test_kernel_loop.py tests/test_kernel_provider.py tests/test_spend_caps.py -q
   ```

6. **Deploy as usual** (`docs/DEPLOY.md` §11) and run the §12.2 smoke: one real
   turn must end `"reason":"complete"` with `"estimated_cost_usd"` greater than
   `0.0`, and `/api/metrics` must show
   `spend_unknown_price_records{window="day"}` at `0` (§13.2). A `null` cost or
   a non-zero unknown count means the pricing row and the model id do not
   match.

### (b) Pointing at a different provider endpoint entirely

1. **Choose the endpoint.** It must speak the OpenAI-compatible
   `POST {base_url}/chat/completions` API with `stream: true`,
   `stream_options.include_usage`, and OpenAI-style streaming tool calls
   (`kernel/provider.py`; `docs/KERNEL.md` §1). Redirects are disabled, so the
   endpoint must serve the API itself.
2. **Set both required variables** in `deploy/kelp.env`:

   ```bash
   KELP_PROVIDER_BASE_URL=https://your-endpoint.example/v1
   KELP_PROVIDER_API_KEY=...
   ```

   The server refuses to start without either one (`ConfigError
   missing_base_url` / `missing_api_key`, `chat/server.py`; `docs/DEPLOY.md`
   §3). There is no default host to fall back to.
3. **Verify the recipient ledger after the first real turn.** The recipient is
   resolved from the actual URL host, not from the variable name
   (`_record_egress` in `kernel/provider.py`, `recipient_for_host` in
   `openshell/egress_ledger.py`): a base URL at `openrouter.ai` is recorded as
   `openrouter`; **any other host is recorded as `unlisted`** (§6). Check one
   `egress` row in the audit trail and confirm the recipient id is the one you
   intended — that row is the record of who now receives the transcript.
4. **Add pricing rows** for the endpoint's model ids (step 3 of recipe (a)).
   Remember the §2 caveat: the table is keyed by model id, so a shared id takes
   the price of the endpoint you now call.
5. **Run the offline evals, the owned suites, and the §12.2 smoke** exactly as
   in recipe (a) steps 4–6.

## 4. Running the evals green

The offline run is the default and the only gated mode:

```bash
python -m evals            # offline, 3 repeats, gated — same as `make eval`
make eval                  # the Makefile target: python -m evals $(EVAL_ARGS)
python -m evals --list     # dataset inventory only
```

There is no `--offline` flag: offline is what `python -m evals` does unless
`--live` is passed (`evals/cli.py`). `--live` is the opt-in, labelled,
non-gating provider run; it requires both `KELP_PROVIDER_BASE_URL` and
`KELP_PROVIDER_API_KEY` and fails with `LiveModeError` without them
(`evals/runner.py`). `make eval` is the same command through the `Makefile`.

The offline run blocks outbound sockets (`evals/offline.py::block_network` — a
task that tries to reach the network fails the run), replays recorded provider
transcripts from `evals/fixtures/recorded/provider_v1.json`, repeats every task
three times, and must finish under 60 seconds. Reports land in
`var/evals/<timestamp>-offline/report.json` and `report.md` (`var/` is
gitignored). A successful run prints:

```
EVAL offline (gating) dataset=1.0.0 tasks=26 repeat=3
pass_rate=100.00% citation_precision=100.00% tool_accuracy=100.00% approval_correctness=100.00% error_rate=0.00%
p50=3.0ms p95=16.0ms tokens=6414 cost=$0.0011777 deterministic=yes
thresholds=pass regressions=0 gates=pass duration=404ms
```

Measured on `kel-29-provider-change`, 2 November 2026. The cost line comes from
real `UsageRecord`s, so a model id missing from `DEFAULT_PRICING` also shows up
here as unknown-cost records.

## 5. What "green" means

Green is every gate in `evals/thresholds.json` passing, plus the run-level
conditions the runner enforces (`evals/README.md`):

| Gate | Bound |
|---|---|
| `pass_rate` | ≥ 0.95 |
| `citation_precision` | ≥ 0.95 |
| `tool_accuracy` | ≥ 0.95 |
| `approval_correctness` | = 1.0 |
| `error_rate` | = 0.0 |
| `p95_latency_ms` | ≤ 5000 |
| task count | 20–30 |
| offline duration | ≤ 60 000 ms |
| determinism | all three repeat hashes identical |
| regression | no gate metric below the most recent offline report for the same dataset version (tolerances in `regression_tolerance`: `citation_precision` 0.02, `p95_latency_ms` 500 ms, everything else 0) |

The 26 tasks span five categories (`evals/datasets/v1/tasks.json`, driven by the
scenario handlers in `evals/scenarios.py`): `retrieval` (answer terms and
`page N` / `row N` citation locators), `tool` (tool names and arguments for a
recorded turn), `approval` (card creation, approve/deny, single use, expiry,
secret redaction), `restart_resume` (boot recovery closes turns, fails tools,
expires approvals, retry reuses the user message), and `multi_turn` (history
append, idempotent client turn ids, thread isolation, stable auto title).

If a gate fails after a swap, check these knobs, in this order:

1. **Temperature.** The default is `0.2` (`ChatRequest.temperature`,
   `kernel/provider.py`); it is sent on every request. A model that needs a
   different sampling setting for reliable tool calls is adjusted there.
2. **`max_tokens` budgets.** `TurnBudget` (`kernel/loop.py`) caps a turn at
   `max_steps=6`, `max_output_tokens=4096`, `max_tokens_per_step=1024`,
   `max_tool_calls=8`, `max_wall_seconds=120.0`. A verbose model can exhaust
   `max_tokens_per_step` mid-answer; a slow one can hit the wall clock.
3. **The endpoint's tool-calling format.** The client assembles streaming tool
   calls from OpenAI-style `delta.tool_calls` entries and parses the
   accumulated `arguments` JSON (`_StreamState` in `kernel/provider.py`).
   Malformed arguments fail the turn with `invalid_tool_arguments`, which shows
   up as `tool_accuracy` and `error_rate` misses. An endpoint whose tool-call
   wire format is not OpenAI-compatible needs client work, not just a URL
   swap.

Also remember the offline eval replays *recorded* turns — it proves the
plumbing, not the new model's behaviour. The labelled live run
(`make eval EVAL_ARGS="--live"`, requires both provider variables) measures the
real model's tool selection; it does not gate, so treat it as evidence, not a
release check.

## 6. The privacy note

The provider endpoint is the content-bearing recipient. On the documented path
the recipient id is `openrouter` (host `openrouter.ai`, `RECIPIENTS` in
`openshell/egress_ledger.py`): the full model request — system prompt, thread
messages, tool results, tool schemas, model name, sampling settings — goes to
it on every assistant turn (`docs/PRIVACY.md` §2; the request is built in
`ProviderClient.stream`). It is the only content-bearing egress in the build.

If the operator points `KELP_PROVIDER_BASE_URL` at any other host, the ledger
records the calls under the `unlisted` recipient instead, because the recipient
is resolved from the actual URL host with an exact host match
(`_record_egress` in `kernel/provider.py` and `recipient_for_host` in
`openshell/egress_ledger.py`; `docs/PRIVACY.md` §2 lists `unlisted` as a real
recipient for exactly this case). A swapped endpoint is therefore a new
recipient of the full transcript, visible in the audit trail as `unlisted` —
point the variable only at endpoints you have decided to trust.

Two smaller facts from the same review, for completeness: the readiness probe
sends one bounded `GET {base_url}/models` with the key but no user content
(`purpose = "provider.health"`, `docs/PRIVACY.md` §2.1), and the ledger stores
counts and fixed labels only — never content (`openshell/egress_ledger.py`).

## 7. Rollback

Set the environment variables back to their previous values and restart. Two
precise notes:

- **The base URL cannot be "unset" back to a default — there is none.** With
  `KELP_PROVIDER_BASE_URL` unset or blank, the server refuses to start
  (`ConfigError("missing_base_url")`, `chat/server.py`; exit `2` per
  `docs/DEPLOY.md` §3). Rolling back an endpoint swap means restoring the
  previous URL value, not deleting the variable. The model variables are the
  opposite: unsetting them restores the `DEFAULT_PRIMARY_MODEL` /
  `DEFAULT_BACKGROUND_MODEL` defaults through `KernelConfig.from_env`.
- **Pricing rows are inert if left.** An unused `DEFAULT_PRICING` row changes
  nothing. It can be removed in the same change that removed the model, but old
  usage records keep the model id they were written with, so a row is safe to
  remove only after no retained record in `usage.jsonl` references that id
  within the 90-day usage retention window (`docs/DEPLOY.md` §8.1); otherwise
  those records become unknown-price records for the spend guardrail (§2
  above). When in doubt, leave the row.

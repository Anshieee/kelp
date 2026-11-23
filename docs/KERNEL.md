# Kernel agent loop (KEL-6 / P0-1)

- **Status:** scaffold, 2026-10-28
- **Branch:** `kel-6`
- **Contract:** `docs/SKILL_SPEC.md` for tools; this doc for the loop
- **Tests:** `tests/test_kernel_provider.py`, `tests/test_kernel_loop.py`

One user turn in, one ordered stream of typed events out. The loop owns provider
calls, tool-call dispatch, budgets, and usage accounting. It does not render
anything; the dashboard, Telegram, and CLI are adapters.

## 1. Provider

`kernel.provider.ProviderClient` speaks the OpenAI-compatible
`POST /chat/completions` API with `stream: true` and
`stream_options.include_usage: true`.

| Setting | Env var | Default |
|---|---|---|
| Provider id | `KELP_PROVIDER` | `openai-compatible` |
| Base URL | `KELP_PROVIDER_BASE_URL` | **none** (empty means unconfigured; typed `missing_base_url`, no request) |
| API key (server-only) | `KELP_PROVIDER_API_KEY` | — |
| Primary model | `KELP_PRIMARY_MODEL` | documented example: `nvidia/nemotron-3.5-super-vl` |
| Background model | `KELP_BACKGROUND_MODEL` | documented example: `nvidia/nemotron-3.5-lightning` |
| Price table override | `KELP_PRICING_JSON` | unset → the example rows in `kernel/providers/presets.py` |

The provider is any OpenAI-compatible service, not a named vendor. `KELP_PROVIDER`
is the id written to records and admin rows; it defaults to `openai-compatible`
and does not select a different code path. To use a second provider, change that
id, the base URL, the key, and the model ids; nothing needs a code fork.

`Super VL` and `Lightning` above are **examples**, kept in
`kernel/providers/presets.py`, because the product reads documents and
screenshots. If your endpoint serves other ids, set `KELP_PRIMARY_MODEL` and
`KELP_BACKGROUND_MODEL` to them. `Lightning` is the cheap **background** model
for titles, summaries, compaction, and other non-user-facing work; select it with
`config.model_for(ModelRole.BACKGROUND)`.

A missing base URL yields a typed `missing_base_url` event and no request is
made, so a misconfigured process cannot send the transcript to an inherited
host. The empty default is deliberate: there is no vendor endpoint to fall back
to. A missing key yields `missing_api_key` and likewise sends nothing. Neither
crashes startup (the server entry point does refuse to start without both).
Redirects are disabled; the client never follows a provider-controlled URL.

`KELP_PRICING_JSON` is optional. It is a JSON object of model id to
`input_per_million` and `output_per_million` (USD per one million tokens). Rows
add to or replace the example table, so a deployment can price its own models or
correct a published price. A model with no row keeps `estimated_cost_usd = None`
(`cost_known: false`); that is unchanged, and it fails closed when a spend cap
is configured.

The earlier Nebius-named variables are accepted only as explicit compatibility
aliases: `NEBIUS_BASE_URL` / `NEBIUS_API_BASE_URL`, `NEBIUS_API_KEY`,
`NEBIUS_PRIMARY_MODEL`, and `NEBIUS_BACKGROUND_MODEL`. The canonical `KELP_*`
name always wins and the alias is read only when the canonical value is blank.
No alias is a default: with none of them set, the endpoint stays empty.

### Streaming events (internal)

`TextDelta`, `ToolCallReady` (assembled from deltas, arguments parsed), `UsageReport`,
`ProviderFailure`, `StreamEnd`.

HTTP status mapping:

| Status | Code | Retryable |
|---|---|---|
| 401 / 403 | `provider_auth` | no |
| 404 | `provider_model_not_found` | no |
| 408 | `provider_timeout` | yes |
| 429 | `provider_rate_limited` | yes |
| 5xx | `provider_unavailable` | yes |
| other 4xx | `provider_rejected` | no |
| read timeout / connect error | `provider_timeout` / `provider_unreachable` | yes |

Provider error bodies are not echoed into events, so upstream payloads cannot
leak into the UI or logs.

## 2. Loop

```python
loop = AgentLoop(
    provider=ProviderClient.from_env(),
    tool_executor=RegistryToolExecutor(registry),
    usage_store=JsonlUsageStore("var/usage/turns.jsonl"),
)
async for event in loop.run_turn(messages, tools=tool_schemas, session_id="s-1"):
    ...
```

### Turn budgets

| Limit | Default | Behaviour |
|---|---|---|
| `max_steps` | 6 | model calls per turn |
| `max_output_tokens` | 4096 | sum of reported completion tokens |
| `max_tokens_per_step` | 1024 | `max_tokens` sent to the provider |
| `max_tool_calls` | 8 | executed tool calls per turn |
| `max_wall_seconds` | 120 | wall clock, checked between steps |

Exceeding a limit emits `error.code = "turn_budget_exceeded"` with
`details.limit` set to the limit name, then `turn_finished` with
`reason = "budget"`. The turn stops; the session and loop stay usable.

Token budgeting consumes the provider's reported usage only. If a backend does
not report usage, only the step, tool-call, and wall-clock limits bound the
turn, and `max_output_tokens` is not consumed by that step. The record keeps
`usage_reported: false` so this is visible. The same applies when a stream is
cancelled or fails before its final usage chunk (the last chunk in OpenAI-style
streams).

The step still reached the provider, so its cost is not recorded as `$0`. A step
that ends without a `UsageReport` is recorded at an upper bound, and the record
carries `usage_estimated: true`:

- input tokens: the UTF-8 byte length of the compact JSON of
  `{"messages": <transcript sent in that request>, "tools": <tools>}`. A token
  covers at least one byte and the JSON keys and quotes add far more bytes than
  a chat template adds tokens, so this is above the billed input for text
  messages. An image or other non-text part could bill more than its bytes; the
  chat service sends text only today. A system prompt added by a provider
  adapter is not counted;
- output tokens: that step's `max_tokens`, which the provider cannot exceed.

The record's `input_tokens`, `output_tokens`, `total_tokens`, and
`estimated_cost_usd` are the reported counts plus these bounds. The bound is
generally well above the real usage, so the cost is a ceiling. A model with no
price still records `estimated_cost_usd: null`. `usage_reported` stays `true`
only if some step reported usage. `turn_finished` keeps the reported token
counts, and its `estimated_cost_usd` equals the record's.

A step is not estimated only when its first and only event is a provider failure
that means no request was accepted for billing: a failure carrying an HTTP status
(the provider answered with an error status), or a failure raised before any
request (`missing_base_url`, `missing_api_key`, and `tenant_mismatch` from
`kernel.providers.base.tenant_failure`). Every other unmetered step is bounded.
That includes a timeout, an unreachable endpoint, and a protocol error that
carry no HTTP status. An unreachable endpoint usually billed nothing, so its
bound overcounts; the loop cannot tell a refused connection from a dropped one,
and overcounting is the safe direction for a cap. The rule also
includes a failure after an HTTP 200 (a truncated stream, a malformed first
chunk, a transport error mid-body, an error chunk inside the body), when the
provider may already have billed. A failure after any text or tool call, a
cancel, or an exception while the stream was open is bounded too.

`TurnFinished` and `UsageUpdated` token fields are reported-only; they never
include the bound. `UsageUpdated.estimated_cost_usd` is likewise priced from
reported tokens only, while `TurnFinished.estimated_cost_usd` equals the record's.
When the turn exits through `CancelledError` or an exception, the loop still
writes the record with the bound, but no `TurnFinished` reaches the chat layer.
`chat/service.py` then finalizes from the last `UsageUpdated` it saw
(`state.estimated_cost_usd`), so the turn's `usage` and the `TaskRun` `cost_usd`
can be lower than the `usage.jsonl` record (and `cost_usd` is `0.0` when no
`UsageUpdated` arrived). Spend caps read `usage.jsonl`, so enforcement uses the
bound.

### Tool calls

The model emits tool calls; the loop validates nothing itself. It hands each call
to the injected `ToolExecutor` and returns the whole result as the observation:
`RegistryToolExecutor` therefore inherits every KEL-5 guarantee (schema validation,
approval hook, timeout, typed error envelope). Tool exceptions and non-object
returns become observations with `ok: false`; they never break the stream.

### Events

`turn_started`, `token_delta`, `tool_call_started`, `tool_call_finished`,
`usage_updated`, `error`, `turn_finished`. All support `to_dict()` and are
JSON-serializable.

A provider failure mid-stream produces a visible `error` event with the
provider's retryable flag, then `turn_finished(reason="error")`. No exception
crosses `run_turn`.

## 3. Usage accounting

One `UsageRecord` per turn is appended to the configured store:

```json
{"turn_id":"turn_…","session_id":"s-1","model":"nvidia/nemotron-3.5-super-vl",
 "input_tokens":812,"output_tokens":143,"total_tokens":955,
 "estimated_cost_usd":null,"cost_known":false,"usage_reported":true,
 "usage_estimated":false,
 "steps":2,"tool_calls":1,"started_at":"…","finished_at":"…",
 "duration_ms":912,"outcome":"stop"}
```

- Pricing comes from `KernelConfig.pricing` (USD per 1M tokens), which the loop
  reads through `estimate_cost` and `pricing_for`. That table starts from
  `kernel.config.DEFAULT_PRICING`, which ships the two default models at the
  documented provider's published list price (`openrouter.ai/api/v1/models`, read
  2026-11-02). When `KELP_PRICING_JSON` is set, its rows are merged over those
  defaults: a row replaces the default with the same model id, and any other model
  id is added. A model id in neither table is unpriced.
- A turn whose consumer disconnects or whose task is cancelled mid-turn still
  writes exactly one usage record, with `outcome: "cancelled"` and the input and
  output tokens counted up to that point. A `CancelToken` stop writes the same
  outcome through the normal path.
- Unknown models record `estimated_cost_usd: null` and `cost_known: false`; the
  kernel never invents a price.
- `JsonlUsageStore` appends with `fsync` and creates the file `0600`. It contains
  no prompts, no responses, and no secrets — only counts and identifiers.
- A broken store sets `usage_persisted: false` on `turn_finished`; accounting
  never kills a turn. No store configured is also reported as not persisted.

`kernel.usage.SpendGuard` turns those persisted records into the M3-C spend
ceiling. It reads the JSONL on every check (never an in-memory counter), sums
the current UTC day and month, and reports:

- a window's counted spend, configured limit, ratio, and unknown-price count;
- `warnings` at 80% of a configured limit, recorded in a small sidecar marker
  file so the warning happens once per window even across restarts;
- `blocked` with `block_reason` `cap`, `unknown_price`, or `usage_unavailable`.

The guard is configured with `KELP_SPEND_CAP_DAY_USD`,
`KELP_SPEND_CAP_MONTH_USD`, `KELP_SPEND_UNKNOWN_PRICE_MODE`
(`block`/`count_as`), and `KELP_SPEND_UNKNOWN_PRICE_USD`. It does not raise: the
chat layer refuses a new turn before it exists, so no provider call is made and
no partial message is written (`docs/DEPLOY.md` §13.1).

## 4. Deliberately not in this slice

- Dashboard/transport (SSE/WebSocket) and any UI.
- Memory, retrieval, and prompt assembly.
- Retry policy beyond surfacing the retryable flag; the caller decides.
- A real recorded-fixture integration test against a live provider. Provider tests
  use `httpx.MockTransport` and stay offline. Add the recorded cassette when the
  first live key is provisioned.

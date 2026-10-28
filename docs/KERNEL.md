# Kernel agent loop (KEL-6 / P0-1)

- **Status:** scaffold, 2026-10-28
- **Branch:** `kel-6`
- **Contract:** `docs/SKILL_SPEC.md` for tools; this doc for the loop
- **Tests:** `tests/test_kernel_provider.py`, `tests/test_kernel_loop.py`

One user turn in, one ordered stream of typed events out. The loop owns provider
calls, tool-call dispatch, budgets, and usage accounting. It does not render
anything; the dashboard, Telegram, and CLI are adapters.

## 1. Provider

`kernel.provider.TokenFactoryClient` speaks the OpenAI-compatible
`POST /chat/completions` API with `stream: true` and
`stream_options.include_usage: true`.

| Setting | Env var | Default |
|---|---|---|
| Base URL | `NEBIUS_TOKEN_FACTORY_BASE_URL` (legacy `NEBIUS_BASE_URL`) | `https://api.tokenfactory.nebius.com/v1` |
| API key (server-only) | `NEBIUS_API_KEY` | — |
| Primary model | `KELP_PRIMARY_MODEL` | `nvidia/Nemotron-3_5-Super-VL` |
| Background model | `KELP_BACKGROUND_MODEL` | `nvidia/Nemotron-3_5-Lightning` |

`Super VL` is the default conversational model because the product reads
documents and screenshots. `Lightning` is the cheap **background** model for
titles, summaries, compaction, and other non-user-facing work; select it with
`config.model_for(ModelRole.BACKGROUND)`.

A missing key does not crash startup. The first call yields a typed
`missing_api_key` event. Redirects are disabled; the client never follows a
provider-controlled URL.

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
    provider=TokenFactoryClient.from_env(),
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

Token budgeting consumes the provider's reported usage. If a backend does not
report usage, only the step, tool-call, and wall-clock limits bound the turn;
the record keeps `usage_reported: false` so this is visible.

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
{"turn_id":"turn_…","session_id":"s-1","model":"nvidia/Nemotron-3_5-Super-VL",
 "input_tokens":812,"output_tokens":143,"total_tokens":955,
 "estimated_cost_usd":0.0003723,"cost_known":true,"usage_reported":true,
 "steps":2,"tool_calls":1,"started_at":"…","finished_at":"…",
 "duration_ms":912,"outcome":"stop"}
```

- Pricing comes from `kernel.config.DEFAULT_PRICING` (USD per 1M tokens, list
  price on 2026-10-28). Update it when Token Factory pricing changes.
- Unknown models record `estimated_cost_usd: null` and `cost_known: false`; the
  kernel never invents a price.
- `JsonlUsageStore` appends with `fsync` and creates the file `0600`. It contains
  no prompts, no responses, and no secrets — only counts and identifiers.
- A broken store sets `usage_persisted: false` on `turn_finished`; accounting
  never kills a turn.

## 4. Deliberately not in this slice

- Dashboard/transport (SSE/WebSocket) and any UI.
- Memory, retrieval, and prompt assembly.
- Retry policy beyond surfacing the retryable flag; the caller decides.
- A real recorded-fixture integration test against Token Factory. Provider tests
  use `httpx.MockTransport` and stay offline. Add the recorded cassette when the
  first live key is provisioned.

# Desktop ↔ chat API bridge

> Why a bridge at all: the Tauri webview cannot call `chat/api.py` directly.
> The server has no CORS layer, and its CSRF gate compares `Origin` to `Host`
> and rejects non-http(s) schemes. A webview origin of `http://tauri.localhost`
> (Windows) or `tauri://localhost` (macOS) fails both checks, so every call
> would die before reaching a route.

## Shape

```
React (webview)  --invoke-->  Rust bridge  --HTTP-->  chat/api.py
                 <--event---               <--SSE---
```

Rust owns the transport, the cookie jar and the stream. The webview never holds
a credential and never speaks HTTP to the server.

## Commands (Rust → webview)

| Command | Purpose |
|---|---|
| `bridge_server_url` | the one configured base URL |
| `bridge_sign_in { bearer, device_label }` | `POST /api/auth/session`; cookies land in the Rust jar, the CSRF half is captured |
| `bridge_sign_out` | `DELETE /api/auth/session`, clear jar |
| `bridge_api { method, path, body }` | any JSON call; adds `Origin` + `X-CSRF-Token` on mutations |
| `bridge_open_stream { thread_id, after }` | opens SSE, forwards frames as `chat-event` |
| `bridge_close_stream { thread_id }` | cancels the task |

`bridge_api` returns the server's JSON verbatim. Failures return
`{ status, code, message, retryable }` taken from the `{ok:false,error:{...}}`
envelope, so React can map codes to copy without parsing HTTP.

## Events (Rust → webview)

`chat-event` payload:
```json
{ "threadId": "thr_…", "id": "1042", "event": "token_delta", "data": { … } }
```
`id` is the SSE `seq`, used for `last_seq` and dedupe. `chat-stream-state`
carries `{ threadId, state: "open" | "reconnecting" | "closed" }`.

## Auth rules the bridge must honour

- Bearer is used **once**, in `bridge_sign_in`. After that the jar's session
  cookie authorizes everything.
- Mutations need `X-CSRF-Token` (from the `csrf` cookie) **and** an `Origin`
  equal to the server origin. Rust is not a browser, so it sets both itself.
- `401` means the session expired: React shows sign-in again.
- `429` carries `Retry-After`; `409` codes (`turn_not_running`,
  `not_latest_turn`, `already_used`, …) are states, not errors.

## Contract details the old fixture client got wrong

- Errors live at `error.message` / `error.code`, not at the top level.
- `POST /api/threads/{id}/turns` **requires** `client_turn_id` (idempotency).
- The stream resumes with `after=<last_seq>` from the history snapshot.

## Out of scope for this pass

Approvals UI, artifacts, devices, spend, shares, wallets. The bridge exposes
them all through `bridge_api`; only chat + streaming + stop/retry is wired into
the UI now.

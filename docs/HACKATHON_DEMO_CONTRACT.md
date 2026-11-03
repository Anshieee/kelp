# Kelp hackathon demo v1 API facade

This is the shared user-facing contract consumed by the isolated mobile fallback.
It matches the Plan B backend implementation in the `kelp-demo-backend` branch;
the mobile app does not define alternate routes or worker APIs. HTTPS only.

## Shared rules

- Prefix `/api/v1`; authenticated requests use a verified managed Identity access
  token. The owner always comes from verified auth, never a caller-supplied ID.
- Responses use `{ok, ...}` or `{ok:false,error:{code,message,retryable}}`.
- Send `Cache-Control: no-store` for authenticated/API traffic. Do not cache keys,
  task responses, events, candidates, diffs, or auth tokens.
- Only OpenRouter is supported. Provider keys are sent transiently to the server,
  validated and encrypted server-side; they are never echoed to clients.
- The demo repository must be allowlisted and the verified user entitled.

## User endpoints

| Method and path | Contract |
| --- | --- |
| `GET /me` | Identity, onboarding completion, provider/model status, entitlement and workspace/repository metadata. No secrets. |
| `PUT /me/provider` | `{provider:"openrouter",api_key,model?}`; validates a bounded inference, encrypts server-side, never echoes the key. |
| `POST /me/onboarding/complete` | Verifies required setup and idempotently completes onboarding. |
| `GET /workspace` | Workspace `status` (`ready`, `offline`, or `busy`), `backend_kind:"github_actions"`, real repository and capabilities. |
| `POST /tasks` | `{client_request_id,prompt,repo}`; checks entitlement/allowlist and returns `202 {task_id,state}`. Request ID is idempotent. |
| `GET /tasks/{id}` | Owner-scoped status, summary, base SHA, tests, pending approval, decision, PR URL and commit SHA. |
| `GET /tasks/{id}/events?after=N` | Ordered finite `{seq,...}` events and cursor for reconnect. |
| `GET /tasks/{id}/candidate` | Candidate `{summary,files,diff,checks,base_sha}` with immutable `candidate_digest`. |
| `POST /tasks/{id}/decision` | `{decision,candidate_digest,client_decision_id}`; atomically consumes one unexpired approval. |
| `POST /tasks/{id}/cancel` | Idempotent; does not claim to undo external effects. |

## Lifecycle and review

Task states are `queued → preparing → working → testing → awaiting_approval →
publishing → completed`, with terminal `failed`, `cancelled`, `rejected`, `expired`,
and `interrupted`. Completed tasks include a confirmed PR URL and commit SHA.
Approval expires after ten minutes. The exact tested candidate is immutable and is
never regenerated after approval; duplicate requests/decisions are idempotent.

The facade has no workspace task-list/history endpoint and no computer-preview or
desktop-connectivity capability. This client shows history for task IDs created in
the current browser session and restores an active task by its session-scoped ID;
it does not invent a listing API or simulate desktop support.

Worker-only endpoints are intentionally excluded from this client. See the Plan B
backend branch's `docs/KELP_DEMO_BACKEND.md` for deployment configuration and worker
details.

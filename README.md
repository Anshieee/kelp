# Kelp

Kelp is a private, always-on assistant for chat, approval-gated work, scheduled tasks, and spend caps. Saved-card payment flows are present in the codebase but are not wired into the running service yet. Kelp stores chat and working data locally; model requests go to the provider configured by the operator. The documented provider endpoint is OpenRouter.

## Current status

Kelp is live as a single-owner, single-provider service. Today it provides:

- an authenticated chat API with streaming turns,
- an approval-gated shell that reviews actions before they run,
- a scheduler for timed work,
- spend caps that stop calls before the provider sees them when budget runs out,
- offline and provider-backed evaluations.

The phone and landing demos are live. The browser chat UI and the dashboard page at `/` are not built yet; the API is the interface.

What just landed: **P0-1 froze the service control layer's contract** (2026-11-09). This is a design freeze, not a runtime change — no code behavior changed. The frozen shapes cover the single owner tenant, the provider stream contract, the `TaskRun` record, the gate order (auth → tenant → entitlement → spend → provider), and the admin read model. Read them at:

- [`docs/SERVICE_CONTRACT.md`](docs/SERVICE_CONTRACT.md) — the frozen contract (normative)
- [`docs/adr/0003-service-control-layer.md`](docs/adr/0003-service-control-layer.md) — the decision record

The contract is machine-checked by `tests/test_service_contract.py`, which is green (13 tests).

## Quick start

Choose Docker Compose or Python. Both need an OpenRouter API key and an owner token; the owner token keeps the API private.

Set these three variables in the environment file used by your chosen path:

```dotenv
KELP_AUTH_TOKEN=
KELP_PROVIDER_API_KEY=
KELP_PROVIDER_BASE_URL=https://openrouter.ai/api/v1
```

Fill in the two credentials locally; do not commit the file or put credentials in chat, logs, or command arguments.

### Docker Compose

Requires Docker Engine and the Compose v2 plugin. Compose runs the image built from this checkout and keeps state in a named volume.

```sh
cp deploy/kelp.env.example deploy/kelp.env
${EDITOR:-vi} deploy/kelp.env   # set the three variables above
docker build -t kelp:local .
KELP_IMAGE=kelp:local docker compose up -d --no-build
```

For local HTTP use only, also set `KELP_SESSION_COOKIE_SECURE=0` in `deploy/kelp.env`. Keep the secure-cookie default for deployments behind HTTPS. Stop the service with `KELP_IMAGE=kelp:local docker compose down`.

### Python

Requires Python 3.12 and `make`.

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
cp .env.example .env
${EDITOR:-vi} .env   # set the three variables above
make serve
```

`make serve` loads `.env` when present. Keep the server running in that terminal.

## First-turn smoke test

The service currently exposes the authenticated chat API; a browser chat UI is not included yet. With the server running, use this Python smoke test to create a thread, send one prompt, and stream until the turn finishes:

Load the same environment file into this shell first: use `set -a; . ./.env; set +a` for the Python setup, or `set -a; . deploy/kelp.env; set +a` for Compose.

```sh
python - <<'PY'
import json
import os
import urllib.request

base = "http://127.0.0.1:8765"
headers = {
    "Authorization": "Bearer " + os.environ["KELP_AUTH_TOKEN"],
    "Content-Type": "application/json",
}

def post(path, payload):
    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)

thread = post("/api/threads", {"title": "smoke test"})["thread"]["id"]
post(f"/api/threads/{thread}/turns", {
    "client_turn_id": "readme-smoke-1",
    "text": "Reply with the single word: ok",
})
request = urllib.request.Request(
    f"{base}/api/threads/{thread}/stream?after=0",
    headers={"Authorization": headers["Authorization"]},
)
with urllib.request.urlopen(request, timeout=90) as response:
    for raw_line in response:
        line = raw_line.decode().rstrip()
        if line.startswith("event:") or line.startswith("data:"):
            print(line)
        if line == "event: turn_finished":
            break
PY
```

The service and API listen at `http://127.0.0.1:8765`. There is no dashboard page at `/` yet. `netlify/` holds the Netlify Functions backend and the mobile demo build, not the assistant UI.

## Repository map

- `chat/` — authenticated chat API, persistence, sessions, scheduler, and runtime.
- `kernel/` — model loop, budgets, and usage accounting.
- `skills/` — built-in skills and their contracts.
- `openshell/` — policy, approvals, audit, and controlled egress.
- `payments/` — payment and saved-card domain code.
- `evals/` — offline and provider-backed evaluation harnesses.
- `deploy/` — container health checks and deployment configuration.
- `desktop/` — desktop app/demo surface (on branches that include it).

## Tests

Install development dependencies and run the suite:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest
```

Run the deterministic offline evaluations with `python -m evals`.

## Roadmap

The control-layer contract (P0-1) is the foundation the work below builds on. Each item implements against the frozen shapes in [`docs/SERVICE_CONTRACT.md`](docs/SERVICE_CONTRACT.md).

- **P1** — provider neutrality and bring-your-own model keys: routes model calls through a provider-agnostic seam so the operator's own keys and endpoints work, not just OpenRouter.
- **P2** — tenant isolation boundaries: harden the single-tenant model into explicit per-tenant scope so a later second tenant cannot read another tenant's data.
- **P3** — task-run records and API: persist each run's identity, gate decisions, and outcome in the `TaskRun` shape and expose them over the API.
- **P4** — per-tenant secrets: store provider credentials and tokens per tenant instead of one shared credential.
- **P5** — entitlements and billing hooks: wire plan limits and windows into the gates and emit billing events at the seams.
- **P6** — admin visibility and support runbook: expose the admin read model and publish a runbook for diagnosing incidents.

Boat and Steel infrastructure pilots come only after the bot control layer is built; they are not a prerequisite for the work above.

## How to contribute

1. Pick an item from [Good first pickups](#good-first-pickups) or from a P1–P6 roadmap item.
2. Create a branch from `origin/main` and do the work there.
3. Keep the test suite green: run `python -m pytest -q` before you consider the work done. The contract tests must keep passing.
4. Open a pull request for review. Merge happens only after green CI and an approving review.
5. Never commit secrets or credentials. Keys live in untracked env files (`.env`, `deploy/kelp.env`), not in commits, chat, logs, or arguments.
6. Keep tests honest: for a behavior change, write the test first, watch it fail, then implement.

The repo rules: no force-push, no remote deletions, plain merges only.

## Good first pickups

These are small, self-contained items for a first contribution. All of them assume you have read the frozen contract first: [`docs/SERVICE_CONTRACT.md`](docs/SERVICE_CONTRACT.md).

- **P1-2 config/env generalization** — move provider config (base URL, key) behind a general env scheme so a second provider can be configured without forking the code.
- **Tenant slug validation tests** — add tests that tenant ids are valid URL-safe slugs, matching the contract's slug rule (ADR 0003 §3).
- **TaskRun recorder** — a minimal module that writes run records in the frozen `TaskRun` shape, covered by tests.
- **Secret-presence admin endpoint** — an admin-only check that reports whether a secret is present without revealing it.
- **Support runbook triage entries** — draft triage steps in the support runbook for the gate failures the contract defines (auth, tenant, entitlement, spend, provider).
- **Extra eval scenarios** — add offline evaluation cases that exercise the frozen contract's edge cases, keeping `python -m evals` deterministic.

## More

- [Deployment guide](docs/DEPLOY.md)
- [Privacy review](docs/PRIVACY.md)
- [Chat and authentication spec](docs/CHAT_SPEC.md#12-security-and-privacy)

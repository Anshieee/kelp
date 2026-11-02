# Kelp

Kelp is a private, always-on assistant for chat, approval-gated work, scheduled tasks, and spend caps. Saved-card payment flows are present in the codebase but are not wired into the running service yet. Kelp stores chat and working data locally; model requests go to the provider configured by the operator. The documented provider endpoint is OpenRouter.

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

The service and API listen at `http://127.0.0.1:8765`. There is no dashboard page at `/` yet. `netlify/` contains the project tracker, not the assistant UI.

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

## More

- [Deployment guide](docs/DEPLOY.md)
- [Privacy review](docs/PRIVACY.md)
- [Chat and authentication spec](docs/CHAT_SPEC.md#12-security-and-privacy)
- [Project tracker](netlify/index.html)

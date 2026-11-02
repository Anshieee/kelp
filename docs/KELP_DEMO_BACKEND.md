# Kelp demo Plan B backend

This implements the frozen `/api/v1` facade with Netlify Functions, PostgreSQL-backed transactional state, Netlify Identity HS256 verification, and repository-dispatch GitHub Actions workers. Provider keys are AES-256-GCM encrypted at rest and only returned to an authenticated worker claim over HTTPS with `Cache-Control: no-store`.

## Required configuration

Set these in Netlify site environment settings and GitHub repository settings; never put values in the repository, workflow inputs, URLs, or issue/chat messages.

| Setting | Where | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | Netlify | PostgreSQL connection string with TLS enabled |
| `NETLIFY_IDENTITY_JWT_SECRET` | Netlify | Netlify Identity token-signing secret |
| `DEMO_KEY_ENCRYPTION_SECRET` | Netlify | Base64-encoded random 32-byte AES-256 key |
| `DEMO_WORKER_TOKEN` | Netlify + GitHub Actions secret | Independent random worker-only bearer token (at least 32 characters) |
| `GITHUB_REPOSITORY` | Netlify | Exact `owner/repo` for the demo repository |
| `DEMO_ALLOWED_REPOS` | Netlify | Comma-separated allowlist; include only the one authorized repository |
| `DEMO_ALLOWED_SUBJECTS` | Netlify | Comma-separated verified Netlify Identity subjects entitled to run the demo |
| `GITHUB_DISPATCH_TOKEN` | Netlify | GitHub token that can dispatch the two demo workflow event types |
| `DEMO_API_BASE` | GitHub Actions variable | Public HTTPS origin of the Netlify site, no trailing path |
| `DEMO_READ_TOKEN` | GitHub Actions secret | Read-only token for prepare checkout; removed before model/test execution |
| `DEMO_REPO_TOKEN` | GitHub Actions secret | Fine-grained repo token for publish branch/commit/pull-request operations |

Enable Netlify Identity and provision the PostgreSQL database before deploying. Use separate read and publish GitHub tokens. Configure the GitHub default branch as `main`, or update `DEMO_PUBLISH_BASE` in the workflow to the selected base branch. Configure the Netlify SPA/login to send bearer tokens; cookie-based mutations must include a matching `csrf_token` cookie and `X-CSRF-Token` header.

The schema is created idempotently at function startup. PostgreSQL transactions and row locks serialize workspace claims, approval consumption, event sequence assignment, and publish completion. The first migration must run under a DB role allowed to create these tables.

## Boundaries

- Only OpenRouter is accepted. The API performs a bounded two-token validation inference and stores the validated model, never the submitted key in plaintext.
- User-owned task reads and mutations use the verified Identity `sub`; request fields cannot select an owner.
- Worker endpoints require the separate worker token and do not accept user JWTs.
- Prepare captures a pinned base, tests, changed file contents, diff, test result, and digest. Publish reloads and verifies that exact candidate; it does not call the model.
- Publish is a pull-request operation only. No auto-merge, payments, Telegram, VNC, or alternate provider is implemented.
- Secrets and prompts are not printed in logs. The provider key is masked before the runner continues and is not available to the test subprocess.

## Local checks

Run `npm ci`, `npm run check:demo`, and `npm run test:demo`. The integration contract checks requiring live Netlify Identity, PostgreSQL, GitHub repository credentials, and an OpenRouter key must be completed by the owner after dashboard setup.

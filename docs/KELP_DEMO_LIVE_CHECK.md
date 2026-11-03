# Phone demo live check, 2026-11-03

Scope: `kelp-demo` only. No production settings or deployments were changed. No desktop VM changes, PRs, merges, force-pushes, or remote deletions.

## Live evidence

| Check | Result |
| --- | --- |
| Netlify MCP project | `kelp-demo`, site `5127f91f-0fa3-45b6-bf08-fe6bf864b477` |
| Production deploy | `6ae96636331bb258f51afe71`, ready, published `2026-11-03T04:42:34.112Z` |
| Deployed functions | `api-v1`, `demo-core`, `demo-worker`, all legacy runtime API version 1, Node 24 |
| `GET /mobile/` | HTTP 200, request `01M6G5VB7KNDYJ9B6Z1HXA9J81` |
| Signed-out `GET /api/v1/me` | HTTP 401 `unauthorized`, request `01M6G5VCEV10MV2NGP2Z1F0VD5` |
| Signed-out `GET /api/v1/workspace` | HTTP 401 `unauthorized`, request `01M6G5VDHZQXPXPH164C93R0CY` |
| Bearer diagnostic, deliberately invalid token | HTTP 500 `internal_error`, request `01M6G5XSFAKZVXW2EJZRAV7SYH` |
| Site environment | Netlify MCP environment read and CLI `getEnvVars` returned no site variables. CLI production Functions environment also returned empty. No variable values were printed. |
| Function log | `api-v1` error at `2026-11-03T07:45:36.592Z`: `demo API request failed { name: 'Error' }` |
| Showcase repository | Authenticated GitHub lookup for `Anshieee/kelp-merge-showcase` returned 404. Git remote lookup also reported repository not found. |
| Local showcase bundle | `git bundle verify /home/ansh/Downloads/kelp-merge-showcase.bundle` passed, complete history, main `2c522f363a2371341190a5b3c0dccad40fd86ab2` |

Signed-out HTTP 401 is required by the authenticated v1 facade. A signed-out HTTP 200 would not prove the signed-in checkpoint. No real signed-in session or credentials were available to this lane, so signed-in `/me` HTTP 200 and workspace opening remain FAIL / unverified. No browser UI success or complete phone-to-PR run is claimed.

## Failure boundary

The legacy deployed verifier reads `NETLIFY_IDENTITY_JWT_SECRET` before parsing a supplied bearer token. A missing secret therefore throws even for the deliberately malformed diagnostic token. Without that configuration error, this token would return 401 before any database access. The observed 500, empty site configuration, and source ordering identify the Identity-verification configuration path as the immediate blocker.

The original logs emit only `Error`, so they do not independently identify the setting. No schema error was observed or proven. `DATABASE_URL` is also missing from current site configuration and will block persistence after Identity verification. Database connectivity, TLS, and CREATE TABLE permissions cannot be checked without the owner's database connection. Deployed environment snapshots can differ from current settings; a new deploy is needed after owner configuration.

## Branch changes, not deployed

The modern `api-v1` default entrypoint uses supported server-side `@netlify/identity.getUser()` rather than asking the owner to extract a managed signing secret. It passes only the verified user identity to the existing route implementation. Cookie mutations retain CSRF checks. The direct event-handler verifier remains for local signature contract tests and is not the deployed entrypoint.

Missing required settings produce HTTP 503 `configuration_missing`, including only the field name. Logs record the safe failure stage (`identity`, `database`, `schema`, or `request`) and the setting name for configuration errors, never tokens, SQL text, connection strings, or arbitrary error messages.

`@netlify/identity` requires Node 22.12 or newer. Production already runs Node 24. The branch raises the declared minimum accordingly. Local Netlify function bundling confirms `api-v1` runtime API version 2 while the worker remains version 1.

## Exact owner steps

1. Review and deploy branch `kelp-demo-phone-fix` to **kelp-demo only**, without merging. The current site was deployed through the CLI and does not show a connected Git build. Production remains on the original deploy until an owner-approved deployment. Project deploys: https://app.netlify.com/projects/kelp-demo/deploys
2. Keep Identity enabled at https://app.netlify.com/projects/kelp-demo/identity. Use the supported managed-session verification in this branch. Do not fetch or invent `NETLIFY_IDENTITY_JWT_SECRET`, and do not substitute the Visitor access JWT secret.
3. Supply the following runtime fields at https://app.netlify.com/projects/kelp-demo/configuration/env with **Functions** scope and **Production** context. Redeploy after changing runtime variables. Source names below are intentional; secret values must stay in the dashboard/password manager.

| Field | Value source |
| --- | --- |
| `DATABASE_URL` | Owner's PostgreSQL provider connection details, TLS-enabled application connection URI. Initial database role needs permission to create the demo tables. |
| `DEMO_KEY_ENCRYPTION_SECRET` | Owner password manager, fresh random 32-byte AES key encoded as base64. Preserve the same value once provider keys are stored. |
| `DEMO_WORKER_TOKEN` | Owner password manager, independent random worker token of at least 32 characters. Same value must be supplied to the showcase Actions secret. |
| `GITHUB_REPOSITORY` | Verified showcase repository full name, after the repository exists and is seeded. |
| `DEMO_ALLOWED_REPOS` | Same verified showcase full name, one repository only. |
| `DEMO_ALLOWED_SUBJECTS` | Verified demo user's Identity ID from the Identity Users page or signed-in `/api/v1/me` after database setup. |
| `GITHUB_DISPATCH_TOKEN` | Owner-created fine-grained GitHub token scoped to the showcase repository with repository-dispatch permission (Contents write). |

4. The showcase currently returns 404, so the conditional seed push was not performed. Create an empty `Anshieee/kelp-merge-showcase` repository at https://github.com/new, with no README, license, or gitignore initialization. Then re-check that it is empty before pushing the verified bundle's `main`. Never overwrite a nonempty repository.
5. After seeding, set default branch `main` at https://github.com/Anshieee/kelp-merge-showcase/settings/branches. Set these Actions fields at https://github.com/Anshieee/kelp-merge-showcase/settings/secrets/actions and https://github.com/Anshieee/kelp-merge-showcase/settings/variables/actions.

| Field | Value source |
| --- | --- |
| `DEMO_API_BASE` variable | Public kelp-demo HTTPS origin, `https://kelp-demo.netlify.app` |
| `DEMO_WORKER_TOKEN` secret | Same owner-managed worker token used by Netlify |
| `DEMO_READ_TOKEN` secret | Owner-created separate read-only GitHub token for prepare checkout |
| `DEMO_REPO_TOKEN` secret | Owner-created separate fine-grained showcase token for Contents write and Pull requests write |

6. Sign in at https://kelp-demo.netlify.app/mobile/ and confirm `/api/v1/me` returns HTTP 200 for that real session. Set the user's entitlement, finish OpenRouter setup with the owner's private key, and confirm `/api/v1/workspace` returns ready and the workspace visibly opens. Then run one task through tests, review, explicit approval, and a real PR URL. Until those checks are evidenced, the checkpoint stays FAIL / unverified.

## Local checks

The two initial regression tests failed before the change (500 instead of 503 for missing configuration and 500 instead of 401 for malformed tokens). After the change: syntax check passed, backend suite 15 passed / 1 skipped, mobile contract suite 14 passed, and Netlify local function build passed. PostgreSQL contract tests were skipped because no test database was supplied. No production deploy was run.

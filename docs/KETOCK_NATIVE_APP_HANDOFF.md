# Handoff for Ketock — native app + v1 facade sign-off

Date: 2026-11-16. Base: `origin/main@2308ba2` (merge of PR #56). Status at writing: zero open PRs repo-wide.

## 1. Where `main` stands (your base)

`origin/main` is at `2308ba2`. Everything below is merged — build on top of it, do not reimplement it:

- **PRs #53 + #54:** service-control layer P0–P6 (service contract, provider neutrality, tenant boundaries, task-run records, per-tenant secrets, entitlements/billing hooks, admin visibility + support runbook) and phone demo sessions via managed Netlify Identity.
- **PR #56 (Storm, merged 2026-11-16):** desktop computer tab with the transport gate (managed Steel/Boat sessions by default, raw RFB only behind `VITE_COMPUTER_TRANSPORT=rfb-stub`), bridge allowlist, fail-closed server defaults, redacted session URLs. Reviewed twice (`CHANGES_REQUESTED` → fixes → `APPROVED`), all 6 CI checks green.
- **Yours, already merged:** #42 (phone PWA), #48 (mobile polish), #49 (Midnight/Meadow theme). The `phone/` PWA, `docs/HACKATHON_DEMO_CONTRACT.md`, and `docs/HACKATHON_DEMO_PROGRESS.md` were deliberately spared from cleanup for you — both docs are present on `main`.

## 2. What you own (two items)

**A. Native mobile app.** `phone/` today is an installable PWA (`make phone` → served at `/phone/`, paired once via root token → `POST /api/auth/session` device session, `HttpOnly; Secure; SameSite=Lax`). The pending item is the *native* app. Whatever shell you choose, it must speak the same `/api` surface over the same origin semantics — no second server, no new credential type (per `docs/PHONE_APP.md`).

**B. v1 facade sign-off.** `docs/HACKATHON_DEMO_CONTRACT.md` ("Kelp hackathon demo v1 API facade") defines the v1 surface: prefix `/api/v1`, managed-Identity-authenticated requests, and explicit non-scope (no workspace task-list/history endpoint, no computer-preview). Sign-off = your confirmation that the facade as documented matches what the native app needs — or your redlines if it doesn't. Nothing about the facade changes until you confirm.

## 3. Contracts you must conform to (non-negotiable, all on `main`)

- `docs/SERVICE_CONTRACT.md` + `docs/adr/0003-service-control-layer.md` — the frozen P0 contract.
- **Auth:** device sessions via pairing; demo sessions via managed Netlify Identity (PR #54). Never invent a credential type; never log/echo tokens (CI secret-scan enforces).
- **Tenancy:** `TenantContext` binds per request; body `tenant_id` mismatch is rejected pre-run (`chat/server.py`). A native client must not send or store any tenant override.
- **Provider neutrality:** generic `KELP_PROVIDER` / `BASE_URL` (empty default, fails closed) / `KELP_PROVIDER_API_KEY`. No vendor defaults, no hardcoded hosts — same rule as the `config.js` fix on PR #56 (placeholder `https://kelp.example.com`, per-build override, never a committed `http://127.0.0.1`).
- **Secrets/entitlements/admin:** per-tenant resolvers, scrubbed logs, entitlement codes, owner-only admin routes — your app consumes these, never bypasses them.

## 4. Proposed definition of done (confirm or redline)

1. Native app builds from current `origin/main`, talks to one configured server URL (fail-closed default).
2. Pair → chat → approvals → spend-glance works end to end against a real server; device appears in `GET /api/auth/devices` and revokes cleanly.
3. Full matrix green on your PR (ruff, mypy, pytest, secret scan, JS tests — all enforced by CI).
4. Independent review requested and findings resolved (same bar PRs #55/#56 met: `CHANGES_REQUESTED` rounds before approval).
5. Explicit v1 facade sign-off comment on your PR: "facade confirmed" or attached redlines.
6. No new credential types, no committed secrets/hosts, no force-push, plain merge after green CI + review.

## 5. Why this is the critical path

The Steel/Boat pilot gate (owner-set, not technical) clears only when **both** Storm's desktop app **and** your native app are delivered and tested end to end. Storm's half merged 2026-11-16. The pilot plan (`pilot/steel-boat`, local-only) stays parked until your sign-off lands — then it gets rebased and runs.

## 6. Working agreement

Branch from `origin/main`, keep the merge discipline (no force-push, no remote deletions), never print secrets, and flag anything ambiguous early — the review rounds on #55/#56 showed the process catches real gate violations (transport bypass, credential-store drift) before they ship.

## 7. Lane status

Nothing of yours is open (zero open PRs repo-wide) and `kel-30-credential-target-binding` (Vaktresh's lane) is not yours — no overlap. Your lane is clear whenever you're ready.

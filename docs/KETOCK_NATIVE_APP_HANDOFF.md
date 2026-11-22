# Handoff for Ketock — native app

Date: 2026-11-16. Base: `origin/main@2308ba2` (merge of PR #56).
Rev 2026-11-22: demo client retired (owner decision) — §2B struck, DoD revised to five items, file references corrected.

## 1. Where `main` stands (your base)

`origin/main` is at `2308ba2`. Everything below is merged — build on top of it, do not reimplement it:

- **PRs #53 + #54:** service-control layer P0–P6 (service contract, provider neutrality, tenant boundaries, task-run records, per-tenant secrets, entitlements/billing hooks, admin visibility + support runbook) and phone demo sessions via managed Netlify Identity (demo flow, retired with the demo client — history only).
- **PR #56 (Storm, merged 2026-11-16):** desktop computer tab with the transport gate (managed Steel/Boat sessions by default, raw RFB only behind `VITE_COMPUTER_TRANSPORT=rfb-stub`), bridge allowlist, fail-closed server defaults, redacted session URLs. Reviewed twice (`CHANGES_REQUESTED` → fixes → `APPROVED`), all 6 CI checks green.
- **Yours, already merged:** #42 (phone PWA), #48 (mobile polish), #49 (Midnight/Meadow theme). The `phone/` PWA was deliberately spared from cleanup for you. Note: `docs/HACKATHON_DEMO_CONTRACT.md` remains in the tree but the demo client and facade are retired (see §2) — not your lane. (An earlier revision of this handoff also referenced a `HACKATHON_DEMO_PROGRESS.md`; that file never existed — corrected here.)

## 2. What you own (one item — was two; facade struck on demo retirement)

**A. Native mobile app.** `phone/` today is an installable PWA (`make phone` → served at `/phone/`, paired once via root token → `POST /api/auth/session` device session, `HttpOnly; Secure; SameSite=Lax`). The pending item is the *native* app. Whatever shell you choose, it must speak the same `/api` surface over the same origin semantics — no second server, no new credential type (per `docs/PHONE_APP.md`).

**B. v1 facade sign-off — STRUCK 2026-11-22.** The demo client is retired (owner decision): the facade contract governed the demo backend, which the native app never touches, so there is nothing to sign off. No replacement item.

## 3. Contracts you must conform to (non-negotiable, all on `main`)

- `docs/SERVICE_CONTRACT.md` + `docs/adr/0003-service-control-layer.md` — the frozen P0 contract.
- **Auth:** device sessions via pairing (PR #54's demo-session flow is retired with the demo). Never invent a credential type; never log/echo tokens (CI secret-scan enforces).
- **Tenancy:** `TenantContext` binds per request; body `tenant_id` mismatch is rejected pre-run (`chat/server.py`). A native client must not send or store any tenant override.
- **Provider neutrality:** generic `KELP_PROVIDER` / `BASE_URL` (empty default, fails closed) / `KELP_PROVIDER_API_KEY`. No vendor defaults, no hardcoded hosts — same rule as the `config.js` fix on PR #56 (placeholder `https://kelp.example.com`, per-build override, never a committed `http://127.0.0.1`).
- **Secrets/entitlements/admin:** per-tenant resolvers, scrubbed logs, entitlement codes, owner-only admin routes — your app consumes these, never bypasses them.

## 4. Proposed definition of done (confirm or redline)

1. Native app builds from current `origin/main`, talks to one configured server URL (fail-closed default: no URL configured → setup/pairing screen, zero network traffic — no baked-in default host anywhere).
2. Pair → chat → approvals → spend-glance works end to end against a real server; device appears in `GET /api/auth/devices` and revokes cleanly.
3. Full matrix green on your PR (ruff, mypy, pytest, secret scan, JS tests — all enforced by CI; proposed: you draft the native-shell workflow hunk in your PR, owner lands it — confirm or redline).
4. Independent review requested and completed, with all findings resolved.
5. No new credential types, no committed secrets/hosts, no force-push to `main` or anyone else's branches (`force-with-lease` on your own fork's PR branches stays the rebase workflow), plain merge after green CI + review.

## 5. Why this is the critical path

The Steel/Boat pilot gate (owner-set, not technical) clears only when **both** Storm's desktop app **and** your native app are delivered and tested end to end. Storm's half merged 2026-11-16. The pilot plan (`pilot/steel-boat`, local-only) stays parked until your native app lands and is tested — then it gets rebased and runs.

## 6. Working agreement

Branch from `origin/main`, keep the merge discipline (no force-push to `main` or anyone else's branches — `force-with-lease` on your own fork is the rebase workflow; no remote deletions), never print secrets, and flag anything ambiguous early — the review rounds on #55/#56 showed the process catches real gate violations (transport bypass, credential-store drift) before they ship.

## 7. Lane status

Nothing of yours is open except this handoff PR (#57) itself, and `kel-30-credential-target-binding` (Vaktresh's lane) is not yours — no overlap. Your lane is clear whenever you're ready.

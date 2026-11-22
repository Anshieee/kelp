# Phone app handoff — what it can and cannot do

Date: 2026-11-22. Branch: `kel-29-phone-site-match` (site theme, blob + grain,
payment sheet, stream live-update fix). Baseline: full suite 1246 passed + 4
skipped, phone vitest 75 passed across 7 files (pre-existing chat/api/
metrics suites; wallet suite incl. key-lifecycle + lookalike-ref refusal;
stream suite incl. named-dispatch + onerror-guard regressions; payment
component suite pinning the receipt-status branching), `npm run build`
clean, `ruff` clean. Every claim below was observed live against a local
server unless marked otherwise. Live API sweep 36/37 on an isolated DB;
the single miss was a one-time `/readyz` 503 on the first call ~3s after
cold boot (unreproducible in 5 follow-ups with a fully green checks
payload) — almost certainly a warm-up race, and defensible either way
since readiness is allowed to refuse until checks pass. Refs:
https://github.com/Anshieee/kelp/issues/2 (KEL-29 tracking issue — items
2, 4, 5: scheduler counter fix, device audit rows, share projection).

## How to run it

```bash
make phone        # npm ci && npm run build -> phone/dist/ (gitignored)
make serve        # loads .env, serves :8765, mounts the app at /phone/
```

Needs in `.env`: `KELP_AUTH_TOKEN` (invent it; typed once into Pair),
`KELP_PROVIDER_API_KEY` (real key for real turns; dummy key exercises only
the failure paths), `KELP_PROVIDER_BASE_URL` (documented default is fine).
Open `http://127.0.0.1:8765/phone/`, pair, go. Real phones need HTTPS
(Tailscale/reverse proxy) — the session cookie is `Secure`.

## What works right now (all observed live)

- **Pairing & sessions.** Root token → `201` + session/CSRF cookies → device
  appears in `GET /api/auth/devices` with `current: true`. Per-device
  revoke, self revoke, nuke-all (then calls 401), re-pair — all verified.
  Wrong CSRF → refused. Unknown device/thread ids → indistinguishable 404s.
- **Threads.** Create (details + `?after=` cursor).
- **Chat, live.** Thread screen opens `GET /api/threads/{id}/stream`
  events arrive and render **without navigating away** (verified: failure
  text appears in place, pill stays `Live`). Root cause of the old
  switch-chats-to-update bug, fixed here: the server names every SSE frame
  (`event: token_delta`, …) and the app previously listened only via
  `onmessage`, which never fires for named frames; plus a server-sent
  `event: error` frame was misread as a transport failure, pinning the
  pill on "Retrying". Both fixed in `phone/src/stream.ts`, pinned by
  regression tests that emulate real browser dispatch.
- **Turns.** Create → 202, concurrent second turn → 409, retry and stop
  endpoints answer. With a dummy provider key the turn fails fast and the
  failure renders inline with retry affordance — the correct fail-closed
  path, verified end to end.
- **Approvals.** Pending list renders with Approve/Deny; "Review purchase >"
  appears only on cards carrying a wallet proposal id; decisions are
  single-use (double-decide is a clean 409 path server-side).
- **Devices screen.** Lists all sessions with online/current flags,
  per-device revoke, sign-out; matches the server's device registry,
  including desktop sessions.
- **Sharing, artifacts, runs, admin, metrics.** Create share → anonymous
  frozen snapshot → revoke → link dead; real CSV upload + byte readback
  (only `pdf`/`csv` accepted — `.txt` correctly 415s); runs create/list/get
  with `tenant: "owner"` linkage (202); admin summary owner-readable,
  anonymous refused; `/healthz` public, `/readyz` + `/metrics` owner-only.
  Tenant scoping is server-default (single `owner` tenant, no client
  surface) and holds across all of the above.
- **Wallet UI (needs Stripe keys for real charges).** Full sheet flow
  verified with contract-faithful fixtures: Wallet home (cards + caps
  glance) → New payment (amount/currency/payee + cap pre-check) → review
  sheet (big amount, payee, expandable Amount/Payee/Card/Caps/Window/
  Idempotency rows, card picker, dark Approve + Cancel) → receipt (amount,
  last4, timestamp, order id). Without server Stripe config every wallet
  call 503s `wallet_not_configured` and the app says so with no dead
  buttons. Real `POST /api/wallet/propose` + `approve` shapes confirmed
  against the live server up to the configuration gate.
- **Look & install.** Site-language light theme by default, Midnight dark
  option (Meadow retired), sprout + serif wordmark in chrome, living
  topo-blob empty state (real site artwork, transform-only motion, frozen
  under reduced-motion), film grain overlay, 44px targets, safe areas.
  PWA: manifest, v2 service worker (shell only, never `/api`), icons
  including maskable. Keyboard-shrink keeps composer + Send visible.
- **Rate limiting.** Unit-covered (52 tests); observed live once as clean
  `rate_limited` JSON with integer `Retry-After` during an accidental
  burst — the gate works, clients back off via the app's 1s/2s/4s/15s
  reconnect ladder.

## What it cannot do yet (honest gaps, each with its unblocker)

1. **Real model turns.** Dummy key = failure path only (verified working).
   Unblock: real `KELP_PROVIDER_API_KEY` → re-run any chat turn.
2. **Live wallet charges.** Needs Stripe test keys server-side; full
   propose→charge→receipt→idempotent-replay then provable live. Unit suite
   (243 payments tests) + HTTP signature-gate matrix already green.
3. **GitHub PRs from chat.** Three stacked gaps, all verified: `github.read`
   is an unimplemented stub (always `not_implemented`); `github.write`
   manifest allowlist is `["Anshieee/kelp"]` only (other repos
   `policy_denied`); no `GITHUB_TOKEN` in server env. Fix = implement the
   read skill + widen the allowlist (policy decision) + token in env; then
   the designed flow works (ask → approval card → approve → PR opens).
4. **Cross-turn memory.** Each turn sends exactly the current message
   (`run_turn([{"role": "user", "content": text}])` — verified in code);
   history is stored but never loaded back, so the assistant re-asks for
   given details. Fix designed (bounded history window in `_run_turn`,
   with token cap and the wider-transcript privacy acceptance), not
   implemented.
5. **Live Razorpay settlement.** Needs test-mode keys + public callback
   URL. Signature gate + unknown-order + no-secret paths proven live;
   settlement/replay proven in unit tests only.
6. **Physical-device testing.** Everything above is headless Chromium
   (390×844, touch emulation). Real iPhone/Android checks outstanding:
   Add-to-Home-Screen icon + reopen restores state, on-screen keyboard in
   hand, narrow viewports in hand.
7. **CI for `phone/`.** `ci.yml` doesn't build or test the app (owner's
   file). Python side is fully gated; JS side is `npm run build` + `npm test`
   run manually.
8. **Desktop interop.** Separate app, shared server only. No cross-device
   UI in either direction (desktop has no devices screen; phone lists all
   devices already). QR/code pairing from desktop needs a new server
   endpoint — spec-only, needs Ansh + security review.
9. **Long-horizon behaviors.** Scheduler ticks over real time, spend caps
   over real usage windows, approval expiry in hand — unit-covered
   (67 + 26 tests), not live-observed.

## Files that matter for reviewers

- `phone/src/stream.ts` — the live-update fix (named SSE listeners +
  MessageEvent-vs-transport-error guard) + `SERVER_EVENT_TYPES` contract.
- `phone/src/__tests__/stream.test.ts` — regression tests using a
  browser-faithful fake (named dispatch, never `onmessage`); the old fakes
  encoded the same wrong browser model and were fixed to match reality.
- `phone/src/wallet.ts`, `screens/Wallet.tsx`, `screens/Propose.tsx`,
  `components/PaymentSheet.tsx`, `__tests__/wallet.test.ts` (30 tests) —
  the payment flow; every row maps to a real endpoint field, nothing
  invented (no delivery address: Kelp has none).
- `phone/src/components/LivingBlob.tsx`, `public/blob.png` — real site
  artwork (captured frame), transform-only motion, reduced-motion frozen.
- Docs: this file + the Wallet section in `docs/PHONE_APP.md` +
  attribution in `phone/NOTICE.md`.

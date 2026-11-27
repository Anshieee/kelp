# Native-phone foundation — status

**Date:** 2026-11-26. **Audit basis:** hosted phone and server source at
`5f55ebe` and the accepted Linux desktop scope at `670e1b0`. This is a status
note, not a new pairing or relay contract. It records source and test coverage;
it does not report a deployed or native-phone validation run.

## 1. Summary

This repository contains a **hosted web PWA**
(`phone/`): a browser app served from the Kelp server's own origin, paired once
with the root bearer, then using the cookie/CSRF/SSE session surface. The
implemented, test-covered paths below are a reusable hosted session foundation.

Everything the native-phone direction needs on top — device-key QR pairing,
a push relay with APNs/FCM app-publisher credentials, end-to-end encryption, a
signed remote-decision binding, and a per-device biometric-gated decision key —
is **planned only in this repository** (L6 in the threat model; ADR 0004 item
10). It conflicts with the older "one server, no new credential type" handoff.

## 2. What exists and may be reused (hosted, verified in source)

All of the following is implemented, committed, and test-covered. Paths are
exact.

- **Session/pairing.** `POST /api/auth/session` is a **bearer-only** sign-in:
  the raw session id exists only in the client's cookie, and a cookie can never
  mint another session (`chat/routers/auth.py`, `_sign_in`; `docs/CHAT_SPEC.md`
  §12.2). The root token is used once and dropped from the pairing screen
  (`phone/src/screens/Pair.tsx`). Response carries `session.expires_at`,
  `session.csrf_token`, `session.device { id, label }`.
- **Cookies.** By default, `_session_cookies` emits a session cookie `HttpOnly; Secure;
  SameSite=Lax` (30-day rolling `Max-Age=2592000`) plus a CSRF cookie (not
  `HttpOnly`) — `chat/routers/auth.py`, `_session_cookies`; pinned by
  `tests/test_auth_sessions.py::test_sign_in_sets_httponly_secure_samesite_cookie_with_30_day_expiry`.
- **CSRF.** Double-submit pair: per-session `csrf` cookie + `X-CSRF-Token`
  header, both derived via `HMAC(secret, id_hash)`; cookie mutations also
  require a same-origin `Origin` (`_csrf_ok`, `_authorize`). Tested in
  `tests/test_auth_sessions.py` (`test_cookie_mutation_requires_session_csrf_pair`,
  `test_cookie_mutation_without_origin_is_rejected`).
- **Devices / sign-out / replay semantics.**
  - `GET /api/auth/devices`, `DELETE /api/auth/devices/{id_short}`
    (per-device revoke), `DELETE /api/auth/session` (revoke the presented
    session and clear both cookies), `DELETE /api/auth/sessions` (nuke-all) —
    `chat/routers/auth.py`.
  - Revoking another device closes that device's open SSE stream on its next
    fresh auth check; revoking the caller clears its cookies.
  - Approvals are single-use with a 600 s TTL (`openshell/approvals.py`,
    `APPROVAL_TTL_SECONDS`) and an `argument_hash` match enforced in
    `chat/service.py`; double-decide is a clean 409. This is the hosted
    *local* approval path — it is **not** a signed remote decision.
  - Tests: `tests/test_auth_sessions.py` (17 tests),
    `tests/test_devices.py` (6 tests, incl. session-cap eviction, scrub,
    cross-restart CSRF validity).
- **SSE.** `GET /api/threads/{id}/stream` frames named events (`token_delta`,
  `tool_call_started`, `approval_requested`, `approval_resolved`, …) replayed
  by `seq` with an `?after=` cursor; `chat/routers/stream.py` + the client in
  `phone/src/stream.ts`. Phone-side parse/seq/reconnect logic and the
  named-event fix are pinned by `phone/src/__tests__/stream.test.ts`.
- **Phone client contract.** Same-origin relative `/api` calls,
  `credentials: "include"`, `X-CSRF-Token` on every mutation, 401 clears the
  session back to pairing (`phone/src/api.ts`); session/CSRF kept in
  `sessionStorage` only, token never stored (`phone/src/state.ts`). Tests in
  `phone/src/__tests__/api.test.ts`.
- **Desktop-side counterpart (owned by the desktop app, not the phone).** The
  Rust bridge `desktop/src-tauri/src/bridge.rs` owns the cookie jar, the CSRF
  pair, and the SSE stream; `bridge_sign_in` uses the bearer once. This is the
  same hosted session surface, reused by the desktop.

**Static PWA limitations (source audit).** The server mounts `/phone/` when
built assets are present. `chat/phone.py` defaults to `phone/dist`, and the
default `Dockerfile` neither builds nor copies that directory. The service
worker caches the app shell only, never `/api` responses (`docs/PHONE_APP.md`).
The PWA has no push subscription, background push handling, or device key.

## 3. What is missing (native — planned only, no code)

The following native-specific mechanisms are not implemented in this
repository. Existing hosted expiry, single-use and hash checks are not a
signed remote decision path.

- **Device-key QR pairing.** L6 specifies pairing as one QR scan from the
  desktop app, a short-lived single-use QR secret shown only after a local user
  action, and a **device key created at pairing**. Nothing in `phone/`,
  `desktop/`, or `chat/` implements QR scanning, a device key, or a device-key
   register/replace flow. T7's planned desktop permission-mode key is distinct
   from the L6 phone key.
- **Push relay.** The relay is a hosted component required because APNs/FCM
   accept only pushes signed with the app publisher's credentials
   (L6). This repository has no relay implementation or self-host package.
- **Owner / app publisher signing.** The checked-in phone client is a PWA.
   Relay ownership and publisher credential custody require coordination; this
   audit did not check external app-publisher accounts.
- **End-to-end encryption.** L6 requires both directions between the paired
  phone and the brain to be E2E-encrypted with the relay seeing only ciphertext
  and timing. No such key material or channel exists.
- **Signed approval binding.** L6 requires each remote decision to be bound to
  (card id, `argument_hash`, expiry) with the brain rejecting replays and
  mismatches. Today's approvals enforce expiry and single-use locally and match
  `argument_hash`, but there is no remote signed-decision path and no
  signed-payload nonce/replay protection for it.
- **Decision-key biometric gate.** L6 requires the phone's decision key to be
  usable only after the device biometric or passcode (mandatory at least for
  approving the start of screen control, S8). Nothing implements it.
- **Per-brain relay credential** and **stored paired-device records** (L5/L6)
  on the brain side.

## 4. Conflict and reconciliation

The native direction from ADR 0004 **conflicts** with the existing handoff
constraint:

- `docs/KETOCK_NATIVE_APP_HANDOFF.md` (§2/§3) and `docs/PHONE_APP.md` require
  **no second server and no new credential type**, and the native-app
  definition of done requires **one configured server URL** with fail-closed
  default (handoff §4, item 1).
- ADR 0004 L6 requires a **push relay** — a hosted component outside the brain's
  trust boundary — plus a **device key**, a **per-brain credential**, and
  **E2E keys** (L5), all of which are new credential material and a second
  server-side component.

ADR 0004 itself records this tension in its follow-ups: "Reconcile it with
Ketock's native app handoff, which says no second server and no new credential
type, and whose definition of done says one configured server URL." This is an
open reconciliation item in the ADR. Consider it alongside the native-app
handoff and [GitHub issue #76](https://github.com/Anshieee/kelp/issues/76).
This source audit did not fetch the issue or establish that it tracks the
conflict, that anyone has replied, or that a reconciliation has been approved.

Also unbuilt but already decided as product direction: the push relay and the
native device key remain on the **brain/desktop/phone** side, never on the Boat
tunnel (L6). The phone and the Telegram bridge do not use the Boat SSH tunnel.

## 5. Scope boundaries (what this slice is not)

- **No raw Local loopback for the phone.** The phone cannot reach a `localhost`
  loopback sidecar; the session cookie is `Secure`, so a remote device needs
  HTTPS/reverse proxy even for the hosted PWA (`docs/PHONE_APP.md`). The Local
  sidecar is a desktop-only path, not a phone API.
- **No desktop Boat tunnel / WAN-direct phone API.** The phone does not reach a
  brain over an SSH forward or a public WAN URL. Remote approval is planned only
  via the (unbuilt) relay.
- **Native phone before Telegram.** ADR 0004 item 10 and threat model L6/L7
  order push to the native app first and a Telegram bridge second. Telegram is
  the secondary channel.
- **Telegram cannot authorize screen-control starts.** The L7 remote-channel
  rule (applies to the phone too via L6) forbids Telegram from starting or
  approving screen control (S8). The paired phone *may* approve starting screen
  control with a signed decision under L6 — but only once L6 exists.
- **Screen features are outside this slice.** Screen control (S8), screen
  frames, and screen reactions are separate work (desktop capture/control), not
  part of the native-phone foundation.

## 6. Platform milestone note (Linux-only current milestone)

The current desktop milestone is **Linux-only**, per [PR #81](https://github.com/Anshieee/kelp/pull/81) (commit
`670e1b0`, "docs: make the current Kelp desktop milestone Linux-only"): Phase 0
desktop validation and current Phase 1 desktop development are Linux-only; `dmg`
and `msi` installers are deferred to future macOS and Windows platform
milestones, and macOS signing/notarisation is deferred to that platform
milestone (`docs/PRODUCT_BRIEF.md`; `docs/adr/0004-desktop-companion.md`).
This status note makes **no claim that the desktop or native-phone app works on
macOS/Windows**, and no installer claims.

## 7. Source map

| Claim | Evidence |
|---|---|
| Hosted bearer pairing, cookie/CSRF session | `chat/routers/auth.py` (`_sign_in`, `_csrf_ok`, `_session_cookies`, `_revoke_*`); `docs/CHAT_SPEC.md` §12.2 |
| Phone PWA client | `phone/src/api.ts`, `phone/src/stream.ts`, `phone/src/state.ts`, `phone/src/screens/Pair.tsx`, `phone/src/screens/Settings.tsx` |
| Phone client + SSE tests | `phone/src/__tests__/api.test.ts`, `phone/src/__tests__/stream.test.ts` |
| Hosted session/device/replay tests | `tests/test_auth_sessions.py`, `tests/test_devices.py` |
| Desktop bridge owns cookie jar/CSRF/SSE | `desktop/src-tauri/src/bridge.rs` |
| Native-phone direction (planned) | `docs/adr/0004-desktop-companion.md` item 10; `docs/THREAT_MODEL.md` §6 L6/L7, §7 S8, §9 T7/T8 |
| Conflict / reconciliation | `docs/KETOCK_NATIVE_APP_HANDOFF.md` §2–§4; ADR 0004 follow-up |
| Linux-only milestone | PR #81 (`670e1b0`); `docs/PRODUCT_BRIEF.md`; ADR 0004 |

## 8. `expires_at` type note (possible discrepancy; not fixed)

Directly validated by reading both sides of the code: the server's sign-in
response serialises `"expires_at"` as an integer epoch (`int(record.expires_at)`
in `chat/routers/auth.py`, `_sign_in`), while the phone client declares the
field as `string | null` (`phone/src/api.ts:53`, `PairResponse.session.expires_at`;
`SessionInfoLike.expiresAt`) and re-reads it from `sessionStorage` only when it
is a string (`phone/src/state.ts:51`). A numeric value would therefore not
survive a reload as a string. This is **not fixed** here. No current phone UI
render of the session field was found; authentication and authorization
depend on the server-managed session cookie, not `SessionInfo.expiresAt`.

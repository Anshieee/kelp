# Desktop app + desk pet — scope (v1)

> **Owner decisions (locked, 2026-10-30):**
> 1. **Tauri thin client** — small binary, OS webview, keychain API, token in OS keychain only.
> 2. **One Nebius-hosted server** — exactly one configured server URL (`desktop/src/config.js`), no bundled sidecar, no runtime URL switcher.
> 3. **Interactive pet** — notifications plus one-tap Approve/Deny (server caps enforced server-side, receipt in-app), click-to-open, mute; on all workspaces, quiet under OS do-not-disturb, reduced-motion respected.
>
> **Settled v1 surface (two grill rounds, locked):**
> - IN: chat + streaming, approval cards, devices list + revoke, spend readout.
> - HIDDEN ENTIRELY (no dead buttons): shares UI, dedicated schedules screen.
> - Auth: one bearer exchange, then OS-keychain session only.
> - Spend mirrors the server: warn at 80% of cap, plain sentence at 100%; amounts + card last4 only, never card details.
> - Unsigned dev builds only. Diagnostics are local logs only, no telemetry.

## What they are

- **Desktop app:** a thin client over the existing Kelp server (`chat/api.py`). It talks to a server URL, signs in with the root bearer once, and keeps a session cookie after that. No chat logic moves into the client.
- **Desk pet:** an always-on-top frameless overlay reusing the landing orb (`landing/e-fx.js` `initOrb`, three flavours, blink plus glance cadence, pointer gaze, CSS `e-orb-face` fallback). It mirrors live assistant state, nothing more.

## App surface (v1)

| Screen | Backed by |
|---|---|
| Threads + streaming chat | `POST /api/threads`, `/api/turns`, SSE `/api/stream` |
| Approval cards (Approve / Deny) | `POST /api/approvals/{id}/decide` over the KEL-7 gate |
| Schedules list | `schedule.create` tasks from `var/scheduler` via chat surface |
| Devices (list + revoke) | `GET /api/auth/devices`, `DELETE /api/auth/devices/{id}` |
| Shares (create + revoke) | `POST /api/threads/{id}/share`, `DELETE` |
| Spend readout | `/api/metrics` spend gauges, 402 copy stays server-side |

Auth follows `CHAT_SPEC.md` §12.2: bearer exchange, `HttpOnly; Secure; SameSite=Lax` cookie, double-submit CSRF. No token storage outside the OS keychain.

## Pet behaviour (v1)

- States driven by the live SSE stream: `working` (turn running), `waiting` (approval pending), `done` (turn finished), `idle` otherwise. Flavour morphs on the same ~2s cadence as the landing orb.
- Click pet: opens (or focuses) the app. Right-click: mute motion, quit.
- Respects OS reduced-motion (CSS face only) and never shows text beyond a tooltip.

## Recommended stack

**Tauri thin client.** Small binary, real OS webview, keychain API for the token, transparent frameless window support for the pet. Alternative: Electron (heavier, wider plugin ecosystem). Not v1: bundling the Python server as a sidecar (packaging + signing cost, restart/lock semantics duplicated); v1 points at a server the owner already runs (`make serve` locally or the Nebius host).

## Build list (when green-lit)

1. `desktop/` scaffold (Tauri + webview UI: threads, stream render, approval cards).
2. `desktop/pet` overlay window (orb canvas port, SSE state hookup).
3. Keychain token store + session sign-in flow.
4. Packaging: signed installers, auto-update note.
5. Tests: auth flow, approval round-trip, SSE reconnect, pet state mapping.

## Out of scope for v1

Bundled server sidecar, offline mode, voice input, plugin UI, share rendering (links open in browser), payments UI (approve-only via cards), Windows-on-ARM packaging.

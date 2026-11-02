# Kelp phone app

A phone-first installable PWA that is the Kelp chat client: threads, streaming
turns, approvals, devices, and a server glance — served **same-origin** from the
Kelp server under `/phone/`, talking to the JSON API at `/api/*` with relative
URLs only.

## Build

```sh
npm install
npm run build     # tsc --noEmit + vite build -> dist/
npm test          # vitest run
npm run icons     # regenerate public/icons/*.png (no image deps)
```

`npm run dev` starts Vite on :5173 and proxies `/api` to a locally running
`python -m chat.server` (:8765) for development.

## Where it mounts

The Kelp server serves `phone/dist/` at the `/phone/` path prefix (same origin
as `/api`). The build is configured with `base: '/phone/'`, so every asset URL
resolves under the mount. The service worker (`/phone/sw.js`) precaches the app
shell, is cache-first for static assets, and never touches `/api/*`.

## Auth model

The root `KELP_AUTH_TOKEN` is used exactly once, on the pairing screen
(`POST /api/auth/session` with `Authorization: Bearer …`). It is never stored.
Afterwards the browser holds the HttpOnly session cookie; the app keeps only
the CSRF token in `sessionStorage` and sends it as `X-CSRF-Token` on every
mutation. Any 401 returns the app to the pairing screen.

## Layout

- `src/api.ts` — typed fetch client (pairing, threads, turns, approvals, metrics, devices)
- `src/stream.ts` — SSE tail with `after=` reconnect + seq dedupe (pure logic unit-tested)
- `src/chatState.ts` — snapshot + stream-event projection (token deltas, tool cards, approvals)
- `src/state.ts` — session (csrf in sessionStorage) + thread-list cache
- `src/screens/` — Pair, Threads, Chat, Settings
- `src/components/` — ApprovalCard, Composer, MessageList, StreamGlance
- `scripts/make-icons.mjs` — generates the PWA icons (pure Node, zlib PNG encoder)

# The phone app

Kelp's phone app is an installable web app (a PWA): the same chat, approvals,
and spend glance as the dashboard, laid out for a phone, installed from the
browser to the Home Screen. It talks to the Kelp server's own `/api` surface
over the same origin, so pairing, sessions, CSRF, and streaming work exactly
as the chat spec defines them — there is no second server and no new
credential type.

This follows the pattern OpenMausBot uses for its phone client (Apache 2.0;
see `phone/NOTICE.md` for what was adapted): the app is the browser UI
installed to the Home Screen, paired once, and revocable like any other
device.

## Build and serve

```bash
make phone        # npm ci && npm run build -> phone/dist/
make serve        # python -m chat.server (serves /phone/ from phone/dist)
```

The server mounts the build at `/phone/` automatically when the build exists
(override the directory with `KELP_PHONE_ROOT`). Without a build the mount
answers one JSON 404 (`phone_not_built`) telling you to run `make phone`.
The mount is read-only and serves no owner data; every `/api` call the app
makes enforces the owner session.

## Pair a phone

1. Reach the server from the phone over HTTPS or your tailnet/LAN address —
   a phone cannot use a `localhost` link. The same reverse-proxy rules as
   `docs/DEPLOY.md` apply; the session cookie is `Secure`, so plain HTTP
   from a remote device will not hold a session.
2. Open `https://<your-server>/phone/`. The pairing screen asks for the
   server's root token (`KELP_AUTH_TOKEN`) once.
3. Submitting exchanges the bearer for a device session
   (`POST /api/auth/session`, device label `Phone`): the cookie is set
   `HttpOnly; Secure; SameSite=Lax`, and the root token is dropped from
   memory immediately. The phone now appears in `GET /api/auth/devices`
   and can be revoked from any other device, exactly like a desktop session.
4. Install it: on iPhone, Safari's Share menu → **Add to Home Screen**; on
   Android, Chrome's menu → **Install app**. It opens standalone, with the
   Kelp icon and no browser chrome.

## What the app shows

| Screen | What it does |
|---|---|
| Pair | One-time root-token exchange for a device session. |
| Threads | Recent threads, new chat, rename, delete, and a badge for pending approvals. |
| Chat | Messages with live streaming (SSE, resumed by `seq` on reconnect), the composer with the 8,000-character cap, stop and retry, and pending approval cards with Approve/Deny. |
| Settings | Install hint, signed-in devices with per-device revoke, sign out, and a spend glance from `/api/metrics`. |

Model text renders as plain text — no HTML, no Markdown injection — matching
the chat spec's XSS rules. The service worker caches the app shell only;
`/api/*` responses are never cached, so nothing owner-owned outlives the
session.

## Security notes

- The root bearer token is used once, at pairing, and never stored — the
  session cookie is the credential afterwards, and the CSRF token lives in
  `sessionStorage` for the tab's lifetime.
- Revoking the phone's device session (from the phone's Settings screen or
  from any other signed-in device) cuts access immediately; the next API
  call returns 401 and the app returns to the pairing screen.
- The app shell itself is public and contains no owner data; treat the
  server address as the only secret-free part of the URL space.

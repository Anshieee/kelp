# Kelp desktop app + desk pet (Tauri thin client)

Thin client over the Kelp server (`chat/api.py`). The React shell talks to the
server through the Rust bridge (`src-tauri/src/bridge.rs`), which owns the
cookie jar and the per-thread SSE stream; the webview only invokes commands
and listens for `chat-event` frames.

- **Main window** (`src/index.html`, `src/App.tsx`): a ChatGPT-shaped shell —
  history sidebar grouped by day, conversation column, composer. Chat and
  Computer tabs; closing it hides it rather than destroying it, so the orb
  and panel stay live and "Open full app" always brings it back.
- **Command panel** (`src/overlay.html`, `src/OverlayPanel.tsx`): transparent,
  frameless, always-on-top, opening to the **left of the orb** with bottom edges
  aligned. Compact it is just a white input pill, 400 wide; expanded it keeps
  that width and adds a white conversation surface below. Summoned by clicking
  the orb or `CommandOrControl+Shift+Space`. The expand button opens the full
  app once already expanded; clicking outside hides the panel.
  The screen button is inert: it opens an explainer, and no capture code exists.
- **Pet overlay** (`src/pet.html`, `src/pet.js`, `src/orb.js`): the avatar, at
  the bottom right of the primary monitor — always-on-top, frameless, 96×96, no
  shadow and no drag region, so only the disc takes clicks. Tapping it toggles
  the panel. Vanilla on purpose — no React.
- **Server URL** is one constant (`src/config.js`) pointing at the Kelp host.
  No runtime switcher, no per-environment list.

## Develop

```sh
cd desktop
npm install
npm run dev         # vite dev server (open / and /overlay.html)
npm run typecheck   # tsc --noEmit
npm run build       # typecheck + vite build (four entries)
npm run pet:dev     # pet overlay only
```

### First-run setup (in progress, #83)

`setup.html` is the setup and recovery window. The shell does not open it
yet: the Rust side (`setup_status`, `setup_choose`, `setup_retry`,
`setup_quit`, and the `setup-status` event) lands separately. The contract
and its rules live in `src/setup.ts`.

To review the screen in a browser, run `npm run dev` and open
`/setup.html?setup=<scenario>`, where the scenario is `first-run`,
`local-only`, `broken`, `guard`, `missing`, or `garbage`. That fake only
loads in dev builds outside the shell; see `src/setupFake.ts`.

Rust/Tauri shell:

```sh
cd desktop/src-tauri
cargo build
```

## Tests

Python contract tests (run from repo root):

```sh
pytest tests/test_desktop_app.py -q
```

JS unit tests (no deps, `node --test`):

```sh
node --test desktop/tests/
```

## Auth

One bearer exchange, then the Rust bridge owns the session. The webview holds
the bearer in React state only while the sign-in dialog is open and clears it
on both submit paths; the only wire it ever travels is the single
`bridge_sign_in` invoke, which POSTs to `/api/auth/session` from Rust. After
that the cookie jar in `bridge.rs` authorizes every call and the CSRF
double-submit pair guards mutations — no routine request carries the bearer.
Nothing is written to localStorage, disk, or logs: `bearer_persistence.test.mjs`
pins that the dialog clears state on both submit paths and that the bridge
interface exposes no getter, no stored token, and no bearer logging.

The old `keychain.js` (direct stronghold access from JS) is intentionally
gone, along with the dead `tauri-plugin-store`/`tauri-plugin-stronghold`
wiring it rode on — nothing at rest, nothing on disk. If a future pass needs
at-rest persistence beyond the session cookie, add the Rust-side stronghold
write inside `bridge_sign_in`; never a webview-held secret.

## Computer tab

The production tab points at a managed session URL (`https://…/session/…`)
the backend hands out: nothing is typed, nothing is remembered, and a card's
"Open session" button is the only thing that dials, always as an explicit user
action. The raw RFB/websockify viewer exists only for development, behind the
explicit `VITE_COMPUTER_TRANSPORT=rfb-stub` flag, against
`tests/tools/rfb_stub_server.mjs`.

On the dev transport, `VITE_VNC_WS_URL` overrides the default
`ws://127.0.0.1:6080/websockify` at dev/build time. The embedded client speaks
the raw websockify stream — `vnc.html` is the standalone page for testing in a
browser and is rejected with an explanation if pasted. A bare remote host that
would send the VNC password in cleartext (`ws://` off-machine) is refused with
a `wss://` pointer instead.

When the server asks for a password, the tab prompts in memory only: the
password is handed straight to noVNC and cleared on submit, disconnect, or
when the tab closes. It is never stored, logged, or placed in a URL.

For a local session without a VM:

```sh
node tests/tools/rfb_stub_server.mjs              # accepts any password
node tests/tools/rfb_stub_server.mjs --reject     # always fails VNC auth
```

Then Connect in the Computer tab. `/stats` on the stub reports
`{ connections, inputEvents }` — `inputEvents` counts key and pointer events
reaching the server, and must stay at 0 while the viewer is view-only.

## Packaging

Unsigned dev builds only: no updater keys, no signing step
(`tauri.conf.json` `updater.active` stays off).

The packaged network posture is pinned by `tests/production_guards.test.mjs`.
Two entries in the Tauri CSP are intentionally broad and stay that way:

- `wss:` is unscoped because tunnel hostnames are operator-chosen and
  dynamic — a new tunnel should not need a config edit to work. The viewer
  still refuses cleartext remotes, strips tokens before display or storage,
  and only dials raw endpoints on the explicit `rfb-stub` dev transport.
- Cleartext (`http://127.0.0.1:*`, `ws://127.0.0.1:*`) is allowed for
  loopback only, for the local stub (`tests/tools/rfb_stub_server.mjs`).
  Release builds default to the `https://kelp.example.com` placeholder and
  fail closed until the operator sets the real server URL.

## Diagnostics

Local logs only, no telemetry. This scaffold sends nothing anywhere: there is
no analytics, no crash reporting, and no network call in either window.

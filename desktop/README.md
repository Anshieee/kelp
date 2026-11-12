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
npm run build       # typecheck + vite build (three entries)
npm run pet:dev     # pet overlay only
```

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

## Diagnostics

Local logs only, no telemetry. This scaffold sends nothing anywhere: there is
no analytics, no crash reporting, and no network call in either window.

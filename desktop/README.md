# Kelp desktop app + desk pet (Tauri thin client)

Thin client over the Kelp server (`chat/api.py`). No chat logic lives here.
This branch is a **frontend scaffold**: React + MUI surfaces with canned
replies. Persistence and server wiring are owned by the backend track.

- **Main window** (`src/index.html`, `src/App.tsx`): a ChatGPT-shaped shell —
  history sidebar grouped by day, conversation column, composer. Fixture
  conversations; sends append to React state only.
- **Command panel** (`src/overlay.html`, `src/OverlayPanel.tsx`): transparent,
  frameless, always-on-top window summoned by `CommandOrControl+Shift+Space`
  or a click on the pet. Compact bar (avatar, field, mic, screen, send) that
  expands rightward into a response column with "Open full app". Escape hides
  it. The screen button is inert: it opens an explainer, and no capture code
  exists yet.
- **Pet overlay** (`src/pet.html`, `src/pet.js`, `src/orb.js`): always-on-top
  frameless window reusing the landing orb. Vanilla on purpose — no React.
- **Server URL** is one constant (`src/config.js`) pointing at the Kelp host.
  No runtime switcher, no per-environment list. `src/api.js` stays as the
  server contract fixture for whoever wires the client up.
- **Not in this scaffold:** sign-in, keychain storage, SSE streaming,
  approvals, devices, spend readout, and persistence. They return with the
  backend wiring; `src/api.js` and `desktop/pet_state.py` already pin the
  contract they must satisfy.

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

## Packaging

Unsigned dev builds only: no updater keys, no signing step
(`tauri.conf.json` `updater.active` stays off).

## Diagnostics

Local logs only, no telemetry. This scaffold sends nothing anywhere: there is
no analytics, no crash reporting, and no network call in either window.

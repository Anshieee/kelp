# Kelp desktop app + desk pet (Tauri thin client)

Thin client over the Kelp server (`chat/api.py`). No chat logic lives here.

- **Main window** (`src/`): sign-in, threads + SSE streaming chat,
  approval cards, schedules, devices, shares, spend readout.
- **Pet overlay** (`src/pet.html`, `src/pet.js`, `src/orb.js`): always-on-top
  frameless window reusing the landing orb (`initOrb` port from
  `origin/landing-fx2`, `landing/e-fx.js`). Mirrors live SSE state only.
- **Auth**: bearer is exchanged once via `POST /api/auth/session`
  (`docs/CHAT_SPEC.md` §12.2). The raw bearer is kept in the OS keychain
  (Tauri stronghold plugin in production; this scaffold ships a
  `keychain` abstraction with an encrypted-store fallback so `cargo test`
  and the webview dev path work without OS credentials). The session
  itself is an `HttpOnly` cookie — JS never sees it.
- **Server URL** is configurable at runtime (Settings field, persisted in
  app config). Defaults to `http://127.0.0.1:8000` for `make serve`;
  production points at the Nebius host. No sidecar is bundled (v1).

## Develop

```sh
cd desktop
npm install
npm run dev        # vite dev server + tauri dev window
npm run pet:dev    # pet overlay only
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

JS pet-state unit test (no deps, `node --test`):

```sh
node --test desktop/tests/pet_state.test.mjs
```

## Packaging

Signed installers + auto-update notes live in
`desktop/src-tauri/tauri.conf.json` (`bundle` + `updater` sections).
Filling in signing keys / update endpoints is a release step, not v1 code.

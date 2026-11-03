# Kelp desktop app + desk pet (Tauri thin client)

Thin client over the Kelp server (`chat/api.py`). No chat logic lives here.

- **Main window** (`src/`): sign-in, threads + SSE streaming chat,
  approval cards, devices, spend readout. Shares UI and the schedules
  screen are hidden entirely in v1.
- **Pet overlay** (`src/pet.html`, `src/pet.js`, `src/orb.js`): always-on-top
  frameless window reusing the landing orb (`initOrb` port from
  `origin/landing-fx2`, `landing/e-fx.js`). Mirrors live SSE state;
  notifications carry one-tap Approve/Deny with the receipt rendered
  in-app; quiet under OS do-not-disturb; reduced-motion respected.
- **Auth**: bearer is exchanged once via `POST /api/auth/session`
  (`docs/CHAT_SPEC.md` §12.2). The raw bearer is kept in the OS keychain
  (Tauri stronghold plugin in production; this scaffold ships a
  `keychain` abstraction with an encrypted-store fallback so `cargo test`
  and the webview dev path work without OS credentials). The session
  itself is an `HttpOnly` cookie — JS never sees it.
- **Server URL** is one constant (`src/config.js`) pointing at the Nebius
  host. No runtime switcher, no per-environment list. Dev may repoint it at
  `http://127.0.0.1:8000` for `make serve` verification.
- **v1 surface:** chat + streaming, approval cards, devices + revoke, spend
  readout. Shares UI and the schedules screen are hidden entirely.

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

JS unit tests (no deps, `node --test`):

```sh
node --test desktop/tests/
```

## Packaging

Unsigned dev builds only: no updater keys, no signing step
(`tauri.conf.json` `updater.active` stays off).

## Diagnostics

Local logs only, no telemetry. The app keeps an in-memory event ring
(Diagnostics card, copyable) and sends data nowhere except the single
configured server URL.

# Desktop overlay — plan (kel-31)

> **Locked decisions (2026-11-02):**
> 1. Base is `desktop-demo` (superset of `desktop-app`), Tauri **v2**.
> 2. **React + TypeScript + Material UI v7** for the app and the overlay. The
>    pet window stays vanilla (`pet.html`, `pet.js`, `pet.svg`, `orb.js`).
> 3. **Frontend only.** No SQL, no persistence, no server calls. Chat state is
>    React state; histories are fixtures. The backend owner wires the rest.
> 4. MUI default light theme with Inter. Visual language gets re-skinned to
>    match the website later.
> 5. Screen access ships as a **visible but inert** control in this PR. No
>    capture code of any kind.

## Surface

Two windows, one shell.

**Overlay** (`overlay.html`, label `overlay`) — a transparent, frameless,
always-on-top window summoned by a global shortcut (`CommandOrControl+Shift+Space`)
or by clicking the pet. It has two states:

- *Compact*: an avatar, a "Ask Kelp…" field, mic, screen, send. 640×88.
- *Expanded*: the same bar plus a response column below it, an "Open full app"
  button and a close control. 760×420.

Escape hides it. Sending appends a canned reply so the interaction reads
correctly; nothing leaves the process.

**Main window** (`index.html`, label `main`) — a ChatGPT-shaped shell:

- Persisted drawer: wordmark, "New chat", history grouped Today / Yesterday /
  Previous 7 days, bottom settings row.
- Top bar: conversation title, search and overflow actions.
- Message column: user and assistant rows, an empty state with suggestion
  chips, and a composer (attach, mic, send) pinned at the bottom.

## What this PR does not do

- No SQLite, no `localStorage` history, no IPC persistence.
- No calls to `chat/api.py`, no SSE, no auth. `src/api.js` stays as the server
  contract fixture for whoever wires it.
- No screen capture. The button opens a short explainer dialog.
- No packaging/signing changes.

## Files

```
desktop/package.json            React, MUI, TypeScript, Vite
desktop/tsconfig.json           strict, bundler resolution
desktop/vite.config.ts          three entry points: main, overlay, pet
desktop/src/index.html          React mount (main)
desktop/src/overlay.html        React mount (overlay)
desktop/src/main.tsx            main-window bootstrap
desktop/src/overlay.tsx         overlay bootstrap
desktop/src/App.tsx             full app shell
desktop/src/Overlay.tsx         compact bar + expanded response column
desktop/src/theme.ts            MUI theme (Inter, light)
desktop/src/types.ts            Conversation / Message shapes
desktop/src/mock.ts             fixture histories
desktop/src/tauri.ts            window helpers, no-op outside Tauri
desktop/src-tauri/tauri.conf.json   + overlay window
desktop/src-tauri/Cargo.toml        + global-shortcut plugin
desktop/src-tauri/src/main.rs       shortcut registration, overlay toggle
desktop/src-tauri/capabilities/default.json   window + shortcut permissions
```

Removed: `desktop/src/app.js`, `desktop/src/keychain.js` (their UI is replaced;
auth returns with the backend wiring). Kept: `api.js`, `demo.js`, `config.js`,
`orb.js`, `pet.*`, `styles.css` — the pet window and their tests are untouched.

## Verification

```sh
cd desktop
npm install
npm run typecheck
npm test          # existing node --test suite must still pass
npm run build     # three html entries in dist/
```

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

**Command panel** (`overlay.html`, label `overlay`) — a transparent, frameless,
always-on-top window. It always opens to the **left of the orb** with the bottom
edges aligned, so growing upward never pushes it off screen; it is clamped to
the monitor work area and only falls back to the right side if the left is out
of room. Summoned by clicking the orb or with `CommandOrControl+Shift+Space`;
both run the same Rust toggle.

- *Compact* (400×76): a fully transparent window holding one white input pill —
  field, mic, screen, expand, send. No panel, no avatar: the orb is the avatar.
- *Expanded* (400×440): the **same width** and same pill at the top, with a
  white conversation surface beneath it and a chevron to collapse.

The expand button does double duty: compact → expanded, and expanded → open the
full app and hide the panel. Clicking anywhere outside hides the window entirely;
Rust decides, so a click on the orb reaches its own toggle instead of being
double-handled. Sending appends a canned reply so the interaction reads
correctly; nothing leaves the process.

**Pet** (`pet.html`, label `pet`) — the avatar, parked at the bottom right of the
primary monitor. 96×96, transparent, no shadow, no drag region, and only the
disc takes pointer events, so the orb looks and behaves like a floating object
rather than a window. A click toggles the panel; the full app is reached from
inside the panel.

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

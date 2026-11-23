# Screen-capture spike: results (2026-11-23)

Phase 0 spike for [`docs/PRODUCT_BRIEF.md`](../PRODUCT_BRIEF.md) sections 8 and 12. The
commands were run by hand; each is shown in the table.

**Answer:** on this Linux machine (MangoWC 0.18.6, a wlroots compositor) Kelp can capture
the full screen in about 38 ms and read the focused app's id in about 11 ms, with **no
permission prompt at all**. That is fast enough for the reaction loop, but it means the
privacy rules in the brief cannot rely on the OS to ask the user: on wlroots compositors
Kelp itself must show the indicator and enforce the per-app opt-in. GNOME, KDE, macOS and
Windows were **not tested**.

## Machine

- Wayland session, compositor MangoWC 0.18.6 (`mangowm` package), Xwayland running rootless on `:0`.
- One 1920×1080 panel (eDP-1).
- Portals running: `xdg-desktop-portal` 1.22.1 with the `wlr` (0.8.4) and `gtk` backends.

## What was measured

All captures were written to `/tmp/opencode/cap` with `umask 077`, never opened or viewed,
and deleted in the same command. Content checks used only a 64×36 grayscale average.

| Method | Result | Prompt? | Time (median of 6) |
|---|---|---|---|
| `grim` (wlr-screencopy protocol), full screen, PPM | 1920×1080, real content | **None** | 38 ms |
| Downscale that frame to 1280 px JPEG q5 (ffmpeg) | ~134 KB per frame | — | 170 ms |
| `grim -t jpeg -q 70 -s 0.5` (capture + scale + encode in one) | 960×540 | None | 304 ms |
| `ffmpeg -f x11grab -i :0` (X11 path) | 1920×1080 frame, **all black** (Xwayland is rootless) | None | 223 ms |
| Portal `org.freedesktop.portal.Screenshot` v2, `interactive: false` | Succeeded | **None** | not timed |
| Portal `ScreenCast` v4 | Advertises `AvailableSourceTypes = 3` (monitor, window) and cursor modes 3; session not started, because the wlr chooser would appear on the user's screen | Chooser (per config) | not run |
| Portal `RemoteDesktop` (input injection) | **Not available** on this compositor | — | — |
| Focused app via `mmsg get focusing-client` (MangoWC IPC) | JSON with `appid`, `title`, `pid`, geometry | None | 11 ms |

### Findings worth carrying into the threat model

1. **No consent gate on wlroots.** Any Wayland client can use wlr-screencopy, so screen
   access is "whatever the process wants". The "watching" indicator, per-app opt-in and the
   banking/password block list must be enforced inside Kelp's capture code.
2. **The wlr portal's non-interactive Screenshot left the image at `/tmp/out.png`,
   mode 0644 (world-readable).** I deleted it. Kelp must not use that portal call on wlroots,
   or must delete the file immediately; the better path is in-memory capture.
3. **The X11 path is useless under Wayland** (black frames), so the app needs a
   Wayland-native capture path, not an X11 fallback.
4. **Per-app opt-in is practical here.** The focused app id is cheap to read, so the reaction
   loop can skip capture entirely when a blocked app (password manager, bank in a browser
   tab title) has focus. This IPC is MangoWC-specific; other compositors need
   `wlr-foreign-toplevel-management` or their own IPC.
5. **Computer use (moving the mouse, typing) has no portal here.** It would need
   `wlr-virtual-pointer`/`virtual-keyboard` or uinput (`ydotool`), which is a separate
   permission and risk decision.

## Not tested, and what is expected (from documentation, unverified)

| Platform | Expected path | Expected consent |
|---|---|---|
| GNOME / KDE Wayland | Portal ScreenCast via PipeWire | A dialog on first use; KDE and GNOME can remember the grant (`persist_mode`) |
| macOS 15+ | ScreenCaptureKit | Screen Recording permission in System Settings, plus a recurring "continue to allow" reminder |
| Windows 10/11 | Windows.Graphics.Capture | A yellow capture border; no permission dialog for a desktop app |

These rows need a real run on each platform before Phase 0 can close.

## Budget implication

At 38 ms capture + 170 ms downscale/encode, one look costs ~0.2 s of CPU on this laptop.
One look every 10 s is ~2% of one core, so capture cost is not the constraint; the vision
model call is.

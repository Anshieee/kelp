# MangoWC window visibility: focus alone misses a visible tile

**Date:** 2026-11-25. **Scope:** MangoWC `0.18.6(release)`, one
`1280×720` headless output, synthetic GTK 3 and GTK 4 windows.
This is a launch and layout test, not an input-control test.

## Why we ran it

The owner saw a Tier 1 spike window tile on the live desktop. The original
spike had a known foreground launch without a window rule. Its saved runs
sampled tag 9 visibility at setup and capture and did not log activation for
the final trials. Those samples cannot identify which launch the owner saw.
The original focus check also did not reject an unfocused but visible target.

## Result

For **each** GTK toolkit, the probe mapped a focused witness on active
tag 1. It then observed three target launches and verified from the
compositor's client list that each target was gone after teardown. GTK 4
also logged an unmap; the GTK 3 target logged exit, not unmap.

| Launch case | Target tag | Target visible in IPC | Target took focus | Witness width while target was mapped |
| --- | --- | --- | --- | --- |
| Normal, no special rule | 1 (active) | Yes | Yes | `576 px` (down from `1280 px`) |
| `is_open_silent:1` | 1 (active) | Yes | No | `576 px` (down from `1280 px`) |
| `is_open_silent:1` | 9 (inactive) | No | No | `1280 px` (unchanged) |

In the first two cases the target occupied `704×720`, with the witness
starting at `x=704`. In both silent cases the witness kept focus. A
focus-only check therefore accepted the active-tag case even though it
changed the layout. The hidden-tag control still mapped a normal
toplevel, but the compositor did not report it visible and the witness
layout stayed unchanged at the sampled boundaries.

This matches MangoWC's separation of placement and focus. In
`src/manage/client.c`, `isopensilent` suppresses initial focus and
automatic switching to the client's tag. It does not hide a client
assigned to a tag that is already visible on its monitor.
The IPC `is_visible` field follows `VISIBLEON` in
`include/mango/manage/client.h`; it is not derived from keyboard focus.

## Reproduction method and isolation

1. Start MangoWC with the wlroots headless backend, pixman, no physical
   input devices and a private one-output config. Use a fresh mode-`0700`
   runtime directory and private home/config paths.
2. Give the compositor an explicit environment without inherited display,
   IPC or D-Bus addresses. Do not inherit `DBUS_SESSION_BUS_ADDRESS`:
   MangoWC's `set_activation_env` and `unset_activation_env` can invoke
   `systemctl --user` when that variable exists. The probe also used an
   empty private `PATH` and absolute program paths.
3. Connect test windows only to the private Wayland socket and `mmsg`
   only to the private IPC socket. Start `watch all-clients`,
   `watch focusing-client` and `watch all-tags` before the target launches.
   Record a witness-only baseline, check the target's compositor-reported
   app ID and tag, and keep the watchers through target removal.
4. Check visibility, active tags, focus, and witness geometry separately.
   The active silent case is a positive control for the visibility and
   layout checks; the inactive silent case is a negative control.
   Confirm no private clients and no private sockets remain after shutdown.

In the isolated GTK 4 trial setup, a `Gtk.Application` ID alone was not
sufficient to match the rule. The synthetic probe sets `GLib.set_prgname`
and asserts the observed app ID before classifying the result. That does
not show the app ID was wrong in any of the original, saved trials.

## Limits and Phase 0 status

- IPC notifications can coalesce. App mapping/removal and the witness
  geometry were observed via compositor events and checkpoints, not every
  rendered frame. `is_visible` is a compositor flag, not pixel or
  z-order evidence.
- No AT-SPI actions, screenshots, keyboard or pointer injection, live
  desktop sockets, or original spike reruns were used in this follow-up.
- This reproduces the mechanism, not a particular historical launch. It
  does not validate the owner's two-monitor configuration, real apps,
  other Wayland compositors, macOS, or Windows.
- The original AT-SPI action measurements are separate. This follow-up
  does not satisfy the full [Phase 0 exit check][phase-0] or change the
  rule against raising or focusing a user's window.

[phase-0]: ../PRODUCT_BRIEF.md#17-phase-0-exit-criteria-and-open-questions

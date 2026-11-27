# Linux background-input findings: what the Phase 0 spikes support

**Date:** 2026-11-28. **Type:** documentation (docs-only PR). **Branch:**
`docs/linux-background-input-findings` at base `670e1b0`. **Status:** corrected
findings offered for peer review; the parent session handles review and
publication. Nothing here is a shipped product capability.

## Scope

This note records what the Phase 0 background-input spike and its follow-ups
actually support. It separates three evidence strands:

1. a live synthetic-widget run on the user's MangoWC desktop (tier 1);
2. a later disposable synthetic MangoWC visibility witness across map/unmap;
3. a nested headless sway virtual-device isolation run (tier 2).

It then states what remains unproven, real-app background input, GNOME/KDE
and broader wlroots coverage, occluded per-window capture, and macOS/Windows.
This is Phase 0 evidence for the [Product Brief §12][brief-12] validation and
[ADR 0004][adr4]; it does not satisfy the full Phase 0 exit criteria and makes
no Phase 1/2 product claim.

## The property being checked

Kelp must drive an unfocused window without changing the user's pointer,
keyboard focus, active window, z-order, workspace or clipboard, and read the
result back through the same API ([brief §12][brief-12], [ADR 0004][adr4]).
Tier 1 acts on the target window through the accessibility API; tier 2 is a
separate workspace with its own input seat. On the user's desktop, Kelp never
injects into the user's seat and never raises or focuses a window.

A target window may be unfocused and still be **visible** (for example tiled
over the rest of the workspace); hidden and occluded are different states. The
spike must therefore check visibility and layout as well as focus, from before
map through unmap, not only at sample points.

## Evidence 1: live synthetic-widget run (tier 1)

Run on the owner's laptop: MangoWC 0.18.6 (wlroots), GTK 3.24.52 and GTK
4.24.1, at-spi2-core 2.62.1, sway 1.12, grim 1.5.0. A synthetic test window
(button, entry, check box) was opened silently on hidden tag 9 through a
runtime window rule. User state was read with `mmsg` (cursor, focused client,
client list, active tags) and `wl-paste` (clipboard and primary selection,
as hashes only); an input watch ran for every trial. No `grab_focus` and no
virtual input on the user's seat were used.

Supported (final runs, 15 trials per toolkit — only aggregate counts below):

| Check | GTK 3 | GTK 4 |
| --- | --- | --- |
| Host snapshots differed before vs after a trial | 0 of 15 | 0 of 15 |
| Test window activation logged | none | none |
| Button click landed (ground truth) | 15/15 | 15/15 |
| Entry text set, ground truth + AT-SPI read-back | 15/15 | 15/15 |
| Check box toggled | 15/15 | 0/15: GTK 4 check box exposes no AT-SPI action |
| Click call time, median | 0.2 ms | 0.4 ms |
| Hidden-tag window capture | 6.1 ms, 420×260, 87% | 6.0 ms, 420×260, 74% |
| Final state vs start | identical | identical |

Limits. The first launch without the window rule opened the window focused on
the active tag and briefly took focus from the previously focused app for about
half a second. The owner saw a tiled spike widget on the live desktop. The saved
evidence cannot identify which launch that was: the setup and capture samples
record the window on tag 9, unfocused and not visible at those points, which
does not prove the window never appeared between them. The two final runs
showed no host-state delta at the sampled boundaries and no logged activation,
but that is point-in-time evidence, not whole-lifecycle invisibility.

Real apps. Only the accessibility-bus agents were present at the time; the
real third-party apps on the machine exposed no accessibility tree unless
accessibility is enabled, and enabling it would alter the user's apps, so it
was not tested. Real-app background input is therefore not demonstrated.

## Evidence 2: disposable synthetic MangoWC witness across map/unmap

A follow-up ran in a disposable, headless MangoWC 0.18.6 session: one
1280×720 output, a fresh mode-0700 runtime directory, private home/config paths,
and no inherited display, IPC or D-Bus addresses. Synthetic GTK 3 and GTK 4
targets plus a focused witness were used. No AT-SPI actions, input injection,
screenshots or host sockets were involved. Details are in the separate
[visibility follow-up][mango-vis].

Supported (each toolkit, three launch cases):

| Launch case | Tag | Visible | Took focus | Witness width |
| --- | --- | --- | --- | --- |
| Normal, no special rule | 1 (active) | Yes | Yes | 576 px |
| `is_open_silent:1` | 1 (active) | Yes | No | 576 px |
| `is_open_silent:1` | 9 (inactive) | No | No | 1280 px |

In the two tiled cases the target occupied 704×720 and the witness shrank to
576×720; the witness returned to 1280×720 and focus after all targets went away.
The silent cases' independent focus stream showed only the witness, and the tag
stream reported tag 1 active throughout the recorded events. This reproduces
the mechanism — a window can stay unfocused and still tile on a visible tag —
consistent with MangoWC's separation of placement and focus (`isopensilent`
suppresses initial focus and automatic tag switching but does not hide a client
on an already-visible tag; IPC `is_visible` follows the compositor flag, not
keyboard focus).

Limits. This is a reproduction of the mechanism, not identification of the
launch the owner saw. IPC notifications can coalesce and are not rendered
frames; `is_visible` is a compositor flag, not pixel or z-order evidence. The
test did not use real apps, the two-monitor live configuration, GNOME/KDE or
macOS/Windows. The hardened harness required isolation evidence, repeated all
six observations with no remaining clients or private sockets, and passed its
28 regression tests. A controlled `SIGTERM` produced exit 143 and a JSON
failure envelope, leaving no captured descendants or private runtime directory.
The harness does not guarantee cleanup for `SIGKILL` or every interruption.

The capture evidence in evidence 1 is for a hidden (inactive-tag) synthetic
window. Per-window capture of an occluded window, and any capture with a real
app, remain unverified.

## Evidence 3: nested sway virtual-device isolation (tier 2)

sway ran nested and headless (`WLR_BACKENDS=headless`,
`WLR_LIBINPUT_NO_DEVICES=1`, Xwayland off) on its own socket. Input came from
a small persistent client that created one virtual pointer and one virtual
keyboard on the nested socket only and refused the user's socket.

Supported (10 rounds per configuration):

| Check | GTK 3 | GTK 4 (cairo, pixman) | GTK 4 (default, gles2) |
| --- | --- | --- | --- |
| Click / type / toggle landed | 10/10/10 | 10/10/10 | 10/10/10 |
| Entry length exactly what was typed (no stray keys) | 10/10 | 10/10 | 10/10 |
| Approx. per round (2 clicks, 13 keys, 1 click) | 338 ms | 342 ms | 342 ms |
| Nested screen capture | 11.8 ms | 13.8 ms | 14.3 ms |
| Host user input during run | 4 key events | none | 3036 pointer, 10 key |
| Host state diff | focus + tag switch | none | cursor, focus, tags, clipboard |

Host diffs appeared only in runs where the owner was actively using the machine
(a tag switch, moving to another app, a copy); the idle run showed no diff. The
nested devices exist only on the nested socket, so they have no path to the
user's seat, but the busy runs alone cannot prove that. In the GTK 3 run with
four owner key events, the nested entry held exactly the characters typed by
the harness, so none of the owner's keys reached the harness's seat.

Failures found: a transient one-command virtual device (`wlrctl`) delivered
nothing (0 of 9 checked outcomes in 3 rounds), so the seat needs a persistent
device that stays alive; and GTK 4 segfaults under the software (pixman) nested
sway with its default Vulkan renderer, working only with `GSK_RENDERER=cairo`
or a GPU-backed nested sway (`WLR_RENDERER=gles2`).

## What is proven and what is not

Proven (for the synthetic setups described, on this hardware):

- Tier 1 AT-SPI actions and text read-back on synthetic GTK 3/GTK 4 controls,
  with no host-state delta at the sampled boundaries of the final runs.
- A window can be unfocused yet visible (tiled), so a focus-only check is
  insufficient; hidden-tag placement prevents this in the tested case.
- Tier 2 nested isolation with a persistent virtual seat works for everything
  tested, at roughly 0.3 s per small round.
- Per-window capture of a hidden synthetic window works on MangoWC in ~6 ms.
- Nested virtual devices are isolated to the nested socket in the tested runs.

Not proven / open (Phase 0 exit criteria still open, [brief §17][brief-17]):

- Real-app background input through tier 1 (only synthetic GTK windows were
  driven).
- GNOME and KDE paths, and wlroots coverage beyond the tested MangoWC/sway
  setup; the permission flow on each compositor is unrecorded.
- Occluded per-window capture, and capture with a real app; the saved capture
  evidence is for a hidden synthetic window.
- Whole-lifecycle invisibility of a live launch; the saved samples cannot
  identify the launch the owner saw.
- macOS (all Macs) and Windows: deferred to a future platform milestone, not
  proven or waived here ([brief §12][brief-12], [ADR 0004][adr4]).

No change to the rule against raising or focusing a user's window, and no
product behavior, follows from this note.

## References

- [Product Brief §12, platform permissions risk][brief-12]
- [Product Brief §17, Phase 0 exit criteria][brief-17]
- [ADR 0004, desktop companion][adr4]
- [MangoWC visibility follow-up][mango-vis]

[brief-12]: ../PRODUCT_BRIEF.md#12-platform-permissions-risk
[brief-17]: ../PRODUCT_BRIEF.md#17-phase-0-exit-criteria-and-open-questions
[adr4]: ../adr/0004-desktop-companion.md
[mango-vis]: ./2026-11-mango-window-visibility.md

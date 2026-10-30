# Kelp explainer — HyperFrames (v2, 90 s)

1920×1080, 30 fps, 90 s (1:30). Text-on-screen + music, no voiceover.
Deterministic: every frame is a function of timeline time; no `Math.random`,
no wall-clock reads, no network fetches at render time.

v1 is the same cut at 160 s (`renders/kelp-explainer-v1.mp4`, commit
`2bbb828`). v2 changes pacing only: same six scenes, every beat, every line
of copy, the same music bed and every SFX hit.

## Timeline

| Time | Scene | On screen |
| --- | --- | --- |
| 0:00–0:11 | `01-intro` | Room cold-open, "CONCEPT PREVIEW" label (0–2.6 s), thumb types three tasks, lid closes, **"Give it your day. Then leave."** (7.0 s) |
| 0:11–0:30 | `02-inbox` | Inbox time-lapse 40 → 0, rows sorted/starred/chips, 3 drafts, orb tracks, phone buzz, **"40 emails. Sorted, starred, replies drafted."** (19.9 s) then **"Task 1 / done."** (27.3 s) |
| 0:30–0:47 | `03-flight` | Three flights compared, best held, **"Found your flight."** (30.5 s), phone approval card, thumb taps Approve (40.0 s), ticket, **"You approve. It books."** (40.5 s) |
| 0:47–1:09 | `04-receipts` | Three receipts → spreadsheet rows → total, **"Receipts in. Expenses filed."**, FILED stamp (60.8 s), phone buzz (65.0 s), **"Task 3 / done."** (66.0 s) |
| 1:09–1:24 | `05-split` | Split frame: closed laptop | three green checks on phone (71.4–72.4 s), **"It has its own computer." / "Yours can stay closed."** (69.6 / 71.3 s), "2h 40m of your day, back." (73.5 s) |
| 1:24–1:30 | `06-end` | Ivory end card, orb blinks, **"Kelp works while you're away."** (84.7 s), URL (85.5 s), "Concept preview · not a live service" |

Scene mounts live in `index.html` (`data-start` / `data-duration`); every
composition builds one paused GSAP timeline and registers it as
`window.__timelines["<id>"]`.

## Pacing (the v2 retime)

No shot, element or keyframe was removed. Each composition keeps its authored
35 s (or 20/25/10 s) timeline and applies one uniform warp before registering:

```js
const k = <authored length> / <new length>;
timeline.getChildren(false, true, true).forEach((child) => {
  child.startTime(child.startTime() / k);
  child.timeScale(child.timeScale() * k);
});
```

Every tween starts `1/k` earlier and plays `k` faster, so relative timing,
stagger, easing and overlap are preserved exactly — only the clock changes.
Scene budgets: 11 + 19 + 17 + 22 + 15 + 6 = 90 s. Receipts gets the largest
share because it carries three filings plus the stamp, phone buzz and done
card; the split and the end card are the shortest.

Because the cut runs ~1.8× faster, page motion blur was switched off
(`data-motion-blur="off"`): at this speed it smeared whole frames on the
phone entrance and the done curtain. The flat 2D wipes are crisper without it.

SFX positions in `index.html` were remapped with the same per-scene factor,
so every hit stays on its picture event (typing on keystrokes, taps on taps,
pops on row reveals, buzz on the phone shake, chime on the end card).

## Look

- Paper-ivory ground `#FBF9F5`, ink `#1B211C`, jade `#59B26F` (deep `#276B3C`),
  vermilion `#E52D2D`, sage/butter support tones.
- Newsreader for display, Inter for UI; both bundled WOFF2 in `assets/fonts/`.
- Flat 2D, character-driven (the orb pet is the through-line), live screen
  mockups cut in for clarity, motion-based wipes between scenes.
- Calm easing (`power2`/`power3`, no elastic except small check pops).

## Audio

The same 160 s music bed at `assets/bgm/kelp-bed.mp3`, trimmed to 90 s with a
1.2 s fade-in and 2.5 s fade-out, plus the same SFX on the visible actions.
All levels authored in `index.html`.

## Verification

- `npx hyperframes check --json`: 0 errors (lint, runtime, layout, contrast,
  audio, craft, motion).
- `npx hyperframes snapshot`: every scene midpoint, both done cards, the
  first frame, the last frame and the scene seams.
- Render: `renders/kelp-explainer-v2.mp4` — H.264 / AAC, 1920×1080, 30 fps,
  90.000 s.
- Audio mastered to −18.2 LUFS integrated, −3.3 dBFS true peak (a +4 dB
  delivery gain on the HyperFrames mix; video stream copied untouched).
- Frame-sampled the encoded MP4 at 20 points and compared against this table.
- v1 remains at `renders/kelp-explainer-v1.mp4` (160 s) for comparison.
- Weakest shot and warnings kept on purpose: `REVIEW.md`.

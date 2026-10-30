# Kelp explainer — HyperFrames (v3, 90 s)

1920×1080, 30 fps, 90 s (1:30). Text-on-screen + music, no voiceover.
Deterministic: every frame is a function of timeline time; no `Math.random`,
no wall-clock reads, no network fetches at render time.

v1 is the same cut at 160 s (`renders/kelp-explainer-v1.mp4`, commit
`2bbb828`). v2 changes pacing only. v3 keeps that cut and fixes the two phone
fingers plus the pacing warp that made chained tweens jump on seek.

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
35 s (or 20/25/10 s) timeline and runs it at `k×` inside a paused host
timeline, which is what gets registered:

```js
const pace = (timeline, k) => {
  timeline.paused(false).timeScale(k);
  return gsap.timeline({ paused: true }).add(timeline, 0);
};
window.__timelines["s1"] = pace(tl, 20 / 11);
```

The host timeline's duration becomes `authored / k` and seeking it at `t`
renders the scene at `t * k`, so relative timing, stagger, easing and overlap
are preserved exactly — only the clock changes. (The first implementation
scaled each tween's `startTime` and `timeScale`; that produced seek-time jumps
where sequential tweens on one property fought, and was replaced.)
Scene budgets: 11 + 19 + 17 + 22 + 15 + 6 = 90 s. Receipts gets the largest
share because it carries three filings plus the stamp, phone buzz and done
card; the split and the end card are the shortest.

Because the cut runs ~1.8× faster, page motion blur was switched off
(`data-motion-blur="off"`): at this speed it smeared whole frames on the
phone entrance and the done curtain. The flat 2D wipes are crisper without it.

SFX positions in `index.html` were remapped with the same per-scene factor,
so every hit stays on its picture event (typing on keystrokes, taps on taps,
pops on row reveals, buzz on the phone shake, chime on the end card).

## Tap anchors (v3)

Both screen taps are placed from measured pixels, not eyeballed offsets:

- **Intro, typing thumb.** Task-row centres are 663 / 738 / 812 px (75 px
  apart, measured from the row dots); fingertip x is 1310, just past the
  caret. The thumb moves on `y` only, presses 12 px, releases, and never
  drifts on `x`.
- **Flight, Approve.** The button measures 792–988 × 856–914 px; the vertical
  thumb's tip is anchored to 890,900 so the press lands on the button while
  "Approve" stays readable. It rises from below, presses 12 px, releases in
  place and fades — no sideways fling.
- Both thumbs sit above their phone (`z-index` 8 and 25 vs 6 and 20) and below
  nothing they should cover; `pointer-events: none`.

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
- `npx hyperframes snapshot`: every scene midpoint, both done cards, every
  finger/tap frame, the first frame, the last frame and the scene seams.
- Render: `renders/kelp-explainer-v3.mp4` — H.264 / AAC, 1920×1080, 30 fps,
  90.000 s, 26,282,718 bytes,
  SHA-256 `35828b522c50a03c9dbdf85812dde905ad5561d4a8ee76615210af8a8476a881`.
- Tap anchors measured on decoded frames: S1 tips x 1310, y centres 664 / 740
  / 814 (targets 663 / 738 / 812); S3 tip x 856–923, top 900 inside the
  Approve button, press 904, gone by 40.4 s; no stray finger in non-tap phone
  shots.
- Audio mastered to −18.2 LUFS integrated, −5.2 dBFS true peak (a +4 dB
  delivery gain on the HyperFrames mix; video stream copied untouched).
- Frame-sampled the encoded MP4 at 21 points and compared against this table.
- v1 (160 s) and v2 (90 s, pre-finger-fix) remain in `renders/` for comparison.
- Weakest shot and warnings kept on purpose: `REVIEW.md`.

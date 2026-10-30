# Kelp explainer — iteration 1 (HyperFrames)

1920×1080, 30 fps, 160 s (2:40). Text-on-screen + music, no voiceover.
Deterministic: every frame is a function of timeline time; no `Math.random`,
no wall-clock reads, no network fetches at render time.

## Timeline

| Time | Scene | On screen |
| --- | --- | --- |
| 0:00–0:20 | `01-intro` | Room cold-open, "CONCEPT PREVIEW" label (0–5 s), thumb types three tasks, lid closes, **"Give it your day. Then leave."** (12.7 s) |
| 0:20–0:55 | `02-inbox` | Inbox time-lapse 40 → 0, rows sorted/starred/chips, 3 drafts, orb tracks, phone buzz, **"40 emails. Sorted, starred, replies drafted."** then **"Task 1 / done."** |
| 0:55–1:30 | `03-flight` | Three flights compared, best held, **"Found your flight."**, phone approval card, thumb taps Approve, ticket, **"You approve. It books."** |
| 1:30–2:05 | `04-receipts` | Three receipts → spreadsheet rows → total, **"Receipts in. Expenses filed."**, FILED stamp, phone buzz, **"Task 3 / done."** |
| 2:05–2:30 | `05-split` | Split frame: closed laptop | three green checks on phone, **"It has its own computer." / "Yours can stay closed."**, "2h 40m of your day, back." |
| 2:30–2:40 | `06-end` | Ivory end card, orb blinks, **"Kelp works while you're away."**, URL, "Concept preview · not a live service" |

Scene mounts live in `index.html` (`data-start` / `data-duration`); every
composition builds one paused GSAP timeline and registers it as
`window.__timelines["<id>"]`.

## Look

- Paper-ivory ground `#FBF9F5`, ink `#1B211C`, jade `#59B26F` (deep `#276B3C`),
  vermilion `#E52D2D`, sage/butter support tones.
- Newsreader for display, Inter for UI; both bundled WOFF2 in `assets/fonts/`.
- Flat 2D, character-driven (the orb pet is the through-line), live screen
  mockups cut in for clarity, motion-based wipes between scenes.
- Calm easing (`power2`/`power3`, no elastic except small check pops).

## Audio

One 160 s music bed at `assets/bgm/kelp-bed.mp3` (fade in 1.2 s, out 3 s),
plus SFX on the visible actions: typing, taps, lid, wipes, buzzes, checks,
chime. All levels authored in `index.html`.

## Verification

- `npx hyperframes check --json`: 0 errors (lint, runtime, layout, contrast,
  audio, craft, motion).
- `npx hyperframes snapshot --review`: beat sheet, six seam strips and the
  360 px phone sheet; plus targeted frames at every handwriting and buzz.
- Render: `renders/kelp-explainer-v1.mp4` — H.264 / AAC, 1920×1080, 30 fps,
  160.000 s, 42,885,720 bytes.
- Audio mastered to −18.2 LUFS integrated, −3.3 dBFS true peak (a +4 dB
  delivery gain on the HyperFrames mix; video stream copied untouched).
- Frame-sampled the encoded MP4 at 14 points and compared against this table.
- SHA-256 `a0cb4cf24ca37c52edbd496b292c431b98be52db473da384b9894ba8e60d2f7a`.
- Weakest shot and warnings kept on purpose: `REVIEW.md`.

## Known deviations / notes

- `craft.text_below_floor` remains a warning: the in-app UI mockups use
  11–16 px type (real product scale inside a 1x mockup). All narrative copy
  is 40–280 px.
- `lint.composition_file_too_large` on `02-inbox.html` (≈380 lines). Kept as
  one scene because the time-lapse reads as one continuous shot.
- Music is CC BY 3.0; credit in `CREDITS.md`.

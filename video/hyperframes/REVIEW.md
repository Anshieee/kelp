# Review — Kelp explainer

Reviewed from `check --json`, `snapshot --review` (beat sheet, seam strips,
360 px phone sheet) and frame samples decoded from the rendered MP4.

## v2 retime (90 s)

The film was re-cut from 160 s to exactly 90 s without dropping a shot, a
line of copy, a beat or an SFX. Every composition keeps its authored timeline
and applies one uniform warp before registering it: each child tween starts
`1/k` earlier and runs `k` faster. Relative timing, stagger, easing and
overlap are therefore identical to v1 — only the clock changes.

- Scene budgets: intro 11, inbox 19, flight 17, receipts 22, split 15,
  end 6 (sum 90). Receipts got the largest share because it carries three
  filings plus the stamp, the phone buzz and the done card; the split and
  end card are the calmest and took the biggest trim.
- Speed factors: 1.56× (split), 1.67× (end), 1.82× (intro), 1.84× (inbox),
  2.06× (flight), 1.59× (receipts). The flight scene is the fastest per
  second because it is mostly UI cards that need little reading time; its
  ticket payoff still holds ~6 s.
- All 29 SFX were remapped with the same per-scene factor and stay on their
  picture event. The 160 s music bed is trimmed to 90 s with a 2.5 s fade.
- Page motion blur is now off. At 1.8× it smeared whole frames on the phone
  entrance and the done curtain; the flat 2D wipes read cleaner crisp.

Measured: `check` 0 errors; MP4 is 90.000 s; −18.2 LUFS / −5.2 dBFS true
peak after the same +4 dB delivery gain; 23 decoded frames matched to the
script table. Looked at: every scene midpoint, both done cards, all seams
and the 360 px sheet.

What got worse: the inbox row cadence (one row every 0.84 s) is now clearly
a time-lapse rather than a considered sort, and the flight approval tap is
quick. That is the cost of 90 s; nothing is unreadable, but the senior-calm
feel of v1 is stronger in the two task scenes. v1 remains at
`renders/kelp-explainer-v1.mp4` for comparison.

## v1 notes (160 s)

## Creative calls

- **The orb is the person.** No human face is shown; the pet orb carries
  attention (eyes track the inbox, the phone, the receipts) and blinks on the
  end card. Hands appear only as a single typing thumb so the viewer reads a
  person without casting an actor.
- **Screen mockups are the proof, not decoration.** Each task cuts to a live
  UI that does the work on screen (rows sorting, drafts filling, approval
  card, spreadsheet rows filing). The headline stays in the left margin so the
  UI keeps the right two-thirds.
- **Chapter wipes instead of cuts.** A jade curtain moves on every scene
  boundary, so the three tasks read as three chapters of one day.
- **Done cards are the punctuation.** Deep-jade (Task 1) and ink (Task 3)
  full-frame cards with one huge italic *done.*, matching the script's two
  "Task N done." beats. Task 2's completion is the booked ticket and
  "You approve. It books." rather than a third card, because the script gives
  no "Task 2 done." line.
- **Cold open.** Frame 0 is the finished room with the "Concept preview"
  label, not a fade from empty, so the thumbnail already shows the film.
- Storylines not built: a "night/day" split and a "many tasks queue up"
  montage. Both were options; the one-take day-in-the-life was chosen because
  it lets each task get a full screen mockup.

## What was measured vs. looked at

- Measured: contract/`check` (0 errors across lint, runtime, layout, contrast,
  audio, craft, motion), MP4 duration (160.000 s), resolution/fps/codec,
  per-clip SFX peaks (no clipping), and 14 decoded frames against the script
  table.
- Looked at: every scene midpoint, both buzz cards, the opening frame, all
  six seams, and the 360 px sheet.
- **Not heard.** The music and SFX were measured for level and position, not
  listened to. The mix is a single music bed at 0.8 with SFX at 0.1–0.3; if
  anything is wrong it will be balance, not sync.

## Weakest shot

The intro thumb. It is a stylised flat finger rather than an anatomical hand;
on the couch it can read as a peach pill for a beat. With more time: a proper
arm entering from the bottom-right corner, or a hand holding the phone with
the thumb inside the screen, would sell the "typing" better. It is confined
to 0–8 s and does not affect the read of the task.

Second weakest: the flight scene dims the three cards to 28 % while the phone
is up (t≈72 s); the comparison context goes very quiet. A 0.55 dim would keep
the "best for you" card legible behind the phone.

## Warnings kept on purpose

- `craft.text_below_floor` (6): the in-app mockups use 11–16 px type because
  they are real UI at 1× inside a 1920 frame. Every line meant to be read from
  the couch is 40–280 px, and the 360 px phone sheet confirms the narrative
  copy reads.
- `lint.composition_file_too_large` (02-inbox, ~380 lines): the inbox
  time-lapse is one continuous shot; splitting it would add a seam the cut
  does not want.
- The jade wipes hold a flat colour for 0.4–0.7 s. That is deliberate chapter
  punctuation, and it is the brand colour rather than black.

## Render

- **v2:** `renders/kelp-explainer-v2.mp4` — H.264 / AAC, 1920×1080, 30 fps,
  90.000 s, 26,727,127 bytes,
  SHA-256 `aa002f80ce9aa2292c60bd8985ce1d2593fba26c94d914d61ca43ace0be81933`.
- **v1 (archive):** `renders/kelp-explainer-v1.mp4` — 160.0 s, 42,885,720 bytes.

Both mixes measured −22.2 LUFS integrated; a +4 dB delivery gain brings them
to −18.2 LUFS with the video stream copied (v2 true peak −5.2 dBFS, v1
−3.3 dBFS). Source is deterministic; re-rendering the same commit reproduces
the cut (the audio gain is a separate, repeatable ffmpeg step).

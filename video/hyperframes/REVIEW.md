# Review — Kelp explainer, iteration 1

Reviewed from `check --json`, `snapshot --review` (beat sheet, seam strips,
360 px phone sheet) and frame samples decoded from the rendered MP4.

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

`renders/kelp-explainer-v1.mp4` — H.264 / AAC, 1920×1080, 30 fps, 160.0 s,
42,885,720 bytes. The HyperFrames mix measured −22.2 LUFS integrated; a
+4 dB delivery gain brings it to −18.2 LUFS / −3.3 dBFS true peak with the
video stream copied. Source is deterministic; re-rendering the same commit
reproduces the cut (the audio gain is a separate, repeatable ffmpeg step).

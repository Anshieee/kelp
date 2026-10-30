---
name: Kelp
description: A welcoming, readable noticeboard for everyday help.
colors:
  forest-ink: "#203b2d"
  secondary-ink: "#435348"
  ivory: "#fbf9f5"
  mint: "#c9dfbc"
  pale-sage: "#e8eddf"
  butter: "#f0e7c5"
  rule: "#bac3b6"
typography:
  display:
    fontFamily: "Newsreader, Georgia, serif"
    fontSize: "clamp(62px, 7vw, 96px)"
    fontWeight: 400
    lineHeight: 1.02
    letterSpacing: "-0.035em"
  headline:
    fontFamily: "Newsreader, Georgia, serif"
    fontSize: "clamp(44px, 5.2vw, 72px)"
    fontWeight: 400
    lineHeight: 1.06
    letterSpacing: "-0.035em"
  title:
    fontFamily: "Newsreader, Georgia, serif"
    fontSize: "32px"
    fontWeight: 400
    lineHeight: 1.06
  body:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "18px"
    fontWeight: 400
    lineHeight: 1.6
  label:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.6
rounded:
  pill: "999px"
  surface: "36px"
  surface-mobile: "26px"
  dialog: "30px"
spacing:
  tight: "8px"
  group: "16px"
  content: "24px"
  section: "40px"
components:
  button-primary:
    backgroundColor: "{colors.forest-ink}"
    textColor: "{colors.ivory}"
    rounded: "{rounded.pill}"
    padding: "13px 25px"
    height: "56px"
  button-secondary:
    textColor: "{colors.forest-ink}"
    rounded: "{rounded.pill}"
    padding: "13px 25px"
    height: "56px"
  question-chip:
    textColor: "{colors.forest-ink}"
    rounded: "{rounded.pill}"
    padding: "10px 15px"
  reading-surface:
    backgroundColor: "{colors.pale-sage}"
    textColor: "{colors.forest-ink}"
    rounded: "{rounded.surface}"
---

# Design System: Kelp

## Overview

**Creative North Star: "A welcoming noticeboard"**

Clear statements, patient instructions, and substantial fields of color create a public-service warmth without making the interface childish. The brief fixes ivory, Newsreader, Inter, and pills; the implemented system gives those commitments dark-green ink and an open, practical rhythm.

**Key Characteristics:**
- Flat, defined reading surfaces.
- Large serif statements and plain sans-serif instructions.
- Generous pill targets with explicit selected states.
- No decorative ambient motion.

## Colors

### Primary
Forest ink carries headings, primary actions, selected questions, and the dark promise field. Mint supplies the broad emphasis surface.

### Secondary
Pale sage separates the working conversation from the page. Butter distinguishes financial boundaries and approval previews without signalling an error.

### Neutral
Ivory is the required page and input ground. Secondary ink is used for supporting text; the rule color divides groups.

**The Reading First Rule.** Text keeps at least AA contrast on every supported reading surface. Measured body-ink ratios range from 5.75:1 on mint to 7.77:1 on ivory.

## Typography

Display and body faces are explicit brand constraints, not replacements to make during future polish. Serif italic provides selective conversational emphasis; it does not become an all-italic reading surface. The mobile display uses `clamp(43px, 10.7vw, 58px)`. Supporting explanations use 16–18px; secondary disclosures use 12–15px. Tracking never exceeds the implemented tightness of -0.035em. Tabular numerals belong to example times.

## Layout

The main container caps at 1280px with 56px side margins on wide screens, 36px below 1100px, and 20px below 540px. The conversation uses two unequal columns above 800px and stacks below that point. Feature descriptions remain present on narrow screens. Main section spacing is 54–104px, while grouped controls use 8–16px. Long modal content scrolls vertically within the viewport rather than reducing text size.

## Elevation & Depth

Tonal fields and rules supply hierarchy. Ordinary content has no shadows. Only the floating storage notice (`0 6px 24px #203b2d17`) and modal dialogs (`0 18px 80px #203b2d33`) are elevated. The backdrop is opaque-tinted forest; no decorative blur or glass is used.

## Shapes

Primary controls and question chips are full pills. Broad reading surfaces use softly rounded corners; open feature rows use straight horizontal rules rather than enclosing every paragraph in a card. The headline capsule is typographic emphasis, not a button.

## Components

### Buttons
Primary actions use forest with ivory text; hover deepens to `#36543e`. Secondary actions are outlined. Most controls have 48–56px minimum height; icon controls keep a 44px minimum target. Keyboard focus is a 3px forest outline with a 5px offset.

### Question chips
Outlined pills invert to forest and ivory when selected, with `aria-pressed` carrying the same state. Editing the input clears the selected starter. Mobile chips form full-width rows to avoid small labels.

### Input
A labelled ivory composer uses a defined green outline. Desktop tools sit beside the input; on phones the input takes the first row and the 44px tools take the second. A returned preview is a readable ivory reply sheet and polite live region.

### Navigation and dialogs
Navigation links have generous target heights and underline on hover. The dashboard action remains visible on phones while in-page navigation yields to the visible content. Native dialogs retain close, Escape, inert background, boundary-tab cycling, and opener-focus restoration. Preview examples never imply that a real action occurred.

### Motion
Only a returned reply receives a short 240ms settle (`cubic-bezier(.16,1,.3,1)`). Content is visible without animation. Manual pause and reduced-motion preferences disable that animation and CSS transitions; smooth scrolling also becomes immediate.

## Do's and Don'ts

### Do:
- **Do** use the same green-ink hierarchy on ivory, sage, and butter surfaces.
- **Do** preserve readable instructions, generous targets, and visible focus.
- **Do** label simulations and placeholder destinations plainly.

### Don't:
- **Don't** introduce emojis, gradient text, decorative glass, or an ambient ticker.
- **Don't** hide feature descriptions or shrink input text to fit narrow screens.
- **Don't** add unverified claims, fabricated testimonials, or implied live results.

# Notices for the Kelp phone app

This directory (`phone/`) contains Kelp's phone web app: an installable PWA
that talks to the Kelp server's own `/api` surface.

## OpenMausBot (Apache License 2.0)

Parts of this app adapt patterns from
[OpenMausBot](https://github.com/milind-soni/OpenMausBot) by Milind Soni and
contributors, licensed under the [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0)
(a copy of the license terms applies to the adapted portions as required by
that license; the upstream NOTICE is reproduced in spirit below).

Adapted patterns, re-expressed against Kelp's API and design:

- The PWA manifest structure (`public/manifest.webmanifest`): `id`/`scope`/
  `start_url`, `display: standalone`, dark theme colors, and separate `any`
  and `maskable` icon entries.
- The phase-based pairing UX from `PhoneSetupFlow.tsx` (intro → credential
  form → busy → error with `role="alert"`, disabled submit while working,
  explanatory trust copy). Kelp's pairing is a single bearer-token exchange,
  so the upstream QR/polling machinery was not carried over.
- The install-guidance dialog pattern from `PhoneAppDialog.tsx`, distilled
  into the Settings screen's per-platform install hint.
- Phone-first responsive conventions: dark panel/hairline styling tokens,
  44px minimum touch targets, and safe-area handling.

No code was copied wholesale; all logic is written against Kelp's API
contract (`docs/CHAT_SPEC.md`). Changes from the upstream originals are
substantial and are recorded here per Apache-2.0 §4(b).

Upstream copyright: `Copyright 2026 Milind Soni and OpenMausBot contributors`.

## In-repo adaptation (Midnight/Meadow, Geist, drawer UI)

The previous visual language (Midnight and Meadow themes, Geist typography,
conversation drawer, rounded composer, approval-card and task bubble patterns,
kelp-avatar motif) was adapted from the OpenMausBot-derived design already in
this repository at `netlify/mobile/` (see `third-party/OpenMausBot/` for the
upstream license and notice). The blue rounded-square avatar came from that
lineage.

## Site-language redesign (current)

The current look follows the Kelp marketing site in `landing/` (same
repository): the warm canvas, ink, and pastel tokens; Newsreader serif
display type with Inter body type; the sprout mark with the serif "kelp"
wordmark in app chrome; pill buttons and rounded cards.

- The `sprout` artwork is copied from the site's inline SVG symbol
  (`landing/index.html`): a stroke-only mark, `fill:none`,
  `stroke:currentColor`. The install icons (`public/icons/*`) are unchanged
  originals.
- The film-grain page overlay (`body::after` in `src/styles.css`) adapts the
  site's static `feTurbulence` data-URI (`landing/styles.css`), with an
  inverted speckle for the dark theme. The empty-state artwork
  (`src/components/LivingBlob.tsx`) is an original canvas re-imagining of the
  site hero field's look — layered sine-warped contour rings over a
  peach/gold/mint wash, drifting on incommensurate tides with pointer
  follow — written fresh for the phone's performance budget (~30fps,
  reduced backing store, DPR cap, pausing when hidden, off-screen, or
  reduced-motion), not a port of the site's WebGL shader.
- `public/fonts/newsreader-latin.woff2`, `newsreader-italic-latin.woff2`,
  and `inter-latin.woff2` are the SIL Open Font License 1.1 faces the site
  loads from Google Fonts, vendored here so the offline-first PWA never
  needs the CDN. The previously bundled `geist-latin.woff2` was removed
  with the redesign.
- The Midnight dark theme is retained as an option; the Meadow theme was
  retired with the redesign.

## In-repo design adaptation (Midnight/Meadow, Geist, drawer UI)

The current visual language (Midnight and Meadow color tokens, Geist
typography, conversation drawer, rounded composer, approval-card and task
bubble patterns, kelp-avatar motif) is adapted from the OpenMausBot-derived
design already in this repository at `netlify/mobile/` (see
`third-party/OpenMausBot/` for the upstream license and notice). Token values
and component patterns were re-expressed in this app's own stylesheet
(`src/styles.css`) and components; nothing was copied wholesale, and no shared
asset files were referenced or moved.

The bundled font `public/fonts/geist-latin.woff2` is Geist, Copyright 2026
The Geist Project Authors, under the SIL Open Font License 1.1 (see
`third-party/Geist/`). The same subset ships in the in-repo demo client.

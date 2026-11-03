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

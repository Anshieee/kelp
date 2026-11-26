# Release and licensing checklist (Phase 0)

- **Status:** owner decision recorded 2026-11-26: Apache-2.0 is the intended
  outbound licence for Kelp's original code, pending verification of
  contributor/rightsholder permissions and downstream release prep. No grant is
  published, the current public repo is not blanket licensed, and no root
  `LICENSE`, package metadata or landing copy is added here. This is an
  engineering checklist, not a legal opinion, and it publishes no licence.
- **Evidence baseline:** `fdb6daa`. No release artifact was built or inspected.
- **Scope:** what the tree states about licensing today, the decisions the
  owner must make before publishing a release, and the artifact checks that
  follow from those decisions.
- **Companions:** [`docs/DEPLOY.md`](DEPLOY.md) (release path),
  [`docs/PRIVACY.md`](PRIVACY.md), [`phone/NOTICE.md`](../phone/NOTICE.md),
  [`third-party/OpenMausBot/NOTICE`](../third-party/OpenMausBot/NOTICE).

## 1. What the tree states today (verified)

- **No root `LICENSE`, `COPYING` or `NOTICE` is tracked** (`git ls-files`).
- **The image declares an `UNLICENSED` licence label** in the
  [`Dockerfile`](../Dockerfile). The final stage copies the virtual
  environment and runtime source, not the repository's `third-party/` or
  phone notices. Installed dependencies may carry their own notice files.
- **The build context excludes some notice paths.**
  [`.dockerignore`](../.dockerignore) excludes `docs/`, `README.md`,
  Markdown files and `LICENSE`. Adding a COPY line for one of these files
  also needs a matching build-context change.
- **The landing page says "open-source" without a project-wide grant in
  the tree**: see
  [`landing/index.html`](../landing/index.html) :9 (meta description) and :20
  (hero copy). The [`product brief`](PRODUCT_BRIEF.md) sets open source as
  the product direction. The [`threat model`](THREAT_MODEL.md) and
  [ADR 0004](adr/0004-desktop-companion.md) make related commitments for
  the planned relay and hosted service.
- **The project manifests do not declare a licence**:
  [`pyproject.toml`](../pyproject.toml),
  [`desktop/package.json`](../desktop/package.json),
  [`desktop/src-tauri/Cargo.toml`](../desktop/src-tauri/Cargo.toml) and
  [`phone/package.json`](../phone/package.json) have no licence field.

A public repository and third-party notices are not a grant for Kelp's
original material. Keep the licences already granted on individual
components separate from a future Kelp-wide licence.

## 2. Owner decisions (blocking; each needs an explicit answer)

- [ ] **D1 Grant (selected 2026-11-26, pending verification).** Apache-2.0 is
      the intended outbound licence for Kelp's **original code** only, pending
      verification of contributor/rightsholder permissions and downstream
      release prep. No grant is published: no root `LICENSE`, package metadata,
      OCI label or landing copy is added, and the current public repo is not
      blanket licensed. This selection does not cover docs, art or third-party
      material; the scope of that other material is a separate future decision
      (D2–D4 remain open).
- [ ] **D2 Rights.** Document the existing rights or obtain permission from
      the relevant rightsholders for the code and assets being released.
      Commit identities are provenance clues, not evidence of a grant.
      Decide how to document permissions for future contributions.
- [ ] **D3 Scope.** D1 selects a licence for original code only. Decide
      separately whether and how to license original docs and artwork in §4.
      Any third-party material distributed with Kelp remains under its own
      terms.
- [ ] **D4 Claim.** Check all tracked release claims (`git grep -inE
      'open[- ]source'`). Distinguish what the landing page promises now
      from future commitments in planning docs. Describe the current
      grant accurately until D1 and D2 are resolved.

Nothing below is a legal conclusion; D1–D4 belong to the owner. The release
checks in §8 are separate from the licence choice.

## 3. Candidate licences (options for D1; this file adopts none)

**Apache-2.0** — permissive, with an express patent grant and a defined
`NOTICE` mechanism. It aligns with the existing Apache-2.0 material
without changing that material's licence. Under §4, recipients receive
a copy of the licence, modified files carry prominent change notices,
and applicable upstream notices are retained. If the upstream work supplied
a `NOTICE`, carry the relevant attributions in one of the forms §4(d)
permits. Canonical text:
<https://www.apache.org/licenses/LICENSE-2.0>.

**MIT** — a short permissive licence without an express patent grant. It
requires retaining the copyright and permission notice for covered
copies and substantial portions. It does not remove third-party
obligations.

**AGPL-3.0** — network copyleft. Section 13 covers modified versions of an
AGPL-covered program that users interact with over a network. This can
require a source offer for modified hosted forks of a covered component.
Assess each service's licence and boundaries separately; this does not
automatically place a separate relay under the same licence. MIT and
Apache-2.0 are also open-source options.

Whichever option D1 chooses, it cannot be presented as covering the third-party
components in §5–§7, whose terms are fixed by their own authors.

## 4. Scope split: code, docs, assets

- **Code** — include the Python service, tooling and tests, the TypeScript
  clients, the Rust/Tauri host under `desktop/src-tauri/`, scripts,
  service-worker code and build configs. `assets/kelp-pet/kelp-pet.js`
  and `build_pet.py` are code, distinct from generated artwork.
- **Docs** — `docs/`, `README.md` and the `landing/` prose: prose may take a
  documentation licence or the same grant as the code. Do not let it inherit
  by assumption.
- **Assets** — phone and desktop icons, `phone/public/blob.png`, the
  current and historical landing artwork, and the art in
  `assets/kelp-pet/`. Include the desktop's distributed copy at
  `desktop/src/public/assets/kelp-pet/`; it also contains a JS component,
  not just artwork. Confirm redistribution rights per asset rather than
  relying on comments and generation scripts as a licence.
- See the [avatar licence preflight](spikes/2026-11-avatar-licensing.md)
  for candidate reuse terms. It does not select or clear specific assets.

These examples are not exhaustive. Classify the full `git ls-files` inventory
by rights and release scope, including third-party exceptions and duplicate
copies, before applying package or repository licence metadata.

## 5. Third-party material already in the tree

- [`third-party/OpenMausBot/LICENSE`](../third-party/OpenMausBot/LICENSE) is
  Apache-2.0, with the upstream
  [`third-party/OpenMausBot/NOTICE`](../third-party/OpenMausBot/NOTICE) tracked
  beside it. [`third-party/Geist/LICENSE`](../third-party/Geist/LICENSE) is
  OFL-1.1.
- [`chat/cards.py`](../chat/cards.py) :515–517 says `approval_rationale` adapts
  OpenMausBot's `PendingApproval`/`ApprovalCard` plain-language pattern.
  Whether that is a direct copy, a modified file, or a pattern re-expressed
  from scratch is **not** settled here — and it matters, because Apache-2.0
  §4(a), §4(b) and §4(d) attach differently in each case. Audit before
  release.
- [`phone/NOTICE.md`](../phone/NOTICE.md) recites adapted patterns and
  historical design sources, including the retired `netlify/mobile/`
  tree. Check the reuse before relying on its claim that the NOTICE is
  reproduced "in spirit" or that the recital covers §4(b). If §4 applies,
  supply a licence copy and applicable notices, and put change notices
  in any modified upstream files. If the reuse is independent, make the
  attribution describe that accurately.
- The upstream NOTICE text also cites `t3-code`, `cache-tax`, `models-dev`,
  `opencode` and `puppeteer` licence files, but only the OpenMausBot and Geist
  directories exist under `third-party/`. Audit what is present, and do not
  read this checklist as an exhaustive dependency audit.

## 6. noVNC 1.7.0 (MPL-2.0)

- [`desktop/package.json`](../desktop/package.json) lists `@novnc/novnc`
  as a direct dependency. The
  [lockfile](../desktop/package-lock.json) resolves `1.7.0` with
  `"license": "MPL-2.0"`.
- [`desktop/src/useViewer.ts`](../desktop/src/useViewer.ts) imports it
  dynamically inside the connect path. The emitted chunk, preload and
  distribution behaviour have not been inspected.
- MPL-2.0 is file-level copyleft. If the emitted desktop artifact carries
  noVNC in executable or minified form, recipients must be told where to get
  the MPL-covered source; the Mozilla FAQ entries for this are Q8 (unmodified
  MPL code inside a larger work) and Q16 (minified JavaScript counts as
  executable form), both confirmed against
  <https://www.mozilla.org/en-US/MPL/2.0/FAQ/>. Confirm against the artifact
  that is actually emitted; this file does not assert the bundle's contents.
- [`third-party/OpenMausBot/NOTICE`](../third-party/OpenMausBot/NOTICE) :60–63
  describes that upstream project's `public/novnc-NOTICE.txt`. No
  equivalent path is tracked here; confirm how Kelp's own artifact gives
  notice and where it points to the source.

## 7. Fonts and art

- [`phone/public/fonts/OFL.txt`](../phone/public/fonts/OFL.txt) sits beside
  `inter-latin.woff2`, `newsreader-latin.woff2` and
  `newsreader-italic-latin.woff2`. OFL-1.1 requires the licence to travel with
  the fonts; Vite copies `public/` verbatim (`phone/vite.config.ts` sets no
  filter), so confirm the licence is still there in the built output.
  The [service worker](../phone/public/sw.js) precaches the font files but
  not that text file. Check that users can access notices offline.
- In contrast, `phone/NOTICE.md` and `third-party/` are not under
  `phone/public/`, and the current Vite configuration adds no copy step
  for them. If the reuse audit finds that the PWA needs notices from
  those paths, package them and verify the emitted PWA rather than relying
  on the source notice.
- Sprite and art rights remain open (§4), independent of the code licence.

## 8. Per-artifact release check

Repeat for each artifact: server image, desktop build, phone PWA, landing site.

1. Inventory what the build actually outputs. Start from its lockfiles,
   manifests and the installed packages, and include fonts, models and art.
2. Map each item to its licence and to the notice text that must accompany it.
3. Confirm the licence text and notices accompany the artifact in the
   form permitted by the applicable terms, not merely in the Git tree.
4. For MPL-style components, confirm the source-availability notice (§6).
5. For Apache-2.0-derived code, confirm modification notices and retained
   attribution (§5).
6. Record the inventory in the release notes so the next release can diff
   against it.

Before publishing an image, resolve what applies to `chat/cards.py` (§5).
The Dockerfile currently copies that file but not the tracked OpenMausBot
licence or NOTICE. If attribution is required, add it to the build context
and runtime image, and inspect the resulting artifact. Do not assume
dependency notices in the virtual environment cover this material.

## 9. Out of scope here

Writing the `LICENSE`/`NOTICE` files, editing `Dockerfile`, `.dockerignore`,
`pyproject.toml` or `package.json`, moving files, and deciding on avatar
reuse are separate follow-ups.

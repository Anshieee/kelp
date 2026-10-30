# Kelp landing preview

Static HTML, CSS, and JavaScript. Serve the repository root locally, then open `/landing/`:

```sh
python3 -m http.server 4277 --bind 127.0.0.1
```

The page is not connected to the assistant. All question and approval examples are local simulations. Dashboard URLs are intentional placeholders; final service and privacy details are still required before launch. Google Fonts is the only external runtime dependency. No deployment configuration was changed.

## Design context

- `PRODUCT.md`: supported product facts and explicit constraints.
- `DESIGN.md` and `.impeccable/design.json`: the implemented visual system.
- `.impeccable/surfaces/index-html.md`: direction contract and concept-roll record.
- `.impeccable/review/finish-review.md`: fallback finish-review verdict and its limitations.

## Local verification

The verification runner expects Node.js, Playwright, axe-core, and Chromium. Keep tooling dependencies outside the repository:

```sh
npm install --prefix /tmp/opencode/kelp-landing-qa playwright axe-core
NODE_PATH=/tmp/opencode/kelp-landing-qa/node_modules \
  node landing/.impeccable/verify.cjs
```

Run that command from the repository root while the local server is running. Override `CHROMIUM_PATH` or `KELP_QA_URL` if needed. The runner exits nonzero on failure and writes `.impeccable/review/verification.json` plus screenshots.

Coverage: page and all feature-dialog overflow at ten widths (`320–1920px`); eleven axe WCAG A/AA states; starter questions; custom and empty input; attachment and microphone disclosures; no network requests during interactions; approval and rejection; storage-notice persistence; privacy dialog; keyboard boundaries, Escape, and restored focus; manual pause and reduced motion. It does not replace screen-reader testing or real-device QA.

Screenshots are local browser-capture evidence, not shipping illustration assets. No stock or generated raster assets are used by the page. The incumbent and taste-reference captures remain local and are excluded from version control.

## Verification result

The recorded Chromium run has no test failures, no page errors, no horizontal overflow, and no axe violations in its tested states. Actual body-text contrast ratios are `7.77:1` on ivory, `5.75:1` on mint, `6.85:1` on pale sage, and `6.59:1` on butter. Forest text on ivory is `11.58:1`.

The mechanical design detector reports the explicitly pinned ivory/Newsreader/Inter choices, a non-text analog-clock inset, and mutually exclusive modal shadows; these are evaluated in the finish review rather than silently suppressed. Nested subagent review was blocked by the harness depth limit, so the finish review and documentation used Impeccable's inline fallback. The original parent STEP 1–5 text was not present in the child session; execution follows the explicit delegated brief and repository product evidence.

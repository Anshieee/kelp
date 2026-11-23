# landing/

A one-page holding site while Kelp is rebuilt in public. It replaces the old
marketing page, which pointed visitors at a demo that no longer exists. A real
landing page comes back once Kelp is ready for production users.

- `index.html` — "Kelp is cooking" with a pre-register form.
- `thanks.html` — where the form lands after a submission without JavaScript.
- `styles.css`, `kelp.svg` — no external fonts, scripts, cookies or analytics.

Netlify adds one script the repo does not contain: on the free plan it injects
`/.netlify/scripts/hud?variant=public`, the "Netlify badge" in the bottom-right
corner. It makes no network requests of its own, stores only its dismissal
state in `localStorage`, and its links carry Netlify `utm_*` parameters. The
script ignores "Hide this badge" when `data-account-tier` is `free`, and the
site's `hud_enabled` setting (already `false`) controls the separate owner
toolbar, not this badge. On a paid plan visitors can hide it; to serve the page
with no injected script at all, host it elsewhere.

## Pre-register form

The form uses [Netlify Forms](https://docs.netlify.com/forms/setup/). With form
detection on (site setting `processing_settings.ignore_html_forms: false`, enabled
2026-11-23), Netlify finds `data-netlify="true"` on each deploy and stores submissions
(email, optional "what would you want Kelp to do") under the site's **Forms**
tab. `netlify-honeypot="company"` drops submissions that fill the hidden field. Netlify's spam filter can also
flag real sign-ups (the 2026-11-23 test with an `example.com` address landed in
**Spam**), so check that list too.
The page promises the email is used only to say Kelp is ready to try; keep it
that way.

Each new submission is emailed to the Netlify account owner through a form
notification (`submission_created`, type `email`, added 2026-11-23). Netlify
sends it only for verified submissions, so anything in **Spam** is not emailed.
The form's `email` field becomes the notification's Reply-To. Change the
recipient under **Forms → Submission notifications**.

## Deploy

The `kelp-landing` Netlify site is not linked to git, so merging does not
publish. Deploy this folder by hand:

```sh
npx netlify-cli deploy --dir landing --prod --site kelp-landing
```

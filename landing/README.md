# landing/

A one-page holding site while Kelp is rebuilt in public. It replaces the old
marketing page, which pointed visitors at a demo that no longer exists. A real
landing page comes back once Kelp is ready for production users.

- `index.html` — "Kelp is cooking" with a pre-register form.
- `thanks.html` — where the form lands after a submission without JavaScript.
- `styles.css`, `kelp.svg` — no external fonts, scripts or trackers.

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

## Deploy

The `kelp-landing` Netlify site is not linked to git, so merging does not
publish. Deploy this folder by hand:

```sh
npx netlify-cli deploy --dir landing --prod --site kelp-landing
```

# landing/

A one-page holding site while Kelp is rebuilt in public. It replaces the old
marketing page, which pointed visitors at a demo that no longer exists. A real
landing page comes back once Kelp is ready for production users.

- `index.html` — "Kelp is cooking" with a pre-register form.
- `thanks.html` — where the form lands after a submission without JavaScript.
- `styles.css`, `kelp.svg` — no external fonts, scripts or trackers.

## Pre-register form

The form uses [Netlify Forms](https://docs.netlify.com/forms/setup/). Netlify
finds `data-netlify="true"` when the site is deployed and stores submissions
(email, optional "what would you want Kelp to do") under the site's **Forms**
tab. `netlify-honeypot="company"` drops submissions that fill the hidden field.
The page promises the email is used only to say Kelp is ready to try; keep it
that way.

## Deploy

The `kelp-landing` Netlify site is not linked to git, so merging does not
publish. Deploy this folder by hand:

```sh
npx netlify-cli deploy --dir landing --prod --site kelp-landing
```

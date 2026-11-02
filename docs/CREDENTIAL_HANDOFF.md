# Private credential handoff and vault

This backend lets a trusted cloud-browser worker pause on a login or secret
form, ask the owner through an authenticated app, privately fill the form, and
optionally save those values in an encrypted local vault. The browser may ask
again for a verification code as a new request. No mobile or desktop UI is
included. No credentials belong in chat messages, tool arguments, or model prompts.

## Components and trust

- `chat/credentials.py`: durable request state and encrypted vault, isolated
  from `ChatStore`, conversation history, model tools, and SSE replay.
- `chat/routers/credentials.py`: owner API and separate worker API.
- `chat/credential_browser.py`: actual Playwright/CDP bridge for an existing
  browser. It reads form metadata, validates the current form, fills fields,
  and optionally clicks the specifically requested submit button.
- `PrivateBrowserSession.screenshot()` refuses observation while private entry
  is active. A computer agent must use this wrapper, and stop all other DOM,
  screenshot, network-body recording and tool observation for this page until
  the owner releases it. A caller holding the raw Playwright page can bypass
  the wrapper; this is a trusted-worker contract, not an OS sandbox.

The server and worker process are trusted with plaintext briefly. Fernet from
`cryptography` encrypts submitted payloads and saved values at rest. The vault
key is supplied separately through the environment, never written alongside
its database. This is not end-to-end encryption against the server operator,
OS process memory, a compromised worker, or the destination website. A website
can read values entered into it. Never enable Playwright traces, video, request
body logging, or model screenshots during this flow.

## Enable on the server

The feature is off by default. Generate a Fernet key with
`python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`
and a separate random worker token with
`python -c 'import secrets; print(secrets.token_urlsafe(48))'`.
Store them using the deployment's protected environment-file/secret manager:

```text
KELP_CREDENTIALS_ENABLED=1
KELP_VAULT_KEY=<generated Fernet key>
KELP_BROWSER_WORKER_TOKEN=<separate random token, at least 32 characters>
KELP_BROWSER_ALLOWED_ORIGINS=https://github.com,https://example.com
```

Use exact HTTPS origins, without paths, query strings, userinfo, wildcards or
nonstandard ports. A worker token must differ from the owner's root token.
`make check-serve` validates these settings without opening the vault. The
server refuses to unlock an existing vault with a different key.

The worker receives only `KELP_BROWSER_WORKER_TOKEN`, not the vault key, owner
token, or model-provider key. This first version supports one trusted worker
identity and the repository's existing single-owner account, not tenant isolation.
Terminate TLS before exposing the server to an app or worker over a network.
Both APIs return `Cache-Control: no-store`; the existing cookie CSRF rules
protect owner mutations. Worker endpoints accept only their explicit bearer
credential, never cookies or browser-origin calls.

## Owner/app contract

Use the normal owner bearer token or signed-in session. A frontend renders only
these APIs, outside its chat transcript, as plain text:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/credential-requests` | Poll bounded request metadata and status for app notifications |
| GET | `/api/credential-requests/{id}` | Show exact origin, fields, submit intent/label, deadline and status |
| POST | `/api/credential-requests/{id}/submit` | Send `{"values":{"username":"…","password":"…"},"save":false}` privately |
| POST | `/api/credential-requests/{id}/submit` | Or explicitly approve reuse with `{"vault_id":"vault_…"}` |
| POST | `/api/credential-requests/{id}/deny` | Reject and erase any undelivered payload; body `{}` |
| POST | `/api/credential-requests/{id}/release` | Owner authorizes agent observations to resume after completion, rejection or failure; body `{}` |
| GET | `/api/credential-requests/vault` | Saved IDs, origins and field roles only; no plaintext export |
| DELETE | `/api/credential-requests/vault/{id}` | Delete a saved credential |

Field IDs are stable identifiers such as `username`, `password`, or `code`.
Kinds are `username`, `password`, `otp`, or `secret` (for an API key or similar).
The owner sees no worker selectors. Show the exact destination origin and
whether the worker will only fill or will also submit the form. A saved item
can be reused only after a fresh owner submission and only for the same origin
and matching field IDs/roles. There is no automatic vault autofill or "remember
approval forever". Saving a request containing an `otp` is refused entirely;
save a password step separately from the later OTP step.

`completed`/`outcome=submitted` means a form was submitted, not that login
succeeded. The owner or a later non-secret page check must establish success.
For SMS or mobile approval, request the site code first using the browser, then
create a new handoff for the visible code field. Never persist an OTP.

## Worker contract

Use `Authorization: Bearer <worker token>` only:

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/credential-worker` | Register an observed form request |
| GET | `/api/credential-worker/{id}` | Poll status (no secret values) |
| POST | `/api/credential-worker/{id}/claim` | Deliver values once, bound to current session/origin/form |
| POST | `/api/credential-worker/{id}/finish` | Record `{"outcome":"filled"}`, `submitted`, or `failed` |

The Playwright adapter produces the register body from the actual page:
`session_id`, `origin`, a SHA-256 `binding`, `fields` (ID, kind, display label,
CSS selector), and `submission` (enabled flag and observed button label).
The fingerprint covers document time origin, page URL, form action/method and
field metadata. Every field and the optional submit button must be unique,
visible, enabled, in the same top-level form; submissions must be HTTPS POST
to the page's own origin. Cross-origin frames and non-form login widgets are
not supported yet. This guard cannot detect every hostile same-page script.

Use `CredentialWorker.request(session, fields, submit_selector)` to pause and
create the request. Once the app submits values, `deliver(session, id)` checks
the current form before claiming them and before filling each field. Changed
forms fail closed. To resume, the owner calls `/release`, then
`CredentialWorker.resume(session, id)` clears any remaining tracked input values
and unlocks observation. Do not call the session's internal release method from
an agent. On a partial failure keep the page private and request owner review.

The claim clears the encrypted pending payload transactionally before delivery.
A second claim fails. A lost HTTP response or worker crash requires a fresh
handoff; never retry a possibly completed fill or form submission automatically.
A crashed claimed request becomes failed at next server start. Pending encrypted
submissions can survive restart until their ten-minute expiry.

## Run an existing cloud browser worker

Install `pip install -e '.[browser,dev]'` on the worker host and install Chrome
through `python -m playwright install chromium` if needed. The server image does
not install a browser. Keep the remote-debugging port local to the worker host.
Provisioning a cloud VM, pairing a mobile app, screen streaming, native desktop
control, Telegram and push notification delivery are separate integrations.

Create a JSON file containing field descriptions only (never values):

```json
[
  {"id":"username","label":"Username","kind":"username","selector":"input[name=login]"},
  {"id":"password","label":"Password","kind":"password","selector":"input[name=password]"}
]
```

Attach to an explicitly selected existing page:

```bash
python -m chat.credential_browser --server https://kelp.example.com \
  --cdp http://127.0.0.1:9222 --page 0 --fields login-fields.json \
  --submit-selector 'input[type=submit]'
```

Without `--submit-selector`, it only fills. The CLI prints ID, origin and status,
never values, HTML, screenshots or exception text. It polls the owner decision
and waits for explicit owner release before exiting successfully. If the worker
is stopped while a page is private, the surrounding agent must keep that browser
session private or close it, rather than silently attach with an unguarded page.

## Storage, cleanup and verification

The new `credentials.db` sits beside `chat.db`, mode `0600`, with private
journaling and secure-delete enabled. Payload encryption is authenticated and
bound to the record ID, origin and field roles. A copied ciphertext from another
request/credential cannot be used under a different record. Plain values are
never stored in chat, request logs, audit rows or model usage records. Audit rows
hold only handoff IDs and fixed outcomes. Python/browser process memory cannot
be reliably zeroized; clearing references is not a zeroization guarantee.

Request values expire after ten minutes, are discarded on denial/claim, and
expired payloads are cleared on the next broker operation (workers poll every
second). Terminal metadata is pruned on broker operations after one day beyond
the deadline. Records are bounded to 128 active/private requests, 4096 retained
request rows and 256 saved credentials. Vault items persist until owner deletion;
request expiry does not delete a separately saved vault entry. Back up this file
only with encrypted storage and protect/restore the vault key separately. Key
rotation/recovery UI, external password managers and multi-worker tenancy are
not included. Losing the key loses access to the saved credentials.

Run `pytest tests/test_credentials.py` for protocol, privacy, race, expiry,
origin binding and vault tests. The optional real-browser checks run with
`KELP_BROWSER_SMOKE=1 pytest tests/test_credential_browser_smoke.py` after installing
Chromium; they test real field filling, hidden fields, changed forms and a
cross-site form action without sending real credentials anywhere.

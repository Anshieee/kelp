# Approved showcase browser

`browser.act` is discovered from `skills/builtin/browser_act/manifest.json` by
`chat.server._tool_specs`. Every action uses the existing single-use chat
approval. No server tool registration or `ExecGate` is added.

## Scope and privacy

- Only `https://github.com/Anshieee/kelp` and its `main/README.md` document are
  navigable. GitHub static assets can load from `github.githubassets.com`.
  Requests outside those paths/origins, navigation redirects outside scope,
  and non-GET requests are blocked while an action runs. New websocket
  connections are closed. Deployment host policy can narrow this further,
  including reads from an already-loaded page.
- Actions are `open`, `move_pointer`, `click_readme`, `read_heading`, and
  `read_text`. No arbitrary selectors, typing, forms, downloads, screenshots,
  executable commands, or model-supplied CDP endpoints.
- The endpoint comes only from server-side `KELP_DEMO_CDP_URL`. It must be an
  explicit HTTP loopback/RFC1918/ULA address with an unprivileged port. DNS
  hosts, public IPs, metadata/link-local addresses, credentials, query strings,
  and paths are refused. Discovery cannot redirect or change websocket host/port.
- CDP process metadata must prove `--user-data-dir` ends in
  `kelp-demo-browser`. Chromium must have `--enable-automation`, otherwise
  profile verification fails closed. Exactly one browser context is required;
  existing service workers are refused.
- `PrivateBrowserSession.begin` refuses that demo profile before inspecting or
  filling a credential form, including in a separate credential-worker process.
  Existing credential browsers also need `--enable-automation` for profile
  verification. In-process private page handles additionally call the existing
  `assert_observable()` guard before any browser action or observation. Released
  credential pages remain excluded from this skill.
- Keep the demo profile unsigned-in and never enter secrets manually. Only
  visible README text is returned, capped at 240 heading / 1200 text characters.
  Inputs, forms, code blocks, hidden/private nodes, known server secret values
  and common token-shaped strings are omitted/redacted. This is not a general
  secret classifier for arbitrary text; the public-document scope and profile
  separation are the primary boundary.
- Browser actions are audited by the skill runtime. Chromium's separate driver
  transport is not byte-metered by the Python HTTP egress ledger. Browser page
  code can run between attachments, when action interception is not installed.
  Treat this as a dedicated public showcase, never a general signed-in browser.
- Repeating `open` on the same URL or `click_readme` on README does nothing.
  Movement and reads have no persistent external side effects. The manifest
  therefore declares `idempotent: true`. `EXEC_AVAILABLE` stays `False` and
  manifests declaring `execute` remain rejected.

## VM setup still required

The VM rehearsal was stopped at the Docker permission boundary. SSH succeeded
using the approved connection and the existing X11 display `:1` was present.
The first Docker inspection returned:

> permission denied while trying to connect to the docker API at unix:///var/run/docker.sock

No privilege escalation was attempted. No VM browser, container, CDP listener,
or deployment was changed by this branch. An authorized operator must resolve
container access before continuing.

After access is resolved, use a dedicated unsigned-in Chromium profile named
`kelp-demo-browser` on display `:1`, with `--enable-automation`,
`--remote-debugging-address=127.0.0.1`, and `--remote-debugging-port=9222`.
Do not use `--no-sandbox` or expose the debugging port publicly.

Give the Kelp container access to that loopback namespace through an approved
deployment change, for example a narrowly reviewed host-network configuration
with all application listeners still loopback-only behind the existing proxy.
A bridge container cannot reach VM loopback by using its own `127.0.0.1`.
Do not solve that by publishing CDP on `0.0.0.0`. Set the server environment to
`KELP_DEMO_CDP_URL=http://127.0.0.1:9222` only after proving the private path from
inside the container. Install the optional `.[browser]` dependency in its image.

Rehearse from real Kelp chat, approving each bounded action:

> Use browser.act in session demo, page 0. Open https://github.com/Anshieee/kelp, move the visible pointer to x=450 y=300, click the README link, then read the README heading and report it exactly. Use the browser tool, not github.read or web.search. Wait for my approval for each action.

This is the exact proposed rehearsal prompt, not a VM-verified working prompt.
The showcase path is the repository already allowlisted by Kelp's shipped GitHub
skills. If the intended showcase is another repository, change the explicit
document allowlist and manifest together before deployment.

The pointer is an orange in-page pointer following actual Playwright mouse
events. CDP alone does not move the desktop hardware cursor. The marker is
visible in the same Chromium window carried by VNC/Computer, and flashes green
on click. Confirm Storm sees the marker move and the README click in that same
window, then compare the chat heading with the rendered README. Connection or
navigation alone is not acceptance evidence.

## Local checks

`python -m pytest tests/test_browser_act.py` checks the manifest, model discovery,
approval refusal, endpoint/discovery bounds, profile isolation, credential guard,
request interception, scoped page selection and retry-safe actions.

`KELP_BROWSER_SMOKE=1 python -m pytest tests/test_browser_act_smoke.py tests/test_credential_browser_smoke.py`
uses real local Chromium and loopback CDP with a synthetic showcase document.
It exercises approved open/move/click/read actions, checks the visible marker's
position, excludes hidden/input/code text, keeps the original browser window
alive after detaching, and covers private credential form binding.
The marker screenshot is `/tmp/opencode/kelp-demo-browser-pointer.png`.
These local tests do not prove remote VNC visibility or model/chat behavior.

Verified locally for this branch:

| Check | Result |
| --- | --- |
| `python -m pytest` | 1225 passed, 5 opt-in Chromium checks skipped |
| Explicit Chromium smoke command above | 5 passed |
| `ruff check .` | Passed, no findings |
| `mypy chat kernel openshell skills payments evals demo tools` | 113 source files, no issues |
| `npm run check:demo` | Passed |
| `npm run test:demo` | 11 passed, 1 optional Postgres check skipped |
| `npm run test:mobile-contract` | 14 passed, no skips |
| `git diff --check` | Passed |

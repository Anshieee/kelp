# ADR 0004: a desktop companion with a local body and a local-or-Boat brain

- **Status:** Proposed
- **Date:** 2026-11-23
- **Issue:** Phase 0 product direction (planning gate; Phase 1 waits on it)
- **Related:** [ADR 0002](0002-sharing-and-identity.md) (single-owner identity),
  [ADR 0003](0003-service-control-layer.md) (frozen service contract),
  [`docs/DESKTOP_SCOPE.md`](../DESKTOP_SCOPE.md),
  [`docs/PRODUCT_BRIEF.md`](../PRODUCT_BRIEF.md), and
  [`docs/THREAT_MODEL.md`](../THREAT_MODEL.md)

## 1. Context

The desktop app today is a thin client. `docs/DESKTOP_SCOPE.md` (locked
2026-10-30) fixes it to one Nebius-hosted server at a single URL in
`desktop/src/config.js`, with no bundled sidecar and no runtime URL switcher. It
lists voice input and a bundled server sidecar as out of scope. The Rust bridge
`desktop/src-tauri/src/bridge.rs` owns the cookie jar, the CSRF pair and the SSE
stream, and the webview only sends commands and listens for `chat-event` frames.
The pet overlay mirrors chat state and nothing more: `desktop/pet_state.py` maps
chat events to pet states, and the panel's screen button is inert because no
capture code exists.

The product direction in [`docs/PRODUCT_BRIEF.md`](../PRODUCT_BRIEF.md) is an
animated companion that sees the screen, talks by voice, and reacts to what
the user is doing. The thin-client, single-server model does not fit that, for
these reasons:

- **Screen and microphone are local.** A server cannot see the user's screen or
  hear the user. Capture, audio and OS permissions must run on the user's machine.
- **A single hosted server is the wrong default for screen data.** Sending screen
  frames to one shared host as the only option conflicts with local-first. Some
  users want everything to stay on their machine. Others want the brain in a
  sandbox they own.
- **No public hosted instance is planned until production** (decision D,
  2026-11-23), so a design that depends on one hosted server has nothing to
  connect to for most users.
- **Setup friction.** The brief requires zero repeated setup. A client that
  points at a server the user must already run, or a CLI they must configure,
  cannot meet that.

## 2. Decision

Split Kelp into a body and a brain, and let the user choose where the brain runs.

**Decisions recorded 2026-11-24.** The owner answered the open questions in
[`docs/PRODUCT_BRIEF.md`](../PRODUCT_BRIEF.md) section 17. The owner has not
accepted this ADR as a whole, so its status stays Proposed. These items are
decided:

- **Local installer: option (a)** (answer 2). Item 6 below.
- **Reaching a Boat brain: SSH local port forward** (answer 3). Item 8 below.
- **Where local secrets live: the OS credential store, from Rust only**
  (answer 4). Item 9 below.
- **Catalog signing: ship with digest pinning first** (answer 6). Kelp's own
  release-pinned entries ship first (Phase 1 voice models). Publisher signing
  (`skills/signing.py`) is wired in before the catalog accepts any entry from
  outside Kelp's own release, and before the full resolver ships in Phase 3.
- **Remote approval channel: push to Ketock's native mobile app first, a Telegram
  bridge second** (answer 9). Item 10 below.
- **Frames, screen reactions and screen control in every mode; desk pet
  optional** (answer 11). Item 11 below.

1. **The body is always local.** It is the avatar rendering, screen capture,
   microphone and speaker, OS permission handling, and the approval UI. It lives
   in the Tauri app under `desktop/`. A cloud VM cannot see the user's screen, so
   the body never moves.
2. **The brain is local or in a Boat sandbox.** The brain is the kernel
   (`kernel/`), the skills (`skills/`), the approval gate (`openshell/`), the
   memory and store (`chat/`), and the scheduler. In Local mode it runs as a
   sidecar process on loopback. In Boat mode it runs inside a Boat sandbox that
   the user owns. The user chooses, and the desktop app connects to whichever was
   chosen.
3. **One API between body and brain.** It is the existing chat API in
   `chat/api.py`, with SSE, reached through `bridge.rs`. The body talks to a local
   sidecar and to a Boat brain with the same calls. No second protocol is
   introduced. In Boat mode the body sends only what the user enabled, and screen
   frames are downscaled, sent on demand, and not written to disk.
4. **The capability resolver goes through the existing approval gate.** When a
   request needs a model, skill, login or OS permission that Kelp lacks, Kelp
   writes a setup plan and shows it as one approval card in the existing approval
   flow when the plan grants new access. It then performs the steps itself and
   verifies them. The model names
   catalog entry ids only; the resolver builds the plan and the card text from
   catalog metadata, so model-written or injected text never appears on the card.
   The card is a new approval kind bound to the hash of the whole plan, because
   today's approvals (`openshell/approvals.py`) are single-use and bound to one
   command and argument hash. Installs come from an allowlisted catalog of pinned
   entries (skills, model weights, runtime dependencies), extending the digest
   pinning that `skills/loader.py` applies to skill packages today. Anything
   outside the catalog is not installed by Kelp. Kelp may propose a catalog entry
   for the project to review
   ([THREAT_MODEL §9, T2](../THREAT_MODEL.md#9-self-setup-the-capability-resolver)).
   [THREAT_MODEL §9, T1](../THREAT_MODEL.md#9-self-setup-the-capability-resolver)
   refines this item: the card appears only when the plan grants new access.
5. **Screen data is untrusted input.** Text from the screen or from a web page
   can never authorise an action and can never trigger a setup step. The
   reaction loop that watches the screen has no tool access.
6. **Local installer: option (a).** The Tauri app bundles the Python brain as a
   sidecar, using a relocatable Python (python-build-standalone) managed by `uv`.
   It ships through signed per-OS installers plus a one-line script that fetches
   the same installer, and it updates through Tauri's updater. The brief
   compares this with a single-binary freeze, a `curl | sh` script alone, and a
   Docker container (brief section 6). The owner chose this option on
   2026-11-24. Unsigned Linux builds are fine while building in public. macOS and
   Windows signing and notarisation are required before general users are
   invited. The status of this ADR stays Proposed, because the owner has not
   accepted the whole ADR.
7. **The local sidecar is never open.** It binds 127.0.0.1 only, on a random
   per-launch port, always requires the owner bearer, and rejects requests whose
   Host or Origin is not the expected loopback value. Any local process or browser
   page can reach loopback, so loopback alone is not an access control.
8. **A Boat brain is reached through an SSH port forward (planned).** The rules
   are written in
   [THREAT_MODEL §6, L4](../THREAT_MODEL.md#6-local-brain-boundary-local-mode).
   In short:
   - The brain inside the sandbox binds 127.0.0.1 only, like the local sidecar
     (item 7). It has no public port and no Boat public URL.
   - The desktop app reaches it through an SSH local port forward to the
     sandbox's loopback port. Boat documents this as `boat forward`, which binds
     127.0.0.1 on the user's computer by default and makes nothing public
     (<https://docs.boat.dev/ssh-access.md>). The forward's local end binds
     127.0.0.1 explicitly, on an ephemeral port, and the Host, Origin and bearer
     checks of item 7 apply to the Boat brain too.
   - The desktop generates its own ed25519 SSH key per device, authorises it on
     the sandbox through the Boat API, and pins the host key from the Boat API
     response. The machine, IP address and host key change after each resume and
     fork, so the app refreshes the endpoint and host key from the Boat API after
     every resume or fork. It never trusts a host key on first use, it never pins
     the brain by IP address, and it refuses to connect if the Boat API response
     has no host key.
   - Removing a device in the desktop app deletes its key from the sandbox
     through the Boat API. Where possible, the authorised key is restricted to
     forwarding the one loopback port. Whether Boat lets the app set
     `authorized_keys` options is unverified (see the follow-ups).
   - The owner bearer is still required on every request inside the tunnel.
   - Provider keys, the vault key and credentials stay inside the sandbox. The
     desktop stores three secrets in the OS credential store (item 9): its SSH
     private key, the Boat API key and the Boat brain's owner bearer. The Boat
     API key can authorise new SSH keys, so it is the highest-value desktop
     secret and never uses the plain-file fallback.
   - Not the Boat public URL (`boat host`): Boat terminates TLS for `on.boat.dev`
     and proxies to the sandbox
     (<https://docs.boat.dev/hosting/how-it-works.md>), so Boat would see the
     plaintext chat and the owner bearer. The default protection is a `_token`
     query parameter, so anyone with the full URL can open it
     (<https://docs.boat.dev/hosting.md>), and query strings end up in logs and
     history.
   - Not a mesh VPN (WireGuard or a hosted mesh): it adds an account or key
     exchange step for every user, which breaks zero setup. It can be an advanced
     option later.
   - The phone app and the messaging bridge do not use this tunnel (item 10).
9. **Local secrets are generated by Rust and kept in the OS credential store
   (planned).** The rules are written in
   [THREAT_MODEL §6, L5](../THREAT_MODEL.md#6-local-brain-boundary-local-mode).
   None of it is built.
   - The Rust side of the desktop app generates all secrets on first run from the
     OS random source. The user never sees, types or copies one.
   - Persistent secrets (`KELP_VAULT_KEY`, `KELP_CREDENTIAL_SIGNING_KEY`) are
     stored in the OS credential store from Rust only: macOS Keychain, Windows
     Credential Manager, or Linux Secret Service. The webview never reads them,
     which matches `desktop/README.md` (future at-rest storage belongs in
     `bridge_sign_in` on the Rust side).
   - The per-launch set is the owner bearer (`KELP_AUTH_TOKEN`),
     `KELP_CSRF_TOKEN`, `KELP_SESSION_SECRET` and `KELP_BROWSER_WORKER_TOKEN` for
     the local brain. It is generated fresh on every launch, held in memory,
     passed to the sidecar over stdin or an inherited descriptor (item 7 and
     THREAT_MODEL L1), and never stored.
   - `KELP_SESSION_SECRET` falls back to `KELP_AUTH_TOKEN`
     (`chat/server.py:813`), and device sessions persist across restarts
     (`.env.example:56`). In Local mode they are not expected to survive a
     restart. The desktop body signs in again automatically on each launch, with
     no user step.
   - The browser worker reads `KELP_BROWSER_WORKER_TOKEN` from the environment
     today (`chat/credential_browser.py:218`). Local mode needs new code so the
     worker gets it over stdin or an inherited descriptor, like the bearer.
   - A credential request left pending across a restart cannot be claimed with
     the old tokens and has to be requested again, the same outcome that
     [`docs/CREDENTIAL_HANDOFF.md`](../CREDENTIAL_HANDOFF.md) describes for a
     rotated signing key or worker token.
   - Lost credential-store entry. Today startup aborts when the vault key does
     not match an existing store (`chat/credentials.py:154-156`). The planned
     rule, which needs new code in the sidecar launcher: when the vault key is
     missing from the credential store but `credentials.db` exists, the launcher
     renames `credentials.db` aside with a timestamp (never a silent delete),
     generates a new key, and Kelp then asks for each login again as new access
     (one card). An encrypted export of the vault key can live in Settings for
     users who want it. It is never part of first run.
   - Linux without a Secret Service daemon (common on minimal Wayland
     compositors): a 0600 file stands in for the credential store, for the
     Local-mode persistent secrets only (the vault key and the credential signing
     key). It is created with `O_EXCL` and mode 0600 inside
     `$XDG_DATA_HOME/kelp/secrets/` (mode 0700), separate from the database
     directory, and Kelp's own backups exclude it. When a Secret Service later
     becomes available, Kelp moves both values into it and removes the file.
     Kelp shows a one-time notice in Settings, not a setup step. It is weaker
     than a credential store. The SSH private key, the Boat API key, the Boat
     brain's bearer, the Telegram bot token and the relay credential, end-to-end
     key and device records (item 10) never use it. Kelp offers to set
     up a Secret Service through the resolver (a catalog entry, one card). Until
     then, Boat mode and the Telegram bridge are unavailable.
   - Boat mode: the Boat brain's owner bearer is stored in the OS credential
     store too, because a Boat sandbox persists across resumes and the bearer
     cannot be per-launch. To rotate it, the desktop writes a new bearer into the
     sandbox over the SSH channel and restarts the brain. A second desktop reads
     the bearer over SSH once its own key is authorised. The relay credential,
     end-to-end key, device records and Telegram bot token of a Boat brain live
     inside the sandbox; the desktop sends them in over SSH at pairing.
10. **Remote approvals: push to Ketock's native app first, a Telegram bridge
    second (planned).** The brief describes the design in section 4.6, and the
    rules are in
    [THREAT_MODEL §6, L6 and
    L7](../THREAT_MODEL.md#6-local-brain-boundary-local-mode).
    A push relay is required, because Apple and Google push services accept only
    pushes signed with the app publisher's credentials. The brain keeps an
    outbound connection to the relay, and the relay authenticates it with a
    per-brain credential. The push carries no content, and the card details and
    the decision travel end-to-end encrypted between the paired phone and the
    brain. Each decision is bound to the card id, the argument hash and the
    expiry. A dropped or late relay message means the card expires and the action
    does not run. The Telegram bridge is outbound only (long polling), accepts
    messages only from the paired Telegram user id, and shows only the action
    type and target. A card is approvable from Telegram only when those fields
    fully determine the action, and it is not in a T4 category, not new access
    (T1) and not a screen-control card (S8). Remote channels can never create a
    standing approval or approve a T1 new-access card. Control of the user's own
    desktop starts and is approved only at the desktop app, and neither the
    phone nor Telegram can start it. The paired phone app may approve control of
    a Boat sandbox desktop (L6), and Telegram never may
    ([THREAT_MODEL §7, S8](../THREAT_MODEL.md#7-screen)). Neither channel uses
    the Boat tunnel (item 8).
11. **Screen frames, screen reactions and screen control in every mode; the
    desk pet is optional (decided 2026-11-24; planned, none of it built).**
    Every run mode, including a plan user with no provider key of their own,
    supports downscaled frames, screen-based reactions and screen control. The
    plan model gateway may carry frames for apps the user marked "may send to
    cloud" and stores none
    ([THREAT_MODEL §7, S3](../THREAT_MODEL.md#7-screen)). Screen control follows
    S8. Users may turn the desk pet off; Kelp then runs from a tray or menu-bar
    icon with the same capabilities.

## 3. What stays

- **Single owner (ADR 0002).** Local mode is still one owner with one identity,
  one approval authority and one spend ceiling. The sidecar does not make Kelp
  multi-user.
- **The service contract (ADR 0003).** The frozen shapes in
  [`docs/SERVICE_CONTRACT.md`](../SERVICE_CONTRACT.md) apply to the sidecar and to
  a Boat brain. The gate order (auth, tenant, entitlement, spend, provider) is
  unchanged.
- **The approval gate.** Every action, including installs and actions that run under a
  standing approval, still goes through `openshell/`: deny by
  default, approvals that move from pending to approved to consumed, a 600 s TTL,
  and the append-only scrubbed audit log.
- **Spend caps.** They keep failing closed (`kernel/usage.py`,
  [`docs/DEPLOY.md`](../DEPLOY.md) section 13.1). Reaction-loop model calls count
  against them.

## 4. What is superseded

This ADR supersedes the following in [`docs/DESKTOP_SCOPE.md`](../DESKTOP_SCOPE.md):

- **Owner decision 2**, "one Nebius-hosted server, no bundled sidecar" (exactly
  one configured server URL, no runtime URL switcher).
- **The out-of-scope items** "bundled server sidecar" and "voice input".

Everything else in that file is unchanged. That includes the Tauri choice, the
approval cards, the OS keychain rule, and the rule that diagnostics are local logs
only. The keychain rule is not implemented today: `desktop/README.md` records that
the old `keychain.js` and its Stronghold wiring were removed and nothing is stored
at rest, and that any future at-rest storage belongs on the Rust side inside
`bridge_sign_in`. Local mode has to add it there. A note at the top of `docs/DESKTOP_SCOPE.md` points here.

## 5. Consequences

**Good.** The avatar can see and hear, because the body is on the user's machine.
The user chooses between data staying local and a sandbox they own. The existing
chat API, approval gate, skills and spend caps are reused instead of rewritten.

**Packaging and signing work.** Bundling a Python runtime and its dependencies
makes the installer larger. Each OS needs code signing, and macOS needs
notarisation. The updater needs signing keys. Linux builds may stay unsigned
while building in public. macOS and Windows signing and notarisation are
required before general users are invited. Today the desktop app ships
unsigned dev builds with the updater off (`desktop/README.md`). The sidecar needs a
lifecycle: start, health check, restart, and a loopback port that does not
collide.

**OS permission work.** Capture differs on each platform: Wayland through
wlr-screencopy or xdg-desktop-portal depending on the compositor, macOS Screen
Recording and Accessibility, and Windows Graphics Capture. Each needs a prototype
before the design relies on it. The first one, on a wlroots compositor, captured
the screen with no prompt at all
([`docs/spikes/2026-11-screen-capture.md`](../spikes/2026-11-screen-capture.md)),
so Kelp's own capture code must enforce the opt-in and the indicator. Screen
control needs its own input mechanism per OS (brief §12).

**Threat model must grow first.** [`docs/THREAT_MODEL.md`](../THREAT_MODEL.md)
covers the server process (`kernel/`, `skills/`, `openshell/`, `chat/`). It does
not cover screen capture, voice, a sidecar on the user's machine, or a resolver
that installs software. It must gain sections for screen, voice and self-setup
before any capture code lands. Those sections now exist, with status planned (no
code enforces them yet): [local brain boundary](../THREAT_MODEL.md#6-local-brain-boundary-local-mode),
[screen](../THREAT_MODEL.md#7-screen), [voice](../THREAT_MODEL.md#8-voice) and
[self-setup](../THREAT_MODEL.md#9-self-setup-the-capability-resolver).
THREAT_MODEL section 5 notes that a compromised process is out of scope, and a
local sidecar makes that process the user's own machine; section 5 now says so
and points to section 6. Section 5 also says the skill
socket guard is cooperative at the Python layer and is backed by the deployment
boundary (no exec, container egress rules). A sidecar running as the user has no
such boundary, so Local mode needs OS-level confinement of the sidecar before
skills with network access run there.

**Auth and secrets in local mode.** The desktop app keeps no bearer at rest
today (`desktop/README.md`), and the credential settings `KELP_VAULT_KEY`,
`KELP_BROWSER_WORKER_TOKEN` and `KELP_CREDENTIAL_SIGNING_KEY` are supplied
through the environment
([`docs/CREDENTIAL_HANDOFF.md`](../CREDENTIAL_HANDOFF.md)). A local installer
must generate these and the owner bearer. Item 9 above decides where they live:
the persistent ones in the OS credential store, written from Rust only, and the
per-launch ones (including `KELP_SESSION_SECRET`) in memory. None of it is built
yet.

**The catalog depends on signing.** Digest pinning proves a package is unchanged
since install, not who published it. Publisher signing exists only as a spike
(`skills/signing.py`,
[`docs/SPIKE_PLUGIN_SIGNING.md`](../SPIKE_PLUGIN_SIGNING.md)). The owner decided
on 2026-11-24 that the catalog ships with pinning first: Kelp's own
release-pinned entries only (Phase 1 voice models), with publisher signing wired
in before the catalog accepts any entry from outside Kelp's own release and
before the full resolver ships in Phase 3. The resolver's catalog is only as
trustworthy as that step.

**Kelp will run a hosted service.** The owner's money answer (brief section 15)
means the Kelp project operates a hosted plan service for plan users: billing, a
model gateway, the push relay (item 10) and Boat provisioning on plans that
include it. The service is open source, and self-hosters run its components on
their own side instead. That is new for this project, and none of it exists yet.
The existing Razorpay wallet code may be reused for billing. A hosted plan's
model gateway will receive conversation text and downscaled frames for apps the
user marked "may send to cloud" (item 11), which the current privacy documents
do not describe (see the follow-ups).
Decision D (no public hosted instance until production) holds until the plan
service launches, and is revisited then.

**Boat limits apply to Boat mode.** Machines are in Germany, Finland and France
only, there is no GPU, auto-stop defaults to 1 h, and the IP changes on resume.
Boat is a third-party service, and its terms and prices can change. Local mode is
unaffected.

**Debt accepted.** This ADR documents a direction. No runtime code changes ship
with it, and no part of the sidecar, resolver or capture exists yet.

**Revisit when.** The Phase 0 spikes show that a platform cannot capture the
screen acceptably, the installer spike shows option (a) cannot be signed and
updated on every OS, or measured Boat numbers make Boat mode unworkable.

## 6. Alternatives considered

- **Stay a thin client to one hosted server.** This is the current design. It is
  simple, but the server cannot see the screen, it makes one shared host the only
  place screen data can go, and it needs a public hosted instance that is not
  planned before production. Rejected for the companion.
- **Cloud-only brain.** The body would always connect to a sandbox. It removes
  the local installer problem. It also removes offline use and the option to keep
  everything on the user's machine, and it makes a paid third-party service
  mandatory. Rejected, but Boat mode keeps it as one of the two choices.
- **Local-only brain.** It is the most private option and needs no third party.
  It leaves out users whose machines cannot run the brain well, and it gives Kelp
  no cloud computer of its own for long or risky computer-use tasks, so those
  would have to drive the user's real desktop. Rejected as the only mode.
- **Electron instead of Tauri.** `docs/DESKTOP_SCOPE.md` already compared them.
  Electron is heavier and has a wider plugin ecosystem. The Tauri app, the Rust
  bridge and the tests exist, and nothing in this direction needs Electron.
  Rejected.

## 7. Follow-ups

- Owner decision on the whole ADR. The installer option (a) is decided; see
  "Decisions recorded 2026-11-24" in section 2.
- Land Storm's 3 pet commits from `origin/kel-31-pet-motion-kit` (3bd69c5,
  4546460, 84fa565) on main by cherry-pick or a fresh PR, as an interim
  placeholder and not the identity. Do not merge the
  branch, because it is based on `desktop-demo`, which carries demo fixtures.
- Capture spikes on Wayland, macOS and Windows, and a local voice spike.
- Done (2026-11-24): screen, voice, self-setup and local-brain sections written in
  `docs/THREAT_MODEL.md` (§6 to §9), including OS-level confinement of the local
  sidecar. Status is planned: the enforcement code is still to build, and the
  sections must land before it.
- Design the plan-bound approval kind for the resolver, including what happens
  when a step outlasts the decision window.
- Design the push relay: the pairing flow (one QR scan from the desktop app), the
  device key created at pairing, the end-to-end encryption between phone and
  brain and the brain's long-term key for it, the per-brain credential the relay
  uses to authenticate the brain, the stored paired-device records, how
  the relay is hosted, and the open-source release for self-hosters. Reconcile
  it with Ketock's native app handoff, which says no second server and no new
  credential type, and whose definition of done says one configured server URL
  (`docs/KETOCK_NATIVE_APP_HANDOFF.md`, sections 2 and 3, and section 4
  definition of done item 1).
- Decide whether to widen which cards may be approved from Telegram. The default
  is that a card is approvable there only when the fields Telegram shows fully
  determine the action, and it is not in a T4 category, not new access (T1) and
  not a screen-control card (S8). Every other card shows "open in the app".
- Find the narrowest Boat API key that can resume and read one sandbox. Whether
  Boat offers a per-sandbox key is unverified. Until it is checked, use the
  narrowest preset that works.
- Check whether Boat lets the desktop app set `authorized_keys` options on the
  sandbox, so that its key can be restricted to forwarding one loopback port
  (`restrict,port-forwarding,permitopen="127.0.0.1:PORT"`). It is unverified.
- Design the hosted plan service: billing, a model gateway that stores no
  frames, the push relay and Boat provisioning on plans that include it (brief
  section 17, owner answer 11). Package the relay and gateway so a self-hoster
  can run them.
- Spike screen control (input) on each OS next to the capture spike (brief
  section 12, THREAT_MODEL S8). Include detecting user input to pause control
  and a global stop shortcut, because S8 does not let control ship on an OS
  without both.
- Before the plan ships, update the recipient table in
  [`docs/PRIVACY.md`](../PRIVACY.md) section 2, and the statements in
  [`docs/THREAT_MODEL.md`](../THREAT_MODEL.md) about telemetry and recipients
  (sections 1.1 and 10), to name the model gateway, the push relay and the
  Telegram Bot API as recipients. A hosted plan's model gateway would receive
  conversation text and downscaled screen frames, and Telegram receives the
  plaintext of bridge messages.
  Do the Telegram part before the bridge ships, even if the plan does not.
- Licence checks for Live2D, AIRI and Open-LLM-VTuber before any reuse.
- When accepted, change the Status line here and update the note at the top of
  `docs/DESKTOP_SCOPE.md`.

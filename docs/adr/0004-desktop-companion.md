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
animated avatar that sees the screen, talks by voice, and reacts to what the user
is doing. The thin-client, single-server model does not fit that, for these
reasons:

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
   frames are downscaled, sent on demand, and not stored by default.
4. **The capability resolver goes through the existing approval gate.** When a
   request needs a model, skill, login or OS permission that Kelp lacks, Kelp
   writes a setup plan and shows it as one approval card in the existing approval
   flow. It then performs the steps itself and verifies them. The model names
   catalog entry ids only; the resolver builds the plan and the card text from
   catalog metadata, so model-written or injected text never appears on the card.
   The card is a new approval kind bound to the hash of the whole plan, because
   today's approvals (`openshell/approvals.py`) are single-use and bound to one
   command and argument hash. Installs come from an allowlisted catalog of pinned
   entries (skills, model weights, runtime dependencies), extending the digest
   pinning that `skills/loader.py` applies to skill packages today. Anything
   outside the catalog needs an extra confirmation.
5. **Screen data is untrusted input.** Text from the screen or from a web page
   can never authorise an action and can never trigger a setup step. The
   reaction loop that watches the screen has no tool access.
6. **Local installer: option (a).** The Tauri app bundles the Python brain as a
   sidecar, using a relocatable Python (python-build-standalone) managed by `uv`.
   It ships through signed per-OS installers plus a one-line script that fetches
   the same installer, and it updates through Tauri's updater. The brief
   compares this with a single-binary freeze, a `curl | sh` script alone, and a
   Docker container (brief section 6). The status of this ADR is Proposed, so
   this choice is open until the owner accepts it.
7. **The local sidecar is never open.** It binds 127.0.0.1 only, on a random
   per-install port, always requires the owner bearer, and rejects requests whose
   Host or Origin is not the expected loopback value. Any local process or browser
   page can reach loopback, so loopback alone is not an access control.
8. **Minimum rules for a Boat brain.** TLS only, the owner bearer always
   required, no pinning by IP address (it changes on resume), and provider keys,
   the vault key and credentials stored only inside the sandbox. Whether the app
   reaches it through an exposed port or a tunnel is a blocking follow-up.

## 3. What stays

- **Single owner (ADR 0002).** Local mode is still one owner with one identity,
  one approval authority and one spend ceiling. The sidecar does not make Kelp
  multi-user.
- **The service contract (ADR 0003).** The frozen shapes in
  [`docs/SERVICE_CONTRACT.md`](../SERVICE_CONTRACT.md) apply to the sidecar and to
  a Boat brain. The gate order (auth, tenant, entitlement, spend, provider) is
  unchanged.
- **The approval gate.** Every action still goes through `openshell/`: deny by
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
notarisation. The updater needs signing keys. Today the desktop app ships
unsigned dev builds with the updater off (`desktop/README.md`). The sidecar needs a
lifecycle: start, health check, restart, and a loopback port that does not
collide.

**OS permission work.** Capture differs on each platform: Wayland through
xdg-desktop-portal with consent per session, macOS Screen Recording and
Accessibility, and Windows Graphics Capture. Each needs a prototype before the
design relies on it.

**Threat model must grow first.** [`docs/THREAT_MODEL.md`](../THREAT_MODEL.md)
covers the server process (`kernel/`, `skills/`, `openshell/`, `chat/`). It does
not cover screen capture, voice, a sidecar on the user's machine, or a resolver
that installs software. It must gain sections for screen, voice and self-setup
before any capture code lands. Its section 5 already notes that a compromised
process is out of scope, and a local sidecar makes that process the user's own
machine, so that boundary needs to be restated. Section 5 also says the skill
socket guard is cooperative at the Python layer and is backed by the deployment
boundary (no exec, container egress rules). A sidecar running as the user has no
such boundary, so Local mode needs OS-level confinement of the sidecar before
skills with network access run there.

**Auth and secrets in local mode.** The desktop app keeps no bearer at rest
today (`desktop/README.md`), and the credential settings `KELP_VAULT_KEY`,
`KELP_BROWSER_WORKER_TOKEN` and `KELP_CREDENTIAL_SIGNING_KEY` are supplied through
the environment ([`docs/CREDENTIAL_HANDOFF.md`](../CREDENTIAL_HANDOFF.md)). A local
installer must generate and store these and the owner bearer. Where they are
stored is not decided here.

**The catalog depends on signing.** Digest pinning proves a package is unchanged
since install, not who published it. Publisher signing exists only as a spike
(`skills/signing.py`,
[`docs/SPIKE_PLUGIN_SIGNING.md`](../SPIKE_PLUGIN_SIGNING.md)). The resolver's
catalog is only as trustworthy as that step.

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

- Owner decision on this ADR, and on the local installer option.
- Land Storm's 3 pet commits from `origin/kel-31-pet-motion-kit` (3bd69c5,
  4546460, 84fa565) on main by cherry-pick or a fresh PR. Do not merge the
  branch, because it is based on `desktop-demo`, which carries demo fixtures.
- Capture spikes on Wayland, macOS and Windows, and a local voice spike.
- Add screen, voice and self-setup sections to `docs/THREAT_MODEL.md`, including
  OS-level confinement of the local sidecar.
- Design the plan-bound approval kind for the resolver, including what happens
  when a step outlasts the decision window.
- Decide where the owner bearer and the vault key live in local mode.
- Decide how the app reaches a brain inside a Boat sandbox (blocking for Boat
  mode).
- Confirm the recommendation that publisher signing is wired in before the full
  resolver ships in Phase 3 (the Phase 1 voice-model resolver uses only entries
  pinned in Kelp's own release).
- Licence checks for Live2D, AIRI and Open-LLM-VTuber before any reuse.
- When accepted, change the Status line here and update the note at the top of
  `docs/DESKTOP_SCOPE.md`.

# Product brief: Kelp as a desktop companion (Phase 0)

- **Status:** Proposed, for owner review
- **Date:** 2026-11-23
- **Decision record:** [ADR 0004](adr/0004-desktop-companion.md)
- **Related:** [`docs/DESKTOP_SCOPE.md`](DESKTOP_SCOPE.md),
  [`docs/THREAT_MODEL.md`](THREAT_MODEL.md),
  [`docs/CREDENTIAL_HANDOFF.md`](CREDENTIAL_HANDOFF.md),
  [ADR 0002](adr/0002-sharing-and-identity.md),
  [ADR 0003](adr/0003-service-control-layer.md)

This brief says where Kelp is going and what Phase 0 has to settle before any
Phase 1 code is written. Phase 0 only decides. It builds nothing except the
small spikes listed in section 16. Where this brief makes a claim about the
current code, the file is named so the claim can be checked.

## 1. What Kelp becomes

Kelp moves from a chat and agent bot to an anime-style avatar that lives on the
user's desktop. The avatar is animated, can see and interact with the screen when
the user allows it, talks by voice, and reacts to what the user is doing. An
initial setup defines how Kelp generally acts and behaves, which is its persona.
Kelp should be able to take on whatever the user throws at it, and it is open
source and accessible to anyone. The user chooses where the thinking happens:
fully on their own machine, or in a Boat cloud sandbox that they own. The
existing server work (chat API, approval gate, skills, spend caps) stays and
becomes the brain behind the avatar.

## 2. Who it is for first, and the first daily loop

**First audience (recommendation):** developers and students who spend the day at
a desktop. They already live with terminals, editors and browsers, they can
tolerate an early build, and their screens give a companion real moments to react
to. This is a guess until the pre-register form (the holding page in `landing/`)
tells us otherwise. The form already has an optional "What would you want Kelp
to do for you?" field (`wish`), and its answers decide whether this
recommendation holds.

**First daily loop:**

1. The companion is present on the desktop while the user works.
2. It notices moments: the user looks stuck, something finished, the user has
   been idle, the user came back.
3. It reacts in its persona, usually with an animation and at most a short line.
4. It offers at most one useful action.
5. It acts only after the user approves.

## 3. Principles

- **Zero repeated setup.** Setup happens once and Kelp does the work itself (section 4).
- **Quiet by default.** The default is that Kelp rarely speaks. One key mutes it.
- **Nothing acts without the user's yes.** Every action goes through the existing
  approval gate in `openshell/`.
- **Screen content is untrusted input.** Text on screen or on a web page can
  describe a task but can never authorise one, and it can never start a setup step.
- **Local-first, cloud by choice.** The user picks Local or Boat. Neither is the
  fallback for the other.
- **Open source, with persona and avatar as shareable files.** Anyone can read,
  change and share what makes Kelp behave and look the way it does.

## 4. Zero repeated setup

This is the core of the owner's ask. The owner's goal is a seamless local setup.
The failure to avoid is one seen in other agent tools: Hermes Agent (Nous
Research) ships a one-line installer and a `hermes doctor` check, yet users report
per-feature friction in its setup wizard, for example GitHub issues #41046 (forced
portal onboarding) and #13301 (tools shown as "not configured" after being
enabled). Kelp should not ask the user to repeat setup for each new feature.

### 4.1 First run

- One installer and one choice: Local or Boat.
- Defaults come from detected hardware. If the machine can run a local model,
  Kelp picks one. If it cannot, Kelp asks for one provider sign-in, once.
- The target is talking to Kelp within a few minutes of installing, with no
  config files edited by hand.

### 4.2 Capability resolver

When a request needs something Kelp does not have, Kelp resolves the gap itself.
The missing thing can be a model, a tool or skill, a login, or an OS permission.
The steps are:

1. **Detect the gap.** The turn needs a capability that is not installed, not
   connected, or not permitted.
2. **Write a setup plan.** The plan lists what Kelp will install or connect, from
   where, at what pinned version or digest, and what access it grants.
3. **Show one approval card.** The whole plan is a single card in the existing
   approval UI. It is not one prompt per step.
4. **Perform every step.** After approval Kelp does the installing and
   connecting itself. The user does not edit config.
5. **Verify.** Kelp runs a doctor-style self-check and reports success or the
   exact step that failed.

### 4.3 Remember once

Approved capabilities, credentials and OS permissions persist, so the same setup
is never asked twice. Credentials go through the existing credential handoff and
encrypted vault ([`docs/CREDENTIAL_HANDOFF.md`](CREDENTIAL_HANDOFF.md)), never
through chat messages or model prompts. A capability is asked about again only if
its scope changes.

### 4.4 Safety limits

- Kelp installs only from an allowlisted catalog of signed or pinned skills and
  packages. The pinning mechanism exists today in `skills/loader.py`. A package
  digest is a SHA-256 over the sorted file paths, modes and contents
  (`package_digest`). The pin is recorded as name, version and digest in
  `plugins.lock.json`. At load time the loader recomputes the digest and refuses
  to start on a mismatch, and it refuses an unpinned package unless the operator
  sets `KELP_PLUGINS_ALLOW_UNPINNED=1` for a local experiment. `find_pinned_plugin`
  ignores any package that is invalid or not pinned.
- A pin proves the package is unchanged since install. It does not prove who
  wrote it. Publisher signing (Ed25519 over the digest string) exists only as a
  spike in `skills/signing.py`, described in
  [`docs/SPIKE_PLUGIN_SIGNING.md`](SPIKE_PLUGIN_SIGNING.md), and is not wired into
  the loader. A catalog that a resolver can trust needs that step first, so it is
  a Phase 3 prerequisite.
- Anything outside the catalog needs an explicit extra confirmation on the card.
- No setup step may be triggered by screen content or web content alone. A setup
  plan can only start from a request the user made.
- Skill network access stays limited to resolved public addresses
  (`skills/policy.py`), and spend caps keep failing closed (`kernel/usage.py`,
  [`docs/DEPLOY.md`](DEPLOY.md) section 13.1).

## 5. Run modes

The architecture has one rule: the body is always local and the brain is local or
in Boat.

- **Body:** avatar rendering, screen capture, microphone and speaker, OS
  permissions, and the approval UI. It always runs on the user's machine, because
  a cloud VM cannot see the user's screen.
- **Brain:** the kernel (`kernel/`), skills (`skills/`), the approval gate
  (`openshell/`), memory and store (`chat/`), and the scheduler. It runs either
  as a local sidecar on loopback or inside a Boat sandbox that the user owns. The
  desktop app connects to whichever the user chose.

What travels from the body to a Boat brain is only what the user enabled. Screen
frames are downscaled and sent only on demand, and they are not stored by
default.

| | Local | Boat |
|---|---|---|
| Where the brain runs | Sidecar process on the user's machine, loopback only | Persistent Ubuntu VM in a Boat sandbox the user owns |
| Cost | No hosting cost. Model calls cost whatever the chosen model costs, or nothing for a local model. | Boat bills per second with a $20/month minimum plan. Small (2 vCPU/4 GB) is $0.018/h and the default (4 vCPU/8 GB) is $0.036/h. There is a 7-day trial with a card and 25 compute hours. Plus model calls. |
| Privacy | Data stays on the machine unless a remote provider is chosen. | Chat, and any screen frames the user enabled, reach the sandbox and the model provider. |
| Works offline | Yes, with a local model and local STT/TTS | No. The sandbox and the provider need the network. |
| Computer-use target | The user's real desktop, only after approval | Kelp's own cloud computer: a 60fps virtual desktop with a browser and Docker, so long or risky tasks do not touch the user's desktop |
| Limits | Depends on the user's hardware | Machines only in Germany, Finland and France. No GPU, so no large local model inside the sandbox. Auto-stop defaults to 1 h (configurable up to 30 days, or off on paid plans). A stopped sandbox keeps its disk for at least 14 days, but memory and processes are not saved across a stop. The IP changes on resume. |

Boat figures were read from boat.dev on 2026-11-23 and belong to a third party, so
re-check them before relying on them. An earlier draft pilot (Nov 9, now
archived) planned to measure how long Boat desktop tasks take and what they cost.
It was never run, so measured numbers are still missing. Because the IP changes on
resume, the desktop app must not pin a Boat brain by IP address. How the app
reaches the sandbox (an exposed HTTP port or a tunnel) is not decided here and is
listed in section 17.

Local mode is still one owner. ADR 0002 (single-owner identity) and ADR 0003 (the
frozen service contract) stay in force in both modes.

## 6. The local install problem

Boat gives easy installs in the cloud. There is no equivalent easy install for a
local user yet, and the local installer is an open Phase 0 problem. The options:

| Option | How it works | For | Against |
|---|---|---|---|
| (a) Tauri app bundles the Python brain as a sidecar, using a relocatable Python (python-build-standalone) managed by `uv` | The desktop installer carries the Tauri app plus a pinned Python 3.12 runtime and the brain's dependencies. The app starts and stops the sidecar on loopback. | One download, no system Python, the runtime is pinned, updates can ship together | Larger installer, signing and notarisation work per OS, process lifecycle (start, restart, port, lock) must be built |
| (b) Single-binary freeze (PyInstaller or Nuitka) | The brain is frozen into one executable that the app launches. | One file, simple to describe | Frozen builds are fragile with dynamic imports, and skills are loaded from directories and packages at runtime (`skills/loader.py`), which a freeze fights. Harder to debug and to update only the runtime. |
| (c) One-line script (`curl \| sh`), like Boat's | A shell or PowerShell script installs the app and a runtime. | Familiar to developers, fast to try | The user pipes a remote script into a shell, which is a poor fit for a project whose principle is explicit approval. It still needs a runtime and the app underneath it. |
| (d) Container (Docker) for local | The brain runs in a container and the app talks to it. | Same image as a server deploy, clean isolation | Requires Docker on the user's machine, which many students and non-developers do not have. It also adds a daemon to the first-run setup. |

**Recommendation: option (a).** Deliver it through signed per-OS installers (dmg
for macOS, msi for Windows, AppImage and deb for Linux), plus a one-line script
that fetches the same signed installer for people who prefer a terminal. Updates
use Tauri's updater. This keeps one artifact per OS and one runtime that Kelp
controls, and the one-line script becomes a convenience instead of a second
install path.

Costs to plan for: Apple code signing and notarisation, a Windows code-signing
certificate, and the updater signing keys. The desktop app today ships unsigned
dev builds only, with the updater off (`desktop/README.md`). Phase 0 only decides.
Phase 1 builds it.

## 7. Reacting versus acting

Kelp does two different kinds of work, and they must not share a cost model.

**Reaction loop (cheap, local, frequent).** It watches cheap signals: the active
app, idle time, other OS signals, and an occasional downscaled screenshot. It is
rate-limited and uses a small, cheap model, ideally a local one. Its output is a
pet state, such as the existing `curious` or `surprised`, and at most one short
line. The reaction loop has no tool access. It can propose an offer, but an offer
only turns into an action when the user accepts and a normal turn starts.

**Actions (rare, expensive, gated).** Anything that does work runs through the
kernel, the skills, and the approval gate. This is the path that exists today.

The cost reason is direct. Spend caps fail closed (`kernel/usage.py`), so a
frontier model called on every frame would exhaust the cap and then stop the
whole assistant, including the actions the user actually asked for. Reaction
calls count against the same spend caps. Whether they need a sub-cap of their own
is listed in section 17.

## 8. Screen and voice privacy rules

- Screen watching is opt-in per app.
- A visible indicator shows whenever Kelp is watching or listening.
- Frames are not written to disk by default.
- A hard block list is always off-limits, including password managers and banking
  apps. The user cannot lift it by mistake in the setup flow.
- Screen text can never authorise an action. It is data, handled as untrusted
  input like any tool result. The existing credential flow already follows the
  same rule for private entry: the browser wrapper refuses to screenshot while
  private entry is active ([`docs/CREDENTIAL_HANDOFF.md`](CREDENTIAL_HANDOFF.md)).
- Voice defaults to push-to-talk. Always-listening is an explicit opt-in.

## 9. Persona

The setup flow writes a versioned persona file. It covers:

- **Temperament.** Several examples to choose from or blend, such as warm,
  dry and deadpan, cheerful, or tsundere. These are starting points and not a
  fixed list.
- **Talkativeness.** How often Kelp speaks unprompted. The default is rarely.
- **Topics** it may comment on, and topics it must leave alone.
- **Voice.**
- **Avatar.**

Mood changes slowly across a session and does not swing on each event. Persona
files are plain files, so users can share them and edit them.

## 10. Avatar and voice

**Avatar.** Start from the existing ten-state kit in `assets/kelp-pet/` (idle,
working, waiting, done, offline, curious, happy, excited, thinking, surprised).
The kit ships 384×384 animated WebP, 64-frame atlases, and a `kelp-pet.js` web
component. Then move to VRM through three-vrm, which is open, or to Live2D. The
Live2D Cubism SDK is not open source, so check its licence before adopting it.

**Reuse before building.** AIRI (MIT, Live2D and VRM, realtime voice) and
Open-LLM-VTuber (Live2D, voice, vision, offline) already solve parts of this.
Reuse their code where the licences allow, and do not rebuild what they have.

**Voice.** The latency target is about 1 s from the end of the user's speech to
the start of Kelp's reply. The default is local speech-to-text and text-to-speech:
Whisper-family models for STT and Piper for TTS.

**What exists today.** `desktop/` is a Tauri app. The Rust bridge
`desktop/src-tauri/src/bridge.rs` owns the cookies, CSRF and the SSE stream. The
debug build defaults to `http://127.0.0.1:8000` and the release build uses the
placeholder `https://kelp.example.com`. There are three windows: the main app, the
command panel (an overlay opened with `CommandOrControl+Shift+Space`), and a
96×96 pet overlay (`desktop/src/pet.js`, `desktop/src/orb.js`).
`desktop/pet_state.py` maps chat events to pet states (working, thinking,
waiting, done, idle). The panel's screen button is inert, and no capture code
exists. The seed of the avatar is on the branch `origin/kel-31-pet-motion-kit`
(Storm, kept). Its 3 commits are 3bd69c5 (animate the pet from the kit's ten
states), 4546460 (lean toward the cursor and fidget) and 84fa565 (overlay pill
alignment). The branch is based on `desktop-demo`, which carries demo fixtures,
so those commits should land on main by cherry-pick or a fresh PR, not by merging
the branch. Issue #69 (Storm) covers desktop demo mode.

## 11. What pauses

These stay on main and get no new work until the companion direction has
something to try:

- the payments wallet and Razorpay checkout,
- multi-tenant entitlements,
- the phone PWA (to be discussed with Ketock).

## 12. Platform permissions risk

Screen capture is different on each OS, and each needs a permission the user must
grant:

- **Linux (Wayland):** capture goes through xdg-desktop-portal, with consent per
  session.
- **macOS:** Screen Recording and Accessibility permissions.
- **Windows:** Windows Graphics Capture.

Prototype all three in Phase 0 and Phase 1, before the design assumes that
capture "just works". The capability resolver can guide the user to a permission
prompt, but it cannot grant an OS permission for them.

## 13. Companion risks

These need decisions before launch:

- **Proactive chatter.** An assistant that speaks too often becomes an annoyance
  and gets uninstalled. The default is that Kelp rarely speaks.
- **Emotional dependence.** A companion that is present all day can become
  something a user leans on too hard. Decide what Kelp does and does not encourage.
- **Minors and age stance.** Decide whether there is an age requirement and what
  it is, and how that is stated.
- **How Kelp describes itself.** Decide how Kelp answers questions about what it
  is. A proposed baseline is that it says plainly that it is an AI, and that no
  persona claims to be human or to have feelings it does not have.

## 14. Build in public

- A short clip every week.
- A public roadmap.
- A Discord, opened when there is something to try.
- Good-first-issues on persona packs and avatar packs.

There is no public hosted instance until production (decision D, 2026-11-23). The
landing site is a holding page with a pre-register form. The real landing page is
the last task before production.

**Metrics to track:**

- pre-register sign-ups,
- daily minutes with the companion open,
- reaction feedback, nice versus annoying,
- approve rate on proposed actions,
- time to first conversation after install,
- setup prompts per user per week. The target is near zero after the first run.

## 15. Money (later, not now)

The core stays open source. Possible paid options for later:

- optional hosted sync,
- managed model credits,
- Boat-mode convenience,
- premium voices.

None of these are built or priced in Phase 0.

## 16. Phases

| Phase | Scope |
|---|---|
| 0 (now) | This brief and ADR 0004. Land Storm's 3 pet commits on main. Spikes for screen capture and voice. The installer decision. |
| 1 | Persona and voice chat with the animated avatar. Local sidecar installer. No screen access. |
| 2 | Opt-in screen awareness through the reaction loop. |
| 3 | Actions through approval-gated skills. The capability resolver. Computer use, either on the local desktop or in a Boat sandbox. |
| 4 | Production hardening, the real landing page, and invitations to the pre-register list. |

## 17. Phase 0 exit criteria and open questions

**Exit criteria (all checkable):**

- [ ] The owner has accepted or changed this brief and ADR 0004.
- [ ] The installer option is chosen, and the choice is written into ADR 0004.
- [ ] Storm's 3 pet commits are on main by cherry-pick or a fresh PR, without
      the `desktop-demo` fixtures.
- [ ] A capture spike has run on Wayland, macOS and Windows, with notes on what
      each permission prompt looks like.
- [ ] A voice spike has measured the end-of-speech to start-of-reply latency of
      the local STT and TTS choice against the target of about 1 s.
- [ ] The Live2D licence has been checked, and the AIRI and Open-LLM-VTuber
      licences have been checked for the parts to be reused.
- [ ] [`docs/THREAT_MODEL.md`](THREAT_MODEL.md) has a plan for screen, voice and
      self-setup sections, to be written before any capture code lands.
- [ ] The first pre-register `wish` answers have been read and the first
      audience in section 2 confirmed or changed.
- [ ] Measured Boat desktop-task durations and cost exist, or the owner has
      accepted going without them.

**Open questions for the owner:**

1. Is developers and students the right first audience, or should the pre-register
   answers decide first?
2. Does the owner accept option (a) for the local installer, given the signing
   and notarisation cost?
3. How does the desktop app reach a brain inside a Boat sandbox: an exposed HTTP
   port, or a tunnel? The IP changes on resume, and the owner bearer must still
   protect the endpoint.
4. In local mode, where do the owner bearer and the credential vault key live?
   Today the vault key comes from the environment
   ([`docs/CREDENTIAL_HANDOFF.md`](CREDENTIAL_HANDOFF.md)) and the desktop app
   keeps no bearer at rest (`desktop/README.md`). A local installer has to
   generate and store both somewhere, most likely in the OS keychain.
5. Do reaction-loop calls get their own spend sub-cap?
6. Should the resolver's catalog wait for publisher signing
   (`skills/signing.py`), or ship first with digest pinning only?
7. What is the age and self-description stance (section 13)?
8. Which Phase 0 spike goes first: capture or voice?

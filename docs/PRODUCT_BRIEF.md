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

This is the core requirement: local setup with no repeated steps.
The failure to avoid is one seen in other agent tools: Hermes Agent (Nous
Research) ships a one-line installer and a `hermes doctor` check, yet users report
per-feature friction in its setup wizard, for example GitHub issues #41046 (forced
portal onboarding) and #13301 (tools shown as "not configured" after being
enabled). Kelp should not ask the user to repeat setup for each new feature.

### 4.1 First run

- One installer and one choice: Local or Boat. Boat adds one follow-up, the Boat
  sign-in, which also hands the owner bearer to the sandbox.
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
   where, at what pinned version or digest, and what access it grants. The model
   may only name catalog entry ids. The resolver builds the plan, and the card's
   text, from catalog metadata. No model-written text appears on the card, so
   injected page or screen text cannot shape what the user approves.
3. **Show one approval card when the plan grants new access.** The whole plan is
   a single card in the existing approval UI. It is not one prompt per step. A
   plan whose catalog entries grant no new access installs without a card, and
   the plan is still logged. The card appears when the plan adds a new account or
   login, a new folder, a new network host, a new OS permission, or spending
   (see [THREAT_MODEL §9, T1](THREAT_MODEL.md#9-self-setup-the-capability-resolver)).
   Today's approvals in
   `openshell/approvals.py` are single-use, bound to one `command` and its
   `argument_hash`, with a 600 s TTL. A plan of several steps, such as a large
   model download, fits neither rule. The resolver therefore needs a new approval
   kind bound to the hash of the whole plan: each step is checked against that
   hash before it runs, and the TTL covers the decision, not the run time.
4. **Perform every step.** After approval Kelp does the installing and
   connecting itself. The user does not edit config.
5. **Verify.** Kelp runs a doctor-style self-check and reports success or the
   exact step that failed.
   A step that cannot be automated ends the check as `manual_fix_required` and
   tells the user the exact edit or command.

### 4.3 Remember once

Approved capabilities, credentials and OS permissions persist, so the same setup
is never asked twice. Credentials go through the existing credential handoff and
encrypted vault ([`docs/CREDENTIAL_HANDOFF.md`](CREDENTIAL_HANDOFF.md)), never
through chat messages or model prompts. A capability is asked about again only if
its scope changes.

### 4.4 Safety limits

- Kelp installs only from an allowlisted catalog. The catalog covers skills,
  model weights and runtime dependencies. Each entry is pinned by version and
  digest. Logins and OS permissions are catalog entries too, but they describe a
  flow to run, not a file to verify. The pinning mechanism exists today for skill
  packages only, in `skills/loader.py`. A package
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
  the loader. The recommendation is to wire signing in before the full resolver
  ships in Phase 3; open question 6 asks the owner to confirm. The minimal Phase 1
  resolver installs only voice models from a short list the project pins by
  digest in its own release, so it does not depend on publisher signing.
- Anything outside the catalog needs an explicit extra confirmation on the card.
- No setup step may be triggered by screen content or web content alone. A setup
  plan can only start inside a turn the user started, and because the card is
  built from catalog metadata, injected text can at most pick a catalog entry the
  user then sees by its real name.
- Skill network access stays limited to resolved public addresses
  (`skills/policy.py`), and spend caps keep failing closed (`kernel/usage.py`,
  [`docs/DEPLOY.md`](DEPLOY.md) section 13.1). The egress guard is cooperative at
  the Python layer, and [`docs/THREAT_MODEL.md`](THREAT_MODEL.md) section 5 relies
  on the deployment boundary (no exec, container egress rules) to back it. A local
  sidecar running as the user has no such boundary, so Local mode needs OS-level
  confinement of the sidecar (a restricted user or sandbox, no exec, egress rules)
  before skills with network access run there.
- The rules for what installs silently, which sources are allowed, how logins are
  stored and removed, and what always needs a card are written as planned rules in
  [THREAT_MODEL §9](THREAT_MODEL.md#9-self-setup-the-capability-resolver). No code
  enforces them yet.

### 4.5 Finishing the job

These are requirements for how Kelp works on a request. None is built yet.

1. Check the live source before acting or reporting: the actual site, inbox or
   repository, not memory.
2. Follow a fallback ladder before saying no. First an official API or connector.
   Then an export feed the user already has (for example a Moodle per-user
   calendar export URL). Then a logged-in browser session, in Kelp's browser or in
   Boat. Ask one specific question only when every route is blocked.
3. Give every request a tracked status: Done, Ongoing, Waiting on you, Paused,
   Dropped, Missed, Declined. Kelp can produce an "everything you asked me" report
   from it on request.
4. Support standing jobs ("watches") with a schedule and a stop condition, using
   the existing scheduler (`SchedulerRuntime` in `chat/`). Examples: a morning
   brief of classes and deadlines, an hourly check on a server.
5. Save standing rules the user states in conversation ("every new login goes into
   the vault", "accept routine terms but flag unusual ones") as user policy, and
   apply them later without asking again. A standing rule can widen behaviour only
   inside the card rules in
   [THREAT_MODEL §9, T4](THREAT_MODEL.md#9-self-setup-the-capability-resolver).
6. Report honest misses. When Kelp was wrong or late, the report says so plainly.
7. When blocked on a one-tap user action (a phone 2FA tap, a login save), say
   exactly what is needed and resume on its own afterwards.
8. When Kelp declines, name the reason and offer the nearest allowed alternative.
9. Deliver approvals to the user away from the desk. A late yes loses
   time-sensitive work. This needs a remote approval channel (a push to the phone
   or a messaging bridge). It is open question 9 in section 17.

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

The detailed rules are written as planned rules in
[THREAT_MODEL §7 (screen)](THREAT_MODEL.md#7-screen) and
[§8 (voice)](THREAT_MODEL.md#8-voice). No code enforces them yet. In short:

- Screen watching is opt-in per app, chosen once on a single screen. First run
  shows app categories with suggested defaults already ticked (editor, terminal,
  most of the browser), and the user changes them in that one place.
- A visible indicator shows whenever Kelp is watching or listening. Kelp draws
  it itself: on wlroots compositors the OS shows nothing during capture.
- Frames are never written to disk. Kelp may keep short text observations for at
  most 24 hours. Anything longer-term enters memory only after the user okays it.
- Some apps are always blocked and the user cannot lift that: password managers,
  private browser windows, Kelp's own credential screen, and authenticator apps.
  Banking and payment sites are blocked by default. The user can unlock one site
  at a time, only from Settings, never from a setup card or chat.
- Screen text can never authorise an action. It is data, handled as untrusted
  input like any tool result. The existing credential flow already follows the
  same rule for private entry: the browser wrapper refuses to screenshot while
  private entry is active ([`docs/CREDENTIAL_HANDOFF.md`](CREDENTIAL_HANDOFF.md)).
- Voice defaults to push-to-talk. A wake word ("Hey Kelp") is an optional opt-in
  and is detected on the device. Audio is deleted once it is transcribed, and
  only the text is kept.

## 9. Persona

The setup flow writes a versioned persona file. It covers:

- **Temperament.** Several examples to choose from or blend, such as warm,
  dry and deadpan, cheerful, or tsundere. These are starting points and not a
  fixed list.
- **Talkativeness.** How often Kelp speaks unprompted. The default is rarely.
- **Topics** it may comment on, and topics it must leave alone.
- **Voice:** which TTS voice, and its speed and pitch.
- **Avatar:** which avatar pack, and which states and expressions it maps.

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

**Voice.** The default is local speech-to-text and text-to-speech:
faster-whisper `tiny.en` for STT and Piper for TTS. The Phase 0 voice spike
([`docs/spikes/2026-11-voice-latency.md`](spikes/2026-11-voice-latency.md)) showed
that the earlier target of about 1 s is not reachable with this pipeline on a
laptop CPU (Intel i7-1255U, no GPU):

| Chain, end of speech to first sentence of audio | Median |
|---|---|
| tiny.en + cloud Haiku 5.5 (reasoning off) + Piper | 3.1–3.3 s |
| tiny.en + local phi4-mini 3.8B (Ollama, CPU) + Piper | about 6.1 s, an estimate: 0.98 s STT + 4.68 s LLM first sentence + 0.46 s Piper, stages measured separately with STT uncontended, not a chained run; about 6.7 s with the chained run's 1.56 s STT |

Both rows exclude endpointing (typically 0.3–0.8 s of silence before the
utterance counts as finished), audio device latency and network variance. The
chained run's STT step was also slowed by other load on the machine (1.43–1.56 s
against 0.87–0.98 s when run alone). Both rows stop when synthesized audio is ready
to play, so speaker latency is excluded too.

The targets are therefore:

- **Acknowledge in ≤ 200 ms.** The avatar switches to its listening or thinking
  state as soon as speech ends. The pet already has those states.
- **First audio ≤ 2.5 s p50** in cloud-brain mode on a laptop CPU, measured from
  the end of endpointing to the first audio sample played. Getting
  there needs streaming STT (transcribing while the user speaks), which the spike
  did not measure.
- **Local-only voice** needs a GPU or NPU to be conversational. On a CPU-only
  machine, local mode uses a configured cloud model for voice replies and says so,
  or offers text only.
- Kokoro TTS sounds better but took 3.6 s for the first sentence on this CPU, so
  Piper stays the default.
- Speech-to-speech realtime APIs are the other route to sub-second replies. They
  send raw audio to a provider, and they were not tested. They are an explicit
  opt-in ("fast voice") with the user's own provider key, off by default, decided
  in [THREAT_MODEL §8, V3](THREAT_MODEL.md#8-voice).

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
the branch. Issue #69 (Storm) covers desktop demo mode. The main app also has a
Computer pane (`desktop/src/ComputerPane.tsx`): a view-only noVNC viewer onto a
VM's graphical session. It is the natural starting point for watching Kelp work in
a Boat sandbox's virtual desktop.

## 11. What pauses

These stay on main and get no new work until the companion direction has
something to try:

- the payments wallet and Razorpay checkout,
- multi-tenant entitlements,
- the phone PWA (to be discussed with Ketock). This conflicts with requirement 9
  in section 4.5: away-from-desk approvals need a remote approval channel, and the
  phone PWA is one candidate for it. Pausing the PWA leaves that need open
  (section 17, question 9).

## 12. Platform permissions risk

Screen capture is different on each OS, and each needs a permission the user must
grant:

- **Linux (Wayland):** it depends on the compositor. The Phase 0 capture spike
  ([`docs/spikes/2026-11-screen-capture.md`](spikes/2026-11-screen-capture.md))
  ran on MangoWC, a wlroots compositor. There, `grim` (wlr-screencopy) captured
  the full 1920×1080 screen in 38 ms with **no prompt at all**, the portal's
  non-interactive Screenshot also ran without a prompt, and X11 capture through
  Xwayland returned black frames. GNOME and KDE route capture through the
  xdg-desktop-portal ScreenCast dialog; that was not tested. On wlroots the
  portal's non-interactive Screenshot also left the image in `/tmp` as a
  world-readable (0644) file, so Kelp captures frames in memory and does not use
  that portal call.
- **macOS:** Screen Recording and Accessibility permissions.
- **Windows:** Windows Graphics Capture.

Prototype all three in Phase 0 and Phase 1, before the design assumes that
capture works without extra steps. Because some platforms ask for nothing, the
rules in section 8 are enforced by Kelp's own capture code, never delegated to
the OS prompt. The capability resolver can guide the user to a permission
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
- the setup-friction metrics in section 14.1.

### 14.1 Setup-friction metrics

Section 4 promises that setup is never repeated. These numbers show whether that
holds, and they are tracked from the first build that has an installer. They are
counted on the user's machine, exposed on the existing `GET /api/metrics`
endpoint, and leave the machine only if the user opts in to sharing them.

| Metric | Definition | Target |
|---|---|---|
| Time to first conversation | Installer start to the first rendered Kelp reply, in seconds | p50 ≤ 5 min, no config file edited |
| First-run questions | Questions the user answers before that first reply | 1 (Local or Boat), plus one provider sign-in only when no local model fits. Boat adds one question, the Boat sign-in that includes the owner bearer handoff |
| Setup prompts per active week | Kelp-initiated prompts shown per user per week after the first day: resolver plan cards, credential handoff requests, and resolver steps that ask the OS for a permission | ≤ 1 median, trending to 0 |
| Repeat asks | A Kelp-initiated setup prompt for a capability that was already approved and whose scope has not changed, excluding a re-request after an OS-initiated lapse (counted under OS re-consent) | **0.** Any non-zero value is a bug |
| OS re-consent | Reminders the OS raises on its own, such as the macOS 15+ "continue to allow" screen-recording prompt. Counted when Kelp detects the permission lapsed | Watched, not a bug |
| Resolver success rate | Approved setup plans whose self-check (section 4.2, step 5) passes on the first attempt | ≥ 95% |
| Setup abandonment | Setup cards dismissed or left to expire, as a share of cards shown | Watched, no target yet |
| Manual fixes | Self-check results that end in `manual_fix_required` (section 4.2, step 5). Bug reports with the same cause are tracked by hand | 0 |

Resolver plan cards are counted from the approval audit rows
(`approval_requested`, `approval_resolved`) once the resolver gets its own
approval kind (section 4.2), so setup cards can be counted separately from action
approvals. Credential handoff requests and OS permission steps get their own
audit rows when they are built. OS dialogs themselves are outside Kelp, so only
the step that triggers them is counted. Repeat asks are
detected by comparing a new plan's capability ids and scopes with the remembered
grants from section 4.3.

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
| 1 | Persona and voice chat with the animated avatar. Local sidecar installer, first run (section 4.1) and remember-once (section 4.3). A minimal resolver for the voice models only. No screen access. |
| 2 | Opt-in screen awareness through the reaction loop. The resolver guides the user through the OS capture permission. |
| 3 | Actions through approval-gated skills. The full capability resolver with the signed catalog. Computer use, either on the local desktop or in a Boat sandbox. |
| 4 | Production hardening, the real landing page, and invitations to the pre-register list. |

## 17. Phase 0 exit criteria and open questions

**Exit criteria (all checkable):**

- [ ] The owner has accepted or changed this brief and ADR 0004.
- [ ] The installer option is chosen, and the choice is written into ADR 0004.
- [ ] Storm's 3 pet commits are on main by cherry-pick or a fresh PR, without
      the `desktop-demo` fixtures.
- [ ] A capture spike has run on Wayland, macOS and Windows, with notes on what
      each permission prompt looks like. Wayland (wlroots) is done; GNOME, KDE,
      macOS and Windows are not.
- [x] A voice spike has measured the end-of-speech to start-of-reply latency of
      the local STT and TTS choice. Result: 3.1–3.3 s with a cloud model on one
      Linux laptop; the target is revised in section 10. A run on Apple Silicon
      and a Windows laptop is still useful before the number is final.
- [ ] The Live2D licence has been checked, and the AIRI and Open-LLM-VTuber
      licences have been checked for the parts to be reused.
- [x] [`docs/THREAT_MODEL.md`](THREAT_MODEL.md) has a plan for screen, voice and
      self-setup sections, to be written before any capture code lands. (sections
      written 2026-11-24, status planned)
- [ ] The first pre-register `wish` answers have been read and the first
      audience in section 2 confirmed or changed. As of 2026-11-23 there are no
      submissions.
- [ ] Measured Boat desktop-task durations and cost exist, or the owner has
      accepted going without them.

**Open questions for the owner:**

1. Is developers and students the right first audience, or should the pre-register
   answers decide first?
2. Does the owner accept option (a) for the local installer, given the signing
   and notarisation cost?
3. How does the desktop app reach a brain inside a Boat sandbox: an exposed HTTP
   port, or a tunnel? ADR 0004 sets the minimum (TLS only, owner bearer always
   required, no pinning by IP, secrets kept inside the sandbox). The transport
   choice blocks Boat mode.
4. In local mode, where do the owner secrets live? A local installer has to
   generate the owner bearer (`KELP_AUTH_TOKEN`) and the credential settings
   `KELP_VAULT_KEY`, `KELP_BROWSER_WORKER_TOKEN` and `KELP_CREDENTIAL_SIGNING_KEY`
   ([`docs/CREDENTIAL_HANDOFF.md`](CREDENTIAL_HANDOFF.md)), and store them. It also
   sets the two non-secret settings `KELP_CREDENTIALS_ENABLED` and
   `KELP_BROWSER_ALLOWED_ORIGINS`. The
   OS keychain is the likely home, but note that `docs/DESKTOP_SCOPE.md` says
   "token in OS keychain only" while `desktop/README.md` records that the old
   `keychain.js` and its Stronghold wiring were removed and nothing is stored at
   rest. It says any future at-rest storage belongs on the Rust side, inside
   `bridge_sign_in`, never in the webview. Local mode has to add that.
5. Do reaction-loop calls get their own spend sub-cap?
6. Should the resolver's catalog wait for publisher signing
   (`skills/signing.py`), or ship first with digest pinning only?
7. What is the age and self-description stance (section 13)?
8. Should voice replies in local mode fall back to a cloud model on machines
   without a GPU (section 10), or stay local and slower?
9. How does an approval reach the user away from the desk? A late yes loses
   time-sensitive work, so Kelp needs a remote approval channel: a push to the
   phone, or a messaging bridge (section 4.5, requirement 9). This conflicts with
   pausing the phone PWA (section 11).
10. Graded and timed work: Kelp never sits timed tests or quizzes. Should Kelp
    ever submit graded coursework the user reviewed, behind a card that
    [THREAT_MODEL §9, T4](THREAT_MODEL.md#9-self-setup-the-capability-resolver)
    requires, or only prepare it and remind the user?

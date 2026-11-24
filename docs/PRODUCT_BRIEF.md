# Product brief: Kelp as a desktop companion (Phase 0)

- **Status:** Proposed, for owner review
- **Date:** 2026-11-23
- **Decision record:** [ADR 0004](adr/0004-desktop-companion.md)
- **Owner answers:** recorded 2026-11-24 in
  [section 17](#owner-answers-2026-11-24), with three later answers in
  [Later answers](#later-answers-2026-11-24)
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

Kelp moves from a chat and agent bot to an animated companion that lives on the
user's desktop. Its look is not decided yet (section 10). Kelp can see the
screen and work in other apps in the background when the user allows it, with
its own pointer and keyboard input. It never takes over the user's mouse or
keyboard, so the user keeps working while Kelp works (section 7, planned). Kelp
also talks by voice and reacts to what the user is doing. The on-screen avatar
(the desk pet) is optional: a user can turn it off and keep everything else
(section 10). The desktop app and the phone app come first, and the desk pet and
Kelp's identity come after them (section 16). An initial setup defines how
Kelp generally acts and behaves, which is its persona. Kelp should be able to
take on whatever the user throws at it, and it is open source and accessible to
anyone. The user chooses where the thinking happens: fully on their own machine,
or in a Boat cloud sandbox that they own. The existing server work (chat API,
approval gate, skills, spend caps) stays and becomes the brain behind the
avatar.

## 2. Who it is for first, and the first daily loop

**First audience (decided by the owner, 2026-11-24):** general users with no
technical background, not developers and students. This replaces the earlier
recommendation. It has these consequences:

- No step ever needs a terminal, a config file or an API key copy-paste for a
  plan user (section 15).
- Kelp is reachable from the desktop app (and the desk pet once it exists), the
  phone app and a messaging bridge, with one brain behind all three (section
  4.6). The phone app and the bridge are planned.
- Signed installers are required before non-developers are invited (section 6,
  Phase 4 in section 16).

The pre-register form (the holding page in `landing/`) has an optional "What
would you want Kelp to do for you?" field (`wish`). Its answers now refine which
errands to support first. They no longer decide who the audience is.

**First daily loop:**

1. The companion is present on the desktop while the user works.
2. It notices moments: the user looks stuck, something finished, the user has
   been idle, the user came back.
3. It reacts in its persona, usually with an animation and at most a short line.
4. It offers at most one useful action.
5. It acts only after the user approves, apart from the logged exceptions in
   section 3.

Errands and reminders that arrive through the phone app or the messaging bridge
are an equal entry point to this loop. They are not limited to desktop moments.

## 3. Principles

- **Zero repeated setup.** Setup happens once and Kelp does the work itself (section 4).
- **Quiet by default.** The default is that Kelp rarely speaks. One key mutes it.
- **Nothing acts without the user's yes, in one of two modes (planned).** Every
  action goes through the existing approval gate in `openshell/`. A global
  permission mode sets how often a card appears
  ([THREAT_MODEL §9, T7](THREAT_MODEL.md#9-self-setup-the-capability-resolver)).
  In **Ask before doing anything**, every action shows a card with an Approve
  button: each skill call with side effects, each command, the start of each
  screen-control task and each install. In **Approve for me**, routine actions
  run without a card and are logged and listed as "Approved for you". No model
  decides what is routine. The desktop app and the catalog classify each action
  deterministically, and an action they cannot classify gets a card. In both
  modes a card is always shown for the always-ask list: money, anything another
  person sees (including course submissions), anything that cannot be undone or
  is destructive, accepting terms
  ([THREAT_MODEL §9, T4](THREAT_MODEL.md#9-self-setup-the-capability-resolver)),
  new access (accounts and logins, folders, hosts, OS permissions, T1) and
  destructive commands (T8). Other logged exceptions: installs that grant no
  new access ([THREAT_MODEL §9,
  T1](THREAT_MODEL.md#9-self-setup-the-capability-resolver)), which are silent
  only in "Approve for me", and actions covered by a standing approval the user
  created (T4). The default mode on a new install is an open question (section
  17, question 16).
- **Screen content is untrusted input.** Text on screen or on a web page can
  describe a task but can never authorise one, and it can never start a setup step.
- **Local-first, cloud by choice.** The user picks Local or Boat. Neither is the
  fallback for the other.
- **Open source, with persona and avatar as shareable files.** Anyone can read,
  change and share what makes Kelp behave and look the way it does.

## 4. Zero repeated setup

This is the core requirement: local setup with no repeated steps.
The failure to avoid is one seen in other agent tools: a one-line installer and a
health check, followed by per-feature friction in the setup wizard, such as
forced onboarding screens and tools shown as "not configured" after being
enabled. Kelp should not ask the user to repeat setup for each new feature.

### 4.1 First run

- One installer and one choice: Local or Boat. Boat adds one follow-up, the Boat
  sign-in, which also hands the owner bearer to the sandbox.
- Defaults come from detected hardware. If the machine can run a local model,
  Kelp picks one. If it cannot, Kelp asks for one provider sign-in, once.
- The target is talking to Kelp within a few minutes of installing, with no
  config files edited by hand.
- The desk pet is on by default once it exists (Phase 3, section 16). The
  welcome screen and Settings each have one
  switch to turn it off (planned). It has a default and nothing waits on it, so
  it does not count as a first-run question in the section 14.1 metrics.

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
   plan whose catalog entries are all flagged as granting no new access installs
   without a card, within the per-turn size and count caps in [THREAT_MODEL §9,
   T1](THREAT_MODEL.md#9-self-setup-the-capability-resolver). It is shown
   afterwards in an Installed list the user can open, and the plan is still
   logged. In Local mode this waits for OS confinement of the sidecar, and every
   install shows a card until then. The card appears when the plan adds a new
   account or login, a new folder, a new network host, a new OS permission, or
   spending (see [THREAT_MODEL §9,
   T1](THREAT_MODEL.md#9-self-setup-the-capability-resolver)).
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
  the loader. The owner decided on 2026-11-24 to ship with pinning first. Kelp's
  own release-pinned entries ship first (the Phase 2 voice models). Publisher
  signing is wired in before the catalog accepts any entry from outside Kelp's
  own release, and before the full resolver ships in Phase 2. The minimal
  resolver, which Phase 2 builds first, installs only voice models from a short
  list the project pins by digest in its own release, so it does not depend on
  publisher signing.
- Anything outside the catalog is not installed by Kelp. Kelp may propose a
  catalog entry for the project to review
  ([THREAT_MODEL §9, T2](THREAT_MODEL.md#9-self-setup-the-capability-resolver)).
- No setup step may be triggered by screen content or web content alone. A setup
  plan can only start inside a turn the user started, and because the card is
  built from catalog metadata, injected text can at most pick a catalog entry. If
  that entry grants new access, the user sees it on a card by its real name. If it
  grants none, it appears in the Installed list instead of on a card, and silent
  install is off in any turn whose context includes screen or web text until the
  user confirms.
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
   calendar export URL). Then a logged-in browser session, in Kelp's browser or
   in Boat. Ask one specific question only when every route is blocked. An
   export feed URL that embeds a token is stored like a login: in the vault,
   scoped to one site, and revocable.
3. Give every request a tracked status: Done, Ongoing, Waiting on you, Paused,
   Dropped, Missed, Declined. Kelp can produce an "everything you asked me" report
   from it on request.
4. Support standing jobs ("watches") with a schedule and a stop condition, using
   the existing scheduler (`SchedulerRuntime` in `chat/`). Examples: a morning
   brief of classes and deadlines, an hourly check on a server.
5. Save standing rules the user states in conversation, and apply them later
   without asking again. There are two kinds. A preference only shapes or
   restricts how Kelp works (where logins are stored, when the report arrives,
   tone). "Every new login goes into the vault" is a preference, and a preference
   never waives a card. A standing approval widens what runs without a card. It is
   scoped to one action type and one target, it expires, and it is listed and
   revocable in Settings. A standing approval never covers spending money,
   deletion or other irreversible acts, anything another person will see, or
   accepting terms of service, or starting screen control (THREAT_MODEL S8), and
   Kelp always shows terms acceptance as a card.
   It cannot waive a card for new access (accounts and logins, folders, hosts, OS
   permissions). Both kinds are created only from a confirmation that the desktop
   app renders from structured fields (action type, target, expiry), never from
   model-written text, never from a turn in which screen, web or tool-result text
   is the source of the request, and never by a scheduled watch. The rules are in
   [THREAT_MODEL §9, T4](THREAT_MODEL.md#9-self-setup-the-capability-resolver).
6. Report honest misses. When Kelp was wrong or late, the report says so plainly.
7. When blocked on a one-tap user action (a phone 2FA tap, a login save), say
   exactly what is needed and resume on its own afterwards. Resuming continues an
   already approved plan. It never starts a new setup step.
8. When Kelp declines, name the reason and offer the nearest allowed alternative.
9. Deliver approvals to the user away from the desk. A late yes loses
   time-sensitive work. The primary channel is a push to Ketock's native mobile
   app, and the secondary channel is a Telegram bridge. Both are planned and are
   described in section 4.6. The owner chose them on 2026-11-24 (open question 9
   in section 17).

### 4.6 Remote approvals (planned)

Nothing here is built. The rules for the relay and the bridge are in
[THREAT_MODEL §6, L6 and L7](THREAT_MODEL.md#6-local-brain-boundary-local-mode).

**Primary channel: push to Ketock's native app.** Ketock's native mobile app (see
[`docs/KETOCK_NATIVE_APP_HANDOFF.md`](KETOCK_NATIVE_APP_HANDOFF.md)) is the main
way to approve a card away from the desk. The work is coordinated with Ketock.

- **A push relay is required.** Apple and Google push services accept pushes only
  when they are signed with the app publisher's credentials. A self-hosted brain
  does not hold those credentials, so it cannot push to the store app directly.
- **The brain keeps an outbound connection to the relay.** This works behind home
  NAT and needs no open port, in Local mode or in Boat mode.
- **The push carries no content.** It says only that a card is waiting, plus an
  opaque id.
- **The app fetches and answers through the relay.** It fetches the card details
  and sends the decision back through the relay. Both directions are end-to-end
  encrypted between the paired phone and the brain, and the decision is signed
  with a device key created at pairing. The relay sees only ciphertext and
  timing.
- **Pairing is one QR scan** from the desktop app.
- **The relay is open source,** so self-hosters can run their own.

The native app's handoff says no second server and no new credential type
(`docs/KETOCK_NATIVE_APP_HANDOFF.md`, sections 2 and 3, and section 4 definition
of done item 1, which says one configured server URL). A relay and a pairing
device key do not fit those words as written, so they have to be reconciled with
Ketock before any design is final. This is a follow-up in
[ADR 0004](adr/0004-desktop-companion.md#7-follow-ups). Section 16 schedules the
phone app with push approvals in Phase 1, built with Ketock, and the Telegram
bridge after it.

**Secondary channel: Telegram bridge.** The bridge is a bot that the brain
reaches by long polling, which is outbound only. Telegram bot messages are not
end-to-end encrypted, so a Telegram card shows only the action type and the
target. Full details stay in the desktop or phone app.

- **What Telegram may approve.** A card is approvable from Telegram only when the
  fields Telegram shows fully determine the action, and the card is not in a
  [T4 category](THREAT_MODEL.md#9-self-setup-the-capability-resolver) (money,
  anything another person sees, irreversible acts, terms of service) and is not
  new access (T1). It is also never a screen-control card
  ([THREAT_MODEL S8](THREAT_MODEL.md#7-screen)). Every other card shows "open in
  the app" in Telegram. Whether to widen this is a follow-up.
- **Who may send.** The bridge accepts messages only from the paired Telegram
  user id and refuses group chats. Pairing uses a one-time code that the desktop
  app shows. The bot token is a persistent secret, stored in the OS credential
  store.
- **Remote channels are limited.** A message from the bridge is a user turn. But
  Telegram and the phone can never create a standing approval or approve a T1
  new-access card, and they can never switch the permission mode to "Approve for
  me" (section 3). Telegram can never start or approve screen control. The
  paired phone app may approve starting control, on the user's own desktop or on
  a Boat sandbox desktop, with a signed decision (L6). This is a change made on
  2026-11-24: starting control no longer has to be approved at the desk,
  because Kelp never takes over the user's input
  ([THREAT_MODEL S8](THREAT_MODEL.md#7-screen)). Planned, not built.

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
frames are downscaled and sent only on demand, and they are not written to disk.

| | Local | Boat |
|---|---|---|
| Where the brain runs | Sidecar process on the user's machine, loopback only | Persistent Ubuntu VM in a Boat sandbox the user owns |
| Cost | No hosting cost. Model calls cost whatever the chosen model costs, or nothing for a local model. | Boat bills per second with a $20/month minimum plan. Small (2 vCPU/4 GB) is $0.018/h and the default (4 vCPU/8 GB) is $0.036/h. There is a 7-day trial with a card and 25 compute hours. Plus model calls. |
| Privacy | Data stays on the machine unless a remote provider or the plan gateway is chosen. | Chat, and any screen frames the user enabled, reach the sandbox and the model provider. |
| Works offline | Yes, with a local model and local STT/TTS | No. The sandbox and the provider need the network. |
| Computer-use target | Apps on the user's own desktop, worked in the background, only after approval. Kelp never moves the user's pointer or takes their keyboard focus (S8). | Kelp's own cloud computer by default: a 60fps virtual desktop with a browser and Docker, so long or risky tasks do not touch the user's desktop. The user's real desktop only when the user asks for that task, with the same S8 card and local checks. |
| Limits | Depends on the user's hardware | Machines only in Germany, Finland and France. No GPU, so no large local model inside the sandbox. Auto-stop defaults to 1 h (configurable up to 30 days, or off on paid plans). A stopped sandbox keeps its disk for at least 14 days, but memory and processes are not saved across a stop. The IP changes on resume. |

Boat figures were read from boat.dev on 2026-11-23 and belong to a third party, so
re-check them before relying on them. An earlier draft pilot (Nov 9, now
archived) planned to measure how long Boat desktop tasks take and what they cost.
It was never run, so measured numbers are still missing. Because the IP changes on
resume, the desktop app must not pin a Boat brain by IP address. The brain inside
the sandbox listens on 127.0.0.1 only, with no public port and no Boat public
URL. The app reaches it through an SSH local port forward to the sandbox's
loopback port. Boat documents this as `boat forward`, which binds 127.0.0.1 on
the user's computer by default and makes nothing public
(<https://docs.boat.dev/ssh-access.md>). The owner chose this on 2026-11-24
(open question 3). The rules are in
[ADR 0004 §2, item 8](adr/0004-desktop-companion.md#2-decision) and
[THREAT_MODEL §6, L4](THREAT_MODEL.md#6-local-brain-boundary-local-mode). They
are planned and not built.

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

**Decision (owner, 2026-11-24): option (a).** Deliver it through signed per-OS
installers (dmg for macOS, msi for Windows, AppImage and deb for Linux), plus a
one-line script that fetches the same signed installer for people who prefer a
terminal. Updates use Tauri's updater. This keeps one artifact per OS and one
runtime that Kelp controls, and the one-line script becomes a convenience
instead of a second install path.

Signing timing: unsigned Linux builds are fine while building in public. macOS
signing and notarisation and Windows signing are required before general users
are invited (Phase 4 in section 16).

Costs to plan for: Apple code signing and notarisation, a Windows code-signing
certificate, and the updater signing keys. The desktop app today ships unsigned
dev builds only, with the updater off (`desktop/README.md`). Phase 1 builds the
installer.

## 7. Reacting versus acting

Kelp does two different kinds of work, and they must not share a cost model.

**Reaction loop (cheap, local, frequent).** Reaction calls are the frequent,
small model calls the avatar makes to react to what the user is doing. The loop
watches cheap signals: the active
app, idle time, other OS signals, and an occasional downscaled screenshot. It is
rate-limited and uses a small, cheap model, ideally a local one. Its output is a
pet state, such as the existing `curious` or `surprised`, and at most one short
line. The reaction loop has no tool access. It can propose an offer, but an offer
only turns into an action when the user accepts and a normal turn starts.

With the desk pet turned off (planned), the reaction loop still runs on the apps
the user allowed ([THREAT_MODEL S1](THREAT_MODEL.md#7-screen)). Its line appears
as a small notification from the tray or menu-bar icon instead of a pet state,
under the same rate limit.

**Actions (rare, expensive, gated).** Anything that does work runs through the
kernel, the skills, and the approval gate. This is the path that exists today.
Screen control (computer use on the user's own desktop or a Boat sandbox
desktop) is one of these actions and is available in every run mode (in Boat
mode the sandbox desktop is the default target). On the user's own desktop Kelp
works in the background with its own pointer and keyboard input. It acts on an
approved window directly, or opens the app in its own workspace. It never moves
the user's pointer, types on the user's keyboard focus, raises a window or
switches the user's workspace, so the user keeps working while Kelp works. Its
rules are in [THREAT_MODEL §7, S8](THREAT_MODEL.md#7-screen) (planned, not
built). Typing into a terminal, or into any field that launches a program,
counts as running a command. Commands are allowed behind approvals (planned,
owner decision 2026-11-24, answer 14): in "Ask before doing anything" each
command shows a card with its exact text, and in "Approve for me" only a command
on a read-only or non-destructive allowlist skips the card
([THREAT_MODEL §9, T8](THREAT_MODEL.md#9-self-setup-the-capability-resolver)).
Commands run through `openshell/exec_gate.py`, not through keystrokes. The exec
boundary in [`docs/PRIVACY.md`](PRIVACY.md) §5 stays in place until the wiring
steps listed there land in one change.

The cost reason is direct. Spend caps fail closed (`kernel/usage.py`), so a
frontier model called on every frame would exhaust the cap and then stop the
whole assistant, including the actions the user actually asked for. Reaction
calls draw from the same single budget as everything else. Kelp rations them
internally: a small fixed share of the budget and a rate limit, so reactions can
never use up the money for real work. This rationing is not a user setting. It is
planned. The share and the rate are not set yet, and no code applies them today.

## 8. Screen and voice privacy rules

The detailed rules are written as planned rules in
[THREAT_MODEL §7 (screen)](THREAT_MODEL.md#7-screen) and
[§8 (voice)](THREAT_MODEL.md#8-voice). No code enforces them yet. In short:

- Screen watching is opt-in per app, chosen once on a single screen. First run
  shows app categories with suggested defaults already ticked (editor, terminal,
  most of the browser), and the user changes them in that one place.
- A visible indicator shows whenever Kelp is watching or listening. Kelp draws
  it itself: on wlroots compositors the OS shows nothing during capture.
- Capture covers only the focused allowed window, and it fails closed: if Kelp
  cannot tell which app, window or site has focus, it takes no frame. For screen
  control the frame comes from the window Kelp is acting in, not the focused one
  (S8), and the same allowed list and block list still decide.
- Frames are never written to disk. Kelp may keep short text observations for at
  most 24 hours, stored as untrusted data. Anything longer-term enters memory only
  after the user okays it.
- Some apps are always blocked and the user cannot lift that: password managers,
  private browser windows, Kelp's own credential screen, and authenticator apps.
  Banking and payment sites are blocked by default. The user can unlock one site
  at a time, only from Settings, never from a setup card or chat.
- Screen text, including window titles, can never authorise an action. It is
  data, handled as untrusted input like any tool result. The existing credential
  flow already follows the same rule for private entry: the browser wrapper
  refuses to screenshot while private entry is active
  ([`docs/CREDENTIAL_HANDOFF.md`](CREDENTIAL_HANDOFF.md)).
- Voice defaults to push-to-talk. A wake word ("Hey Kelp") is an optional opt-in
  and is detected on the device. Audio is deleted once it is transcribed, and
  only the text is kept.

## 9. Persona

Scheduled for Phase 3 with the identity (section 16). Until then Kelp uses one
neutral built-in persona.

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

**Decided: the avatar is optional. Undecided: what it looks like.** The owner
decided on 2026-11-24 that users choose a desk pet or no pet. Without it, Kelp
lives in a tray or menu-bar icon plus the chat window, the phone app and
Telegram, and keeps screen reactions, screen control and voice. None of this
exists in `desktop/` today: there is no tray icon and no pet switch. Kelp's
identity is still open, and the owner does not want to commit to Storm's pet
sprites or an anime style yet. The owner ranked identity lower than the desktop
app and the phone app (answer 15), so the pet and identity are Phase 3 work
(section 16).

**Avatar.** The identity is undecided. The existing ten-state kit in
`assets/kelp-pet/` (idle, working, waiting, done, offline, curious, happy,
excited, thinking, surprised) is the placeholder pet, and its state names are
the interface any avatar must implement. The kit ships 384×384 animated WebP,
64-frame atlases, and a `kelp-pet.js` web component. VRM through three-vrm,
which is open, or Live2D, are options, not a plan. The Live2D Cubism SDK is not
open source, so check its licence before adopting it.

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
- **Local-only voice** needs a GPU or NPU to be conversational. On machines
  without a capable GPU, local mode uses a cloud text model for voice replies,
  only when a provider (or the plan, section 15) is already set up.
  Speech-to-text and text-to-speech stay on the machine, so only text leaves it,
  matching the V3 default in [THREAT_MODEL §8](THREAT_MODEL.md#8-voice). This
  adds no setup step. Kelp says so when it does this. Without a provider, it
  offers text only.
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
exists. The current placeholder pet is on the branch
`origin/kel-31-pet-motion-kit` (Storm, kept). Its 3 commits are 3bd69c5 (animate
the pet from the kit's ten states), 4546460 (lean toward the cursor and fidget)
and 84fa565 (overlay pill alignment). The branch is based on `desktop-demo`,
which carries demo fixtures, so Phase 3 considers those commits (section 16),
by cherry-pick or a fresh PR and not by merging the branch. Issue #69 (Storm)
covers desktop demo mode. The main app also has a Computer pane
(`desktop/src/ComputerPane.tsx`): a
view-only noVNC viewer onto a VM's graphical session. It is the natural starting
point for watching Kelp work in a Boat sandbox's virtual desktop, and for
watching Kelp's own workspace on the user's machine (S8, tier 2).

## 11. What pauses

These stay on main and get no new work until the companion direction has
something to try:

- the payments wallet and Razorpay checkout. They stay paused for now, but they
  are the likely base for the plan's billing (section 15),
- multi-tenant entitlements.

**No longer paused: the phone app.** Ketock's native mobile app (see
[`docs/KETOCK_NATIVE_APP_HANDOFF.md`](KETOCK_NATIVE_APP_HANDOFF.md)) is now the
primary remote approval channel (requirement 9 in section 4.5, and section 4.6).
The work is coordinated with Ketock, and Phase 1 builds it in parallel with the
desktop app (section 16).

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

Screen control (S8) works without sharing the user's input seat, so it needs a
different mechanism on each OS: tier 1 acts on the target window through the
accessibility API, and tier 2 uses a workspace with an input seat of its own.
Planned and untested: macOS Accessibility (`AXUIElement` actions), Windows UI
Automation patterns (and window messages for native controls), and Linux AT-SPI
(Action, EditableText, Value). Tier 2 on Linux is a nested headless wlroots
compositor, where the virtual-pointer and virtual-keyboard protocols are used on
that compositor only. Per-window capture is also needed, so that Kelp can see
the window it acts in while another window has focus: ScreenCaptureKit on macOS,
Windows Graphics Capture on Windows, and
`ext_foreign_toplevel_image_capture_source_manager_v1` with
`ext_image_copy_capture_manager_v1` on wlroots. Input injected into the user's
own seat (uinput or `ydotool`, `zwlr_virtual_pointer_v1` and
`zwp_virtual_keyboard_v1` on the user's compositor, `SendInput`, HID-level
`CGEventPost`) is never used on the user's desktop (S8). The capture spike
probed the xdg-desktop-portal RemoteDesktop interface on the wlroots machine and
found it unavailable, and it named those virtual-input routes as the only
wlroots route on the user's seat. That route is now ruled out for the user's
desktop. No input was actually sent on any OS.

**Background-input spike (Phase 0, not yet run).** On each OS, drive an
unfocused test window through tier 1 and confirm three things: (a) the user's
pointer position and keyboard focus did not change, (b) the action landed, read
back through the same API, and (c) per-window capture of an occluded window
works. On wlroots, also run a tier 2 test in a nested compositor. Screen control
does not ship on an OS where tier 1 and tier 2 both fail this spike, and Kelp
offers Boat there instead. A global stop shortcut for each OS is a separate open
item (S8).

**Already probed, with no input sent.** On the owner's machine (mango 0.18.6, a
wlroots compositor) the compositor advertises `zwlr_virtual_pointer_manager_v1`,
`zwp_virtual_keyboard_manager_v1`, `ext_foreign_toplevel_list_v1`,
`zwlr_foreign_toplevel_manager_v1`,
`ext_foreign_toplevel_image_capture_source_manager_v1`,
`ext_image_copy_capture_manager_v1` and `zwlr_layer_shell_v1`. That shows the
protocols exist. It does not show that they work. The AT-SPI bus runs
(at-spi2-core 2.62.1), but at probe time only two processes were registered on
it: the polkit agent and xdg-desktop-portal-gtk. Other apps there may need
accessibility enabled before tier 1 can see them. Sway is installed on the same
machine, and the tier 2 test has not been run.

Prototype all three OSes before the design assumes that capture and background
input work without extra steps. Because some platforms ask for nothing, the
rules in section 8 will be enforced by Kelp's own capture code, never delegated to
the OS prompt. The capability resolver can guide the user to a permission
prompt, but it cannot grant an OS permission for them.

## 13. Companion risks

These need decisions before launch. The owner decided the last three on
2026-11-24. Emotional dependence is still open:

- **Proactive chatter.** An assistant that speaks too often becomes an annoyance
  and gets uninstalled. The default is that Kelp rarely speaks.
- **Emotional dependence.** A companion that is present all day can become
  something a user leans on too hard. Decide what Kelp does and does not encourage.
- **Minors and age stance (decided 2026-11-24).** There is no age gate on the
  open-source download. A paid plan needs an adult payer under the payment
  provider's terms. That is a billing rule, not an app gate.
- **How Kelp describes itself (decided 2026-11-24).** Kelp always says that it is
  an AI when asked, or when it matters. No persona can claim to be human or to
  have feelings it does not have.
- **Coursework (decided 2026-11-24).** Coursework is not a refusal category. Kelp
  may prepare, do and submit graded work when the user asks, like any other task.
  A submission is something another person sees, so it still gets a card like
  sending an email
  ([THREAT_MODEL §9, T4](THREAT_MODEL.md#9-self-setup-the-capability-resolver)).

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
| First-run questions | Questions the user answers before that first reply | 1 (Local or Boat), plus one provider sign-in only when no local model fits. Boat adds one question, the Boat sign-in that includes the owner bearer handoff. The desk pet switch (section 4.1) is not counted. |
| Setup prompts per active week | Kelp-initiated prompts shown per user per week after the first day: resolver plan cards, credential handoff requests, and resolver steps that ask the OS for a permission | ≤ 1 median, trending to 0 |
| Repeat asks | A Kelp-initiated setup prompt for a capability that was already approved and whose scope has not changed, excluding a re-request after an OS-initiated lapse (counted under OS re-consent) | **0.** Any non-zero value is a bug |
| OS re-consent | Reminders the OS raises on its own, such as the macOS 15+ "continue to allow" screen-recording prompt. Counted when Kelp detects the permission lapsed | Watched, not a bug |
| Resolver success rate | Approved setup plans whose self-check (section 4.2, step 5) passes on the first attempt | ≥ 95% |
| Setup abandonment | Setup cards dismissed or left to expire, as a share of cards shown | Watched, no target yet |
| Card approval rate | Share of cards approved unchanged | No target. It is the input for [THREAT_MODEL §9, T5](THREAT_MODEL.md#9-self-setup-the-capability-resolver) |
| Manual fixes | Self-check results that end in `manual_fix_required` (section 4.2, step 5). Bug reports with the same cause are tracked by hand | 0 |

Resolver plan cards are counted from the approval audit rows
(`approval_requested`, `approval_resolved`) once the resolver gets its own
approval kind (section 4.2), so setup cards can be counted separately from action
approvals. Credential handoff requests and OS permission steps get their own
audit rows when they are built. OS dialogs themselves are outside Kelp, so only
the step that triggers them is counted. Repeat asks are
detected by comparing a new plan's capability ids and scopes with the remembered
grants from section 4.3.

## 15. Money: one plan price

The core stays open source. The owner's answer (2026-11-24) is one price with no
per-feature limits that covers model usage and the relay. Boat compute depends
on the plan.

- **Plan user.** A user who wants Kelp to handle everything pays one plan price
  to start. It covers model usage (including reactions, voice, and downscaled
  screen frames for screen reactions and screen control), the push relay
  (section 4.6) and, depending on the plan, Boat compute. The user sees one
  number. The UI shows no sub-caps. Within a plan there are no per-feature
  limits. Whether a plan includes Boat compute depends on the plan.
- **Self-hoster.** A self-hoster runs the same open-source plan service
  components on their own side instead of using the project's: the push relay,
  and a model gateway only if they want one in front of their own provider key.
  Billing is not needed. A self-hoster pays Kelp nothing. They have at most one
  monthly cap, the existing `KELP_SPEND_CAP_MONTH_USD`
  ([`docs/DEPLOY.md`](DEPLOY.md)). The day cap (`KELP_SPEND_CAP_DAY_USD`) stays
  available to operators, but it is not shown to plan users.
- **What this means for the project.** The Kelp project will operate a hosted
  service for plan users: billing, a model gateway, the push relay, and Boat
  provisioning on plans that include it (decided 2026-11-24). The service is
  open source so self-hosters can run it. That is new. None of it exists today.
  Decision D (no public hosted instance until production) still holds until the
  plan service launches; decision D is revisited then. The existing Razorpay
  wallet and Checkout code (section 11) may be reused for it. A hosted plan's
  model gateway will receive conversation text and downscaled screen frames for
  apps the user marked "may send to cloud", which the privacy documents do not
  yet describe (see the follow-ups in
  [ADR 0004](adr/0004-desktop-companion.md#7-follow-ups)).

Price, plan tiers and what a plan includes are not set. Nothing here is built.

## 16. Phases

| Phase | Scope |
|---|---|
| 0 (now) | This brief and ADR 0004. Spikes for screen capture (done), voice (done) and background input (section 12, not yet run). The installer decision (done). |
| 1 | The desktop app: the local brain sidecar and installer, first run (section 4.1), remember-once (section 4.3), chat, approval cards with the permission modes (section 3), the tray or menu-bar icon, and Boat mode over the SSH forward. No desk pet. In parallel, with Ketock: the phone app with chat and push approvals (relay, THREAT_MODEL L6). The Telegram bridge comes after the phone app. |
| 2 | Actions: approval-gated skills, the full capability resolver with the signed catalog, commands through the exec gate (THREAT_MODEL T8), background screen control (THREAT_MODEL S8), screen awareness with reactions shown as notifications, and voice chat (the minimal resolver for voice models moves here). The resolver guides the user through the OS capture permission, and frames go to a local model or the user's own provider key until the plan gateway exists. |
| 3 | The desk pet and identity: decide Kelp's look, the avatar, animation, lip-sync and persona visuals, plus the persona file and its setup flow (section 9). Until then Kelp uses one neutral built-in persona. Consider Storm's 3 pet commits here (section 10), not before. Identity is still not decided. |
| 4 | Production hardening, signed and notarised installers (section 6), the real landing page, and invitations to the pre-register list. The plan service must exist before invitations. |

## 17. Phase 0 exit criteria and open questions

**Exit criteria (all checkable):**

- [ ] The owner has accepted or changed this brief and ADR 0004. The owner
      answered the open questions on 2026-11-24 (see below) but has not accepted
      the whole brief or the whole ADR.
- [x] The installer option is chosen, and the choice is written into ADR 0004.
      (Option (a), 2026-11-24.)
- [ ] A capture and background-input spike has run on Wayland, macOS and
      Windows, with notes on what each permission prompt looks like. For each OS
      it confirms that tier 1 drove an unfocused window without moving the
      user's pointer or keyboard focus, that the action landed (read back), and
      that per-window capture of an occluded window works. On wlroots it also
      tests tier 2 in a nested compositor (section 12). Wayland (wlroots)
      capture is done. For input, only the compositor's advertised protocols
      and the AT-SPI bus were probed, and nothing was sent. GNOME, KDE, macOS
      and Windows are not done.
- [x] A voice spike has measured the end-of-speech to start-of-reply latency of
      the local STT and TTS choice. Result: 3.1–3.3 s with a cloud model on one
      Linux laptop; the target is revised in section 10. A run on Apple Silicon
      and a Windows laptop is still useful before the number is final.
- [ ] The Live2D licence has been checked, and the AIRI and Open-LLM-VTuber
      licences have been checked for the parts to be reused.
- [x] [`docs/THREAT_MODEL.md`](THREAT_MODEL.md) has a plan for screen, voice and
      self-setup sections, to be written before any capture code lands. (sections
      written 2026-11-24, status planned)
- [ ] The first audience is decided (general users, 2026-11-24). The first
      pre-register `wish` answers are read to choose the first errands. As of
      2026-11-23 there are no submissions.
- [ ] Measured Boat desktop-task durations and cost exist, or the owner has
      accepted going without them.

**Open questions for the owner.** The owner answered questions 1 to 12 on
2026-11-24. The question text below is kept as a historical record. The answers
and where each one is applied are in
[Owner answers (2026-11-24)](#owner-answers-2026-11-24).

1. Is developers and students the right first audience, or should the pre-register
   answers decide first? (answered, see below)
2. Does the owner accept option (a) for the local installer, given the signing
   and notarisation cost? (answered, see below)
3. How does the desktop app reach a brain inside a Boat sandbox: an exposed HTTP
   port, or a tunnel? The answer is an SSH local port forward to a loopback-only
   brain. ADR 0004 item 8 and
   [THREAT_MODEL §6, L4](THREAT_MODEL.md#6-local-brain-boundary-local-mode) hold
   the rules. (answered, see below)
4. In local mode, where do the owner secrets live? A local installer has to
   generate the owner bearer (`KELP_AUTH_TOKEN`) and the credential settings
   `KELP_VAULT_KEY`, `KELP_BROWSER_WORKER_TOKEN` and `KELP_CREDENTIAL_SIGNING_KEY`
   ([`docs/CREDENTIAL_HANDOFF.md`](CREDENTIAL_HANDOFF.md)), and store them. It also
   sets the two non-secret settings `KELP_CREDENTIALS_ENABLED` and
   `KELP_BROWSER_ALLOWED_ORIGINS`. `docs/DESKTOP_SCOPE.md` says "token in OS
   keychain only", while `desktop/README.md` records that the old `keychain.js`
   and its Stronghold wiring were removed and nothing is stored at rest. It says
   any future at-rest storage belongs on the Rust side, inside `bridge_sign_in`,
   never in the webview. Local mode has to add that. (answered, see below)
5. Do reaction-loop calls get their own spend sub-cap? (answered, see below)
6. Should the resolver's catalog wait for publisher signing
   (`skills/signing.py`), or ship first with digest pinning only? (answered, see
   below)
7. What is the age and self-description stance (section 13)? (answered, see
   below)
8. Should voice replies in local mode fall back to a cloud model on machines
   without a GPU (section 10), or stay local and slower? (answered, see below)
9. How does an approval reach the user away from the desk? A late yes loses
   time-sensitive work, so Kelp needs a remote approval channel: a push to the
   phone, or a messaging bridge (section 4.5, requirement 9). (answered, see
   below)
10. Graded and timed work: should Kelp ever submit graded coursework the user
    reviewed, behind a card that
    [THREAT_MODEL §9, T4](THREAT_MODEL.md#9-self-setup-the-capability-resolver)
    requires, or only prepare it and remind the user? (answered, see below)

**Open questions raised by the answers:**

11. Will the Kelp project run the hosted plan service, and does the one price
    include Boat compute or only model usage and the relay? At the time of
    asking, under [THREAT_MODEL S3](THREAT_MODEL.md#7-screen) the plan's model
    gateway carries text only, so a plan user without their own provider key
    gets no frame-based reactions. Should the gateway carry downscaled frames
    for apps the user allowed, under the same no-storage rules? (answered, see
    below)
12. Should submissions to a course the user names be allowed under a standing
    approval, which T4 currently forbids for anything another person sees?
    (answered, see below)

**Open question raised by the later answers** (numbers 13 to 15 are used by the
later answers below, which did not answer a numbered question):

16. Which permission mode is the default on a new install? The recommendation is
    "Approve for me". Money, anything another person sees, anything that cannot
    be undone, new access and destructive commands always show a card in both
    modes, so the default removes only routine cards, which fits the zero
    repeated setup goal. The owner has not decided. (open)

### Owner answers (2026-11-24)

These are the owner's answers to questions 1 to 12 above. Each entry says where
the answer is applied. Nothing here is built unless the linked section says so.

1. **Audience: general users, anyone.** The first audience is general users with
   no technical background. No step ever needs a terminal, config file or API key
   copy-paste for a plan user, and the `wish` answers now refine which errands to
   support first. Applied in
   [section 2](#2-who-it-is-for-first-and-the-first-daily-loop).
2. **Installer: option (a) accepted.** Unsigned Linux builds are fine while
   building in public. macOS and Windows signing and notarisation are required
   before general users are invited. Applied in
   [section 6](#6-the-local-install-problem) and in
   [ADR 0004](adr/0004-desktop-companion.md#2-decision) (item 6 and the
   follow-ups). The ADR status stays Proposed.
3. **Reaching a Boat brain: SSH port forward.** The owner asked for best
   practice. The brain listens on loopback inside the sandbox, and the desktop
   app reaches it through an SSH local port forward with a per-device key and a
   pinned host key. Applied in [section 5](#5-run-modes), in
   [ADR 0004](adr/0004-desktop-companion.md#2-decision) (item 8) and in
   [THREAT_MODEL §6, L4](THREAT_MODEL.md#6-local-brain-boundary-local-mode).
4. **Local secrets: OS credential store, generated by Rust.** The owner asked
   for best practice. The Rust side generates all secrets and keeps the
   persistent ones in the OS credential store. The per-launch ones are held in
   memory and never stored. In Boat mode the Boat brain's bearer is stored too.
   Applied in
   [ADR 0004](adr/0004-desktop-companion.md#2-decision) (item 9) and in
   [THREAT_MODEL §6, L5](THREAT_MODEL.md#6-local-brain-boundary-local-mode).
5. **Money: one price that covers everything, no per-feature limits.** Reactions
   draw from the same single budget, and Kelp rations them internally. Applied in
   [section 7](#7-reacting-versus-acting), [section 11](#11-what-pauses) and
   [section 15](#15-money-one-plan-price). It raises open question 11.
6. **Catalog signing: ship with pinning first.** Kelp's own release-pinned
   entries ship first, and publisher signing comes before the catalog accepts any
   outside entry and before the full resolver in Phase 2. Applied in
   [section 4.4](#44-safety-limits) and in
   [ADR 0004](adr/0004-desktop-companion.md#5-consequences).
7. **Age and self-description: recommendation accepted.** There is no age gate on
   the open-source download, and Kelp always says it is an AI when asked or when
   it matters. Applied in [section 13](#13-companion-risks).
8. **Local voice without a GPU: cloud fallback.** Local mode uses a cloud text
   model for voice replies when a provider or the plan is already set up, and
   speech-to-text and text-to-speech stay on the machine. Applied in
   [section 10](#10-avatar-and-voice).
9. **Approvals away from the desk: Ketock's native app first, Telegram second.**
   A push relay carries content-free pushes, and a Telegram bridge shows only the
   action type and target. Applied in
   [section 4.6](#46-remote-approvals-planned),
   [section 11](#11-what-pauses),
   [ADR 0004](adr/0004-desktop-companion.md#2-decision) (item 10) and
   [THREAT_MODEL §6, L6 and
   L7](THREAT_MODEL.md#6-local-brain-boundary-local-mode).
10. **Graded coursework: no guardrails.** Kelp may prepare, do and submit
    coursework when the user asks, like any other task, so no refusal rule for it
    remains. The T4 card for anything another person sees stays, because it
    protects against injected instructions. This raises open question 12. Applied
    in [section 13](#13-companion-risks) and
    [THREAT_MODEL §9, T4](THREAT_MODEL.md#9-self-setup-the-capability-resolver).
11. **Plan service: yes for plan users; self-hosters run their own.** The
    project runs the hosted plan service for plan users. Whether a plan includes
    Boat compute depends on the plan. Self-hosters run the same open-source
    components on their side instead. Users choose a desk pet or no pet. Screen
    frames, screen-based reactions and screen control are supported in every
    mode, so the plan gateway carries downscaled frames for apps the user
    marked "may send to cloud" (off for every app by default), under the S2
    no-storage rule. Applied in
    [section 1](#1-what-kelp-becomes), [section 4.1](#41-first-run),
    [section 7](#7-reacting-versus-acting), [section 10](#10-avatar-and-voice),
    [section 12](#12-platform-permissions-risk),
    [section 15](#15-money-one-plan-price),
    [ADR 0004](adr/0004-desktop-companion.md#2-decision) (item 11) and
    [THREAT_MODEL §7, S3 and S8](THREAT_MODEL.md#7-screen). "Supported in every
    mode" does not switch watching on by default: S1 opt-in, the S4 block list
    and the S7 indicator still apply.
12. **Course submissions keep the card.** A submission is something another
    person sees, so it always shows an approval card, and a standing approval
    cannot cover it. T4 is unchanged.

### Later answers (2026-11-24)

The owner gave three more answers after questions 1 to 12. They did not answer
a numbered question. Each entry says where it is applied. Nothing here is built.

13. **Kelp never takes over; it has its own pointer and keyboard.** The owner
    does not want Kelp to take control. Kelp should have its own input devices,
    such as a mouse pointer and keyboard input, with which it uses apps that are
    not focused, so the user keeps working uninterrupted while Kelp works, the
    same way ChatGPT Work does. This replaces the earlier shared-input model for
    screen control, including "the user can always take over" and the rule that
    the user's own desktop is approved only at the desk. Starting control may
    now be approved at the desktop app or the paired phone app, and Telegram
    never may. The "takeover detection" spike is replaced by a background-input
    spike. Applied in [section 1](#1-what-kelp-becomes),
    [section 4.6](#46-remote-approvals-planned),
    [section 7](#7-reacting-versus-acting),
    [section 12](#12-platform-permissions-risk),
    [ADR 0004](adr/0004-desktop-companion.md#2-decision) (items 10 to 12) and
    [THREAT_MODEL §7, S8](THREAT_MODEL.md#7-screen).
14. **Permission modes, and commands behind approvals.** The approval rule
    stays, and approval becomes a button for things like these. Kelp still asks
    for destructive commands. A general toggle between "Ask before doing
    anything" and "Approve for me" will exist, the same way Kiro IDE has it. The
    owner's answer is read as follows: typing into a terminal still counts as
    running a command, and commands become allowed (planned), each approved
    with a button. Applied in [section 3](#3-principles),
    [section 7](#7-reacting-versus-acting),
    [ADR 0004](adr/0004-desktop-companion.md#2-decision) (item 12),
    [THREAT_MODEL §9, T4, T7 and
    T8](THREAT_MODEL.md#9-self-setup-the-capability-resolver) and a planned
    change note in [`docs/PRIVACY.md`](PRIVACY.md) §5. Question 16 above asks
    for the default mode.
15. **Identity is lower priority.** Complete the desktop app and the phone app
    before the desk pet and identity. Kelp's look stays undecided, and Storm's 3
    pet commits move from Phase 0 to Phase 3 as "consider". Applied in
    [section 1](#1-what-kelp-becomes), [section 4.1](#41-first-run),
    [section 10](#10-avatar-and-voice), [section 16](#16-phases) and
    [ADR 0004](adr/0004-desktop-companion.md#7-follow-ups).

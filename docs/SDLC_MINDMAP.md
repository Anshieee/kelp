# SDLC mind map — Always-On Private Assistant

Snapshot: 29 October 2026
Program target: **30 October 2026** for M0 through M4
Tracker site: [`netlify/index.html`](../netlify/index.html)

This file is the written record of the delivery. It answers three questions in plain words:
what the build is made of, how far each piece has got, and where the evidence lives. The picture
that goes with it is [`sdlc-mindmap.svg`](sdlc-mindmap.svg), with a PNG fallback at
`sdlc-mindmap.png`. Both are static files, so the diagram looks the same everywhere. The tracker
site embeds a copy of the SVG directly in the page, so every box there can be clicked; that copy
and these two files must be updated together.

What this snapshot rests on: `main` is at `1f08e53`, all **259 tests pass**, and the repository is
public. KEL-5 through KEL-9 are merged and reviewed. KEL-10 is agreed and not started.

## How to read the labels

| Label | What it means |
|---|---|
| **DONE** | Finished, merged into the shared copy of the code, and reviewed. |
| **IN REVIEW** | Merged into the shared copy, with a code review still open. |
| **SPEC FIXED** | The written spec exists, has been through review, and its fixes are applied. No code yet. |
| **IN PROGRESS** | Being built right now. |
| **TO COME** | Agreed, not started. |

An **M number** is a milestone, a stage of the build. A **KEL number** is a work item in Linear,
our task tracker. Linear can lag behind by a day; the statuses below match the tracker site on the
snapshot date.

**LATER** on the M4 leaves means outside this round.

## The map

![Mind map of the delivery: milestones M0 to M4, work items KEL-5 to KEL-10, product surfaces, payments, and design.](sdlc-mindmap.svg)

The same map in words, in case the image does not load:

- **Milestones** — five stages, M0 to M4.
- **The work** — six work items, KEL-5 to KEL-10: five done, one to come.
- **Surfaces** — where you talk to the assistant: dashboard first, Telegram next, WhatsApp and voice memos later.
- **Money** — Stripe now, Razorpay and an in-app balance later.
- **Design** — the screen plan is written, and so is the chat spec. The chat surface code is merged; the drawing file and the browser screen are next.

On the tracker site ([`netlify/index.html`](../netlify/index.html)) the same SVG is embedded
inside the page rather than loaded as a picture. Each box is a link: activating it opens the
matching milestone, work item, or decision below. M2 to M4 are drawn on the map as well. The
static `.svg` and `.png` files are still the source of the picture.

## Milestones

Every milestone currently points at the same date. Treat that as the program target, not a
promise that full-vision scope lands this week. Scope is cumulative: M2 includes everything in M1.

| Stage | Target | State | What finishing it means |
|---|---|---|---|
| **M0 Foundation** | 30 Oct 2026 | IN PROGRESS | The repo (public), the docs, the tests, the standard development setup, model and provider chosen, and a written list of what could go wrong. The repo, docs, tests, and skill contract are in. Still open: the automated checks, the development setup, and the threat model. |
| **M1 Prototype** | 30 Oct 2026 | IN PROGRESS | The end-to-end story: ask, get an answer with sources, read a file, approve an action, pay with a saved card, watch the six-scene demo. All six work items live here. Five are done and merged: KEL-5 to KEL-9. One remains: KEL-10, the wallet. |
| **M2 Polished MVP** | 30 Oct 2026 | TO COME | Survives ordinary use: picks up after a restart, self-checks, better search over your documents, repeatable quality measurements, and a deploy on the Nebius GPU. |
| **M3 Hardening** | 30 Oct 2026 | TO COME | The privacy and cost promises hold under pressure: who sees what, where keys live, caps on traffic, a retention rule, and a spend ceiling. |
| **M4 Full vision** | 30 Oct 2026 | TO COME | More devices, work the assistant starts on its own, abilities added by others, and team sharing. A direction, not a deadline. |

## The six work items

KEL-5 feeds KEL-6 and KEL-7. Those two feed KEL-8. KEL-7 also feeds KEL-10. KEL-8 comes before the
demo, KEL-9, which is merged; KEL-10, the wallet, is the only item left.

### KEL-5 — Skill contracts — DONE

The rulebook every new ability follows: what it does, what it takes in, what it gives back, and
what it is allowed to touch. Four starter abilities sit on top of it: web search, reading GitHub,
answering from PDFs and spreadsheets, and creating a scheduled job.

*Why it matters:* adding an ability becomes a small, contained job instead of a rewiring of the
assistant, and the safety layer knows what a tool intends to do before it runs.

*Plain words:* a **schema** is the shape of the data, written down so a machine can check it. A
**manifest** is the label on the box: what is inside and what it needs.

- Linear: https://linear.app/kelp-ai/issue/KEL-5/skill-contracts-schema-capability-manifest-4-prototype-skills
- Contract: [`docs/SKILL_SPEC.md`](SKILL_SPEC.md)
- Schema: [`skills/manifest.schema.json`](../skills/manifest.schema.json)
- Tests: [`tests/test_skill_loader.py`](../tests/test_skill_loader.py)
- Commit: [`b8de92f`](https://github.com/Anshieee/kelp/commit/b8de92f)

Status detail: merged into `main`; 60 tests cover the contract and the loader.

### KEL-6 — Kernel loop — DONE

The engine. It reads your message, asks the model what to do, streams the answer to you while it
is still being written, runs a tool when one is needed, feeds the result back, and stops at a
ceiling on steps and length. It also keeps a tally of what each turn costs.

*Why it matters:* the chat screen, the approval step, the wallet, and the demo all plug into this
loop.

*Plain words:* **streaming** means showing words as they arrive. A **tool call** is the assistant
asking a small program to do one specific thing. A **token** is a word-sized chunk of text, and
price is counted per chunk.

- Linear: https://linear.app/kelp-ai/issue/KEL-6/kernel-llm-streaming-agent-loop-with-tool-calling
- Design: [`docs/KERNEL.md`](https://github.com/Anshieee/kelp/blob/main/docs/KERNEL.md) (main)
- Loop: [`kernel/loop.py`](https://github.com/Anshieee/kelp/blob/main/kernel/loop.py)
- Tests: [`tests/test_kernel_loop.py`](https://github.com/Anshieee/kelp/blob/main/tests/test_kernel_loop.py)
- Merge: [`d3c763e`](https://github.com/Anshieee/kelp/commit/d3c763e) (branch [`kel-6`](https://github.com/Anshieee/kelp/tree/kel-6), head `4c72afc`)

Status detail: merged into `main` at `d3c763e`. The kernel suite is 43 tests — 25 for the loop and
18 for the streaming client — and passes with the rest of the 259. The review written up in
[`docs/REVIEW_KEL6_KEL7.md`](REVIEW_KEL6_KEL7.md) signed off, and KEL-8 built on top of it.

### KEL-7 — Approval gate — DONE

The check at the door. Before anything that changes something, this layer decides: allow, refuse,
or stop and ask you. Asking creates a one-time request that names exactly what will run and
expires after ten minutes. Every decision and result goes into a log that cannot be edited, with
keys and passwords removed.

*Why it matters:* a private assistant is only trustworthy if it cannot act behind your back. The
wallet and the demo both depend on this gate.

*Plain words:* an **audit log** is a diary that can only be added to. **Deny by default** means if
the rules say nothing, the answer is no.

- Linear: https://linear.app/kelp-ai/issue/KEL-7/openshell-approval-gated-tool-execution-audit
- Design: [`docs/OPENSHELL.md`](https://github.com/Anshieee/kelp/blob/main/docs/OPENSHELL.md) (main)
- Policy: [`openshell/policy.py`](https://github.com/Anshieee/kelp/blob/main/openshell/policy.py)
- Tests: [`tests/test_openshell_approvals.py`](https://github.com/Anshieee/kelp/blob/main/tests/test_openshell_approvals.py)
- Merge: [`daf3b14`](https://github.com/Anshieee/kelp/commit/daf3b14) (branch [`kel-7`](https://github.com/Anshieee/kelp/tree/kel-7), head `ef6ea9f`)

Status detail: merged into `main` at `daf3b14`. Policy, one-time approvals, gated execution, the
audit log, and 50 tests are in and passing. The review signed off, and KEL-8 built on top.

### KEL-8 — Dashboard chat — DONE

The main screen. Conversations you can name and pick up later, answers that appear as they are
written, a small openable card for each tool step, previews of PDF and spreadsheet answers, labels
showing where an answer came from, and approval requests shown in the conversation itself.

*Why it matters:* it is the front door. Nothing else can be shown to a person until this exists.

- Linear: https://linear.app/kelp-ai/issue/KEL-8/dashboard-chat-surface-primary-interface
- Chat spec: [`docs/CHAT_SPEC.md`](https://github.com/Anshieee/kelp/blob/main/docs/CHAT_SPEC.md) (main)
- Surface: [`chat/api.py`](https://github.com/Anshieee/kelp/blob/main/chat/api.py)
- Tests: [`tests/test_chat_service.py`](https://github.com/Anshieee/kelp/blob/main/tests/test_chat_service.py)
- Merge: [`e0bbb72`](https://github.com/Anshieee/kelp/commit/e0bbb72) (branch [`kel-8-build`](https://github.com/Anshieee/kelp/tree/kel-8-build), head `d9e8618`)
- Screen plan: [`docs/frontend-scope-simple.md`](frontend-scope-simple.md)

Status detail: merged into `main` at `e0bbb72`. The chat surface is in: named threads that can be
reopened, an append-only stream of events, a card for each tool step, approvals shown in the
conversation over the KEL-7 gate, artifacts, and a cooperative cancel token so Stop works. The chat
suite is 86 tests, part of the 259. The spec is on `main` at 1,164 lines; the review fixes that
shaped it landed at `56b0785`:

- **Honest dependencies (M1).** The web search, GitHub read, and document handlers are still stubs.
  The spec says so, and says the demo needs saved fixtures or recorded results instead of
  pretending live tools exist.
- **Attachments (M2).** A user message can carry a file, and the spec defines the chip, the record,
  and the moment the file is bound to a thread.
- **Sweep card (M3).** A run of GitHub reads gets one wider card, with its own scope row, instead of
  many look-alike rows.
- **Framing (M4).** Frame denial stays on the app pages, while the single file-preview endpoint is
  allowed inside the app's own sandboxed frame, so PDF previews work without making the app
  frameable.
- **Errors (N1).** Six missing error codes are added, plus a catch-all for anything unlisted. A
  late approval answers 409, not a success.
- **Limits (N2).** The message limit is 8,000 characters, not bytes. Budget timing uses a monotonic
  clock, so a wall-clock change cannot stretch a turn.
- **Scope (N3).** Shell execution is OpenShell-only, and source chips share one "+n more" overflow
  string.

The browser screen that draws this surface is still to come. The drawing file comes first, then the
screen code.

### KEL-10 — Wallet — TO COME

Saved cards through Stripe: save once, list, a per-purchase cap, a monthly cap, a confirmation
window, and a receipt. Every payment passes through the KEL-7 approval step, so nothing is
charged without a yes.

*Why it matters:* paying is the clearest case of something the assistant must never do on its own.

*Plain words:* **Stripe** is the payment company that stores card details, so the app never holds
them. **SetupIntent** is Stripe's name for the one-time step that saves a card for later.

- Linear: https://linear.app/kelp-ai/issue/KEL-10/wallet-stripe-saved-cards-setupintent-attach-list-propose-approve
- Placeholder: [`payments/stripe_wallet.py`](https://github.com/Anshieee/kelp/blob/main/payments/stripe_wallet.py) (main)
- Tests: [`tests/test_payments.py`](https://github.com/Anshieee/kelp/blob/main/tests/test_payments.py)

Status detail: the Linear issue is created and sits in Todo. Only a placeholder settings file
exists today, now on `main` because it came in with KEL-7. No real Stripe calls yet. Razorpay and
an in-app balance are outside this round.

### KEL-9 — Demo run — DONE

Six short scenes played in order by one command: ask and get sources; summarise a PDF and answer a
spreadsheet question; look up a GitHub issue; approve a write and see it in the log; schedule a
job; stop the server mid-answer and resume without losing anything. Each scene writes a receipt,
and the run stops with an error if a scene fails.

*Why it matters:* it is both the proof and the guard rail. A regression breaks the script before it
reaches a person.

*Plain words:* a **fixture** is a saved, make-believe model answer, so the run is repeatable and
works offline.

- Linear: https://linear.app/kelp-ai/issue/KEL-9/demo-choreography-6-beat-scripted-end-to-end-run-npm-run-demo
- Choreography: [`demo/README.md`](https://github.com/Anshieee/kelp/blob/main/demo/README.md) (main)
- Entry point: [`demo/runner.py`](https://github.com/Anshieee/kelp/blob/main/demo/runner.py)
- Tests: [`tests/test_demo.py`](https://github.com/Anshieee/kelp/blob/main/tests/test_demo.py)
- Merge: [`1f08e53`](https://github.com/Anshieee/kelp/commit/1f08e53) (branch [`kel-9`](https://github.com/Anshieee/kelp/tree/kel-9), head `60f653b`)

Status detail: merged into `main` at `1f08e53`. `make demo` plays six beats — 129 checks in about
two seconds — using the real chat service, kernel loop, skill registry, approval gate, artifact
registry, and HTTP API. The provider and the four handlers are recorded fixtures, so the run is
offline, deterministic, and needs no keys. Beat 6 kills a real child server with `SIGKILL`
mid-stream and proves recovery in a new process. The review findings are fixed: beat 5 now settles
its approval in the audit log, so no outcome stays `awaiting_execution`. Screenshot capture belongs
to the dashboard slice; the receipts are the review artefacts for this backend-only run.

## Decisions that shape the map

### Surfaces

- **Dashboard, now.** The main surface, and the place every flow is designed.
- **Telegram, next.** A mirror of the same assistant, under the same rules and the same approval step.
- **WhatsApp, later.** Not in this round. Older notes that call it the main mirror are out of date.
- **Voice memos, later.** No recording or listening work yet.

### Money

- **Stripe, now.** Saved cards with a per-purchase cap, a monthly cap, a confirmation window, and a
  receipt. Tracked by KEL-10.
- **Razorpay, later.** No integration in this prototype.
- **In-app balance, later.** Showing a balance, topping it up, and keeping a ledger all wait.

### Design

- **The screen plan is done.** Thirteen screens, three main flows, four states per screen. See
  [`docs/frontend-scope-simple.md`](frontend-scope-simple.md).
- **The chat spec is done, fixed, and merged.** Threads, approvals inside the conversation, and
  artifact cards are written out in
  [`docs/CHAT_SPEC.md`](https://github.com/Anshieee/kelp/blob/main/docs/CHAT_SPEC.md) on `main`,
  with the review fixes applied at `56b0785`.
- **The chat surface code is merged.** Backend threads, the event stream, cards, inline approvals,
  and artifacts came in with KEL-8 at `e0bbb72`.
- **The drawing file is next, then the browser screen.** No browser screen code exists until the
  drawing file exists.

## Where everything lives

### GitHub

- Repository (public): https://github.com/Anshieee/kelp
- Shared copy of the code: https://github.com/Anshieee/kelp/tree/main — head `1f08e53`
- All branches: https://github.com/Anshieee/kelp/branches
  - `kel-5` — merged (`b8de92f`)
  - `kel-6` — merged at `d3c763e`; branch head `4c72afc`
  - `kel-7` — merged at `daf3b14`; branch head `ef6ea9f`
  - `kel-8` — carries the spec history, head `56b0785`
  - `kel-8-build` — merged at `e0bbb72`; branch head `d9e8618`
  - `kel-9` — merged at `1f08e53`; branch head `60f653b`
- Change history: https://github.com/Anshieee/kelp/commits/main
- Pull requests: https://github.com/Anshieee/kelp/pulls
- Tests: 259 pass on `main` (`python -m pytest`); `make demo` plays all six beats

### Linear

- Project, Always-On Private Assistant:
  https://linear.app/kelp-ai/project/always-on-private-assistant-23297ecfd7f0
- Work items:
  [KEL-5](https://linear.app/kelp-ai/issue/KEL-5/skill-contracts-schema-capability-manifest-4-prototype-skills),
  [KEL-6](https://linear.app/kelp-ai/issue/KEL-6/kernel-llm-streaming-agent-loop-with-tool-calling),
  [KEL-7](https://linear.app/kelp-ai/issue/KEL-7/openshell-approval-gated-tool-execution-audit),
  [KEL-8](https://linear.app/kelp-ai/issue/KEL-8/dashboard-chat-surface-primary-interface),
  [KEL-9](https://linear.app/kelp-ai/issue/KEL-9/demo-choreography-6-beat-scripted-end-to-end-run-npm-run-demo),
  [KEL-10](https://linear.app/kelp-ai/issue/KEL-10/wallet-stripe-saved-cards-setupintent-attach-list-propose-approve)

### Docs and code

- Mind map image: [`docs/sdlc-mindmap.svg`](sdlc-mindmap.svg), PNG fallback `docs/sdlc-mindmap.png`
- Mind map source: [`docs/sdlc-mindmap.gv`](sdlc-mindmap.gv)
- Tracker site: [`netlify/index.html`](../netlify/index.html)
- Skill contract: [`docs/SKILL_SPEC.md`](SKILL_SPEC.md)
- Kernel design: [`docs/KERNEL.md`](https://github.com/Anshieee/kelp/blob/main/docs/KERNEL.md) (main)
- Approval gate design: [`docs/OPENSHELL.md`](https://github.com/Anshieee/kelp/blob/main/docs/OPENSHELL.md) (main)
- Chat surface spec: [`docs/CHAT_SPEC.md`](https://github.com/Anshieee/kelp/blob/main/docs/CHAT_SPEC.md) (main, review fixes at `56b0785`)
- Code review for KEL-6 and KEL-7: [`docs/REVIEW_KEL6_KEL7.md`](REVIEW_KEL6_KEL7.md)
- ADR 0001, one document skill: [`docs/adr/0001-docs-answer-single-skill.md`](adr/0001-docs-answer-single-skill.md)
- Screen plan: [`docs/frontend-scope-simple.md`](frontend-scope-simple.md)
- Issue pack: [`docs/linear-issue-pack-kel.md`](linear-issue-pack-kel.md)
- Linear setup notes: [`docs/linear-setup.md`](linear-setup.md)
- Manifest schema: [`skills/manifest.schema.json`](../skills/manifest.schema.json)
- Loader: [`skills/loader.py`](../skills/loader.py)
- Contract tests: [`tests/test_skill_loader.py`](../tests/test_skill_loader.py)

## Keeping this true

1. When a work item moves, change it here, in Linear, and on the tracker site in the same pass.
2. When a branch merges and its review signs off, mark the item DONE and link the merge commit.
3. When a date or the scope changes, change the milestone table, the Graphviz source
   `docs/sdlc-mindmap.gv`, and the rendered `docs/sdlc-mindmap.svg` and `.png` together. The
   tracker site carries its own copy of the SVG inline, so paste the new rendering into
   `netlify/index.html` in the same pass. The picture and the words must agree.
4. Deployment is handled separately. This repository does not publish the tracker.

## Words used here

- **SDLC** — software development life cycle: the whole trip from an idea to something shipped and looked after.
- **Milestone** — a stage of the build, not a feature.
- **Repo** — the project folder kept on GitHub, with its full history. **main** is the shared copy.
- **Branch** — a separate copy where new work is prepared before it joins main.
- **Kernel** — the thinking loop: read the message, decide, act, answer.
- **Manifest and schema** — the label on a tool's box, and the shape of the data inside it.
- **Approval gate** — a checkpoint that asks you before an action, and a yes that works once.
- **Fixture** — a saved sample answer, used so a test runs the same way every time.
- **Eval** — a repeatable test of answer quality, not just whether the code runs.
- **GPU** — the chip that runs large models quickly. Nebius provides ours.

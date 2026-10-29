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

What this snapshot rests on: `main` is at `daf3b14`, all **153 tests pass**, and the repository is
public. KEL-6 and KEL-7 are merged and waiting on review; KEL-8 has a written spec on its branch.

## How to read the labels

| Label | What it means |
|---|---|
| **DONE** | Finished, merged into the shared copy of the code, and reviewed. |
| **IN REVIEW** | Merged into the shared copy, with a code review still open. |
| **IN PROGRESS** | Being built right now. |
| **TO COME** | Agreed, not started. |

An **M number** is a milestone, a stage of the build. A **KEL number** is a work item in Linear,
our task tracker. Linear can lag behind by a day; the statuses below match the tracker site on the
snapshot date.

Two boxes on the map carry a plainer word instead of one of these labels: **SPEC DRAFTED** on
KEL-8 means the written spec exists and no code does, and **LATER** on the M4 leaves means outside
this round.

## The map

![Mind map of the delivery: milestones M0 to M4, work items KEL-5 to KEL-10, product surfaces, payments, and design.](sdlc-mindmap.svg)

The same map in words, in case the image does not load:

- **Milestones** — five stages, M0 to M4.
- **The work** — six work items, KEL-5 to KEL-10: one done, two in review, one in progress, two to come.
- **Surfaces** — where you talk to the assistant: dashboard first, Telegram next, WhatsApp and voice memos later.
- **Money** — Stripe now, Razorpay and an in-app balance later.
- **Design** — the screen plan is written, and so is the chat spec; the drawing file is next.

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
| **M1 Prototype** | 30 Oct 2026 | IN PROGRESS | The end-to-end story: ask, get an answer with sources, read a file, approve an action, pay with a saved card, watch the six-scene demo. All six work items live here. Four are moving: KEL-5 is done, KEL-6 and KEL-7 are merged and in review, and KEL-8 has a spec. |
| **M2 Polished MVP** | 30 Oct 2026 | TO COME | Survives ordinary use: picks up after a restart, self-checks, better search over your documents, repeatable quality measurements, and a deploy on the Nebius GPU. |
| **M3 Hardening** | 30 Oct 2026 | TO COME | The privacy and cost promises hold under pressure: who sees what, where keys live, caps on traffic, a retention rule, and a spend ceiling. |
| **M4 Full vision** | 30 Oct 2026 | TO COME | More devices, work the assistant starts on its own, abilities added by others, and team sharing. A direction, not a deadline. |

## The six work items

KEL-5 feeds KEL-6 and KEL-7. Those two feed KEL-8. KEL-7 also feeds KEL-10. KEL-8 and KEL-10
come before the demo, KEL-9.

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

### KEL-6 — Kernel loop — IN REVIEW

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

Status detail: merged into `main` at `d3c763e`. The kernel suite is 35 tests and passes with the
rest of the 153. The open code review is written up in
[`docs/REVIEW_KEL6_KEL7.md`](REVIEW_KEL6_KEL7.md); KEL-8 waits on its sign-off.

### KEL-7 — Approval gate — IN REVIEW

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
audit log, and 50 tests are in and passing. Sign-off from the review is the remaining step.

### KEL-8 — Dashboard chat — IN PROGRESS (spec drafted)

The main screen. Conversations you can name and pick up later, answers that appear as they are
written, a small openable card for each tool step, previews of PDF and spreadsheet answers, labels
showing where an answer came from, and approval requests shown in the conversation itself.

*Why it matters:* it is the front door. Nothing else can be shown to a person until this exists.

- Linear: https://linear.app/kelp-ai/issue/KEL-8/dashboard-chat-surface-primary-interface
- Chat spec: [`docs/CHAT_SPEC.md`](https://github.com/Anshieee/kelp/blob/kel-8/docs/CHAT_SPEC.md) (kel-8 branch)
- Spec commit: [`7bcf6d3`](https://github.com/Anshieee/kelp/commit/7bcf6d3)
- Screen plan: [`docs/frontend-scope-simple.md`](frontend-scope-simple.md)

Status detail: the spec is written and committed on `kel-8` — threads, in-chat approvals, and
artifact cards, 1,037 lines. The `kel-8` branch is not on GitHub yet, so those links open once it is
pushed. No screen code exists. Building starts once KEL-6 and KEL-7 are signed off.

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

### KEL-9 — Demo run — TO COME

Six short scenes played in order by one command: ask and get sources; summarise a PDF and answer a
spreadsheet question; look up a GitHub issue; approve a write and see it in the log; schedule a
job; stop the server mid-answer and resume without losing anything. It takes screenshots and stops
with an error if a scene fails.

*Why it matters:* it is both the proof and the guard rail. A regression breaks the script before it
reaches a person.

*Plain words:* a **fixture** is a saved, make-believe model answer, so the run is repeatable and
works offline.

- Linear: https://linear.app/kelp-ai/issue/KEL-9/demo-choreography-6-beat-scripted-end-to-end-run-npm-run-demo
- Repository: https://github.com/Anshieee/kelp (no demo script yet)

Status detail: runs last, after the dashboard and the wallet. Nothing is written for it yet.

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
- **The chat spec is done.** Threads, approvals inside the conversation, and artifact cards are
  written out in [`docs/CHAT_SPEC.md`](https://github.com/Anshieee/kelp/blob/kel-8/docs/CHAT_SPEC.md)
  on the `kel-8` branch.
- **The drawing file is next.** No screen code until it exists.

## Where everything lives

### GitHub

- Repository (public): https://github.com/Anshieee/kelp
- Shared copy of the code: https://github.com/Anshieee/kelp/tree/main — head `daf3b14`
- All branches: https://github.com/Anshieee/kelp/branches
  - `kel-5` — merged (`b8de92f`)
  - `kel-6` — merged at `d3c763e`; branch head `4c72afc`
  - `kel-7` — merged at `daf3b14`; branch head `ef6ea9f`
  - `kel-8` — carries the chat spec, head `7bcf6d3` (not on GitHub yet)
- Change history: https://github.com/Anshieee/kelp/commits/main
- Pull requests: https://github.com/Anshieee/kelp/pulls
- Tests: 153 pass on `main` (`python -m pytest`)

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
- Chat surface spec: [`docs/CHAT_SPEC.md`](https://github.com/Anshieee/kelp/blob/kel-8/docs/CHAT_SPEC.md) (kel-8 branch)
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
2. When a branch merges, move the item to IN REVIEW and link the merge commit; move it to DONE once
   the review signs off.
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

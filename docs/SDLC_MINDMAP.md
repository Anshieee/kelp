# SDLC mind map — Always-On Private Assistant

Snapshot: 30 October 2026
Program target: **30 October 2026** for M0 through M4
Tracker site: [`netlify/index.html`](../netlify/index.html)

This file is the written record of the delivery. It answers three questions in plain words:
what the build is made of, how far each piece has got, and where the evidence lives. The picture
that goes with it is [`sdlc-mindmap.svg`](sdlc-mindmap.svg), with a PNG fallback at
`sdlc-mindmap.png`. Both are static files, so the diagram looks the same everywhere. The tracker
site embeds a copy of the SVG directly in the page, so every box there can be clicked; that copy
and these two files must be updated together.

What this snapshot rests on: `main` is at `3f4d6cc`, all **734 tests pass**, and the repository is
public. KEL-5 through KEL-10 are merged, reviewed, and done; KEL-11, KEL-12, KEL-13, KEL-14, KEL-15,
and KEL-16 are merged and reviewed too, and close M2. KEL-10, payments, carries both its paths: the
Stripe saved-card scaffold, the Razorpay gateway, and the Razorpay hosted Checkout.

KEL-10 closed on **owner confirmation on 30 October 2026** (Assume-Done). The code paths are merged
and the suite is green; the order path was proven earlier against the Razorpay test API. The one
thing not physically observed is a person finishing a hosted Checkout in a browser, so that step is
taken on the owner's word, and the receipt it produces is to be verified on the next live run. With
that, **M1 Prototype is complete**: every work item inside it is done.

KEL-11 opened **M2 Polished MVP** on 30 October 2026. It is the serve entrypoint: one process, one
command, ordered boot, a single-instance lock, a clean stop, and boot recovery that closes out a turn
a crash left behind. It merged at `33994ab` from `kel-11` (head `46ba746`) and added 19 tests.

KEL-12 and KEL-13 then took M2 further, both merged on 30 October 2026. KEL-12 is health and
readiness: a fast liveness answer, a readiness answer that names each dependency, and bounded, cached
probes that leak no secrets. It merged at `66228bc` from `kel-12` (head `2c94e0e`) and added 38
tests. KEL-13 is real document search: PDF and CSV ingestion, chunking, hybrid ranking, and answers
quoted with page and row citations. It merged at `7ad39b7` from `kel-13` (head `66ddd70`) and added
68 tests. With the two of them in, the suite went from 510 to **616**.

KEL-14 then made the service observable, merged on 30 October 2026. It is one request id per call,
JSON log lines with a fixed field list and every string scrubbed, and a Prometheus registry behind
an authenticated route. It merged at `38130fb` from `kel-14` (head `38c3162`) and added 31 tests,
taking the suite from 616 to **647**.

KEL-15 then made answer quality measurable, merged on 30 October 2026. It is one offline command that
replays recorded model turns with the network blocked, scores 26 versioned cases across retrieval,
tool choice, approvals, restart recovery, and multi-turn history, and exits non-zero when a gate is
missed. It merged at `793569c` from `kel-15` (head `225f7c0`) and added 22 tests, taking the suite from
647 to **669**.

KEL-16 then closed M2 on 30 October 2026. It is the deploy slice: one two-stage image that runs as a
non-root user on a read-only filesystem, one Compose file that runs a named image and never builds or
pulls, one systemd unit that refuses to start without that tag, a stdlib health probe, and a
seventeen-section runbook from a bare VM to a working turn and back down again. It merged at
`3f4d6cc` from `kel-16` (head `a63d49a`) and added 65 tests, taking the suite from 669 to **734**.
No application code changed. Nothing was deployed to Nebius: every command in the runbook was run
locally against the image on 30 October 2026 and the measured results are recorded there, while the
end-to-end pass on a real host by someone other than the author is still owed.

## How to read the labels

| Label | What it means |
|---|---|
| **DONE** | Finished, merged into the shared copy of the code, and reviewed. |
| **COMPLETE** | A milestone whose work items are all done. |
| **IN REVIEW** | Merged into the shared copy, with a code review still open. |
| **SPEC FIXED** | The written spec exists, has been through review, and its fixes are applied. No code yet. |
| **IN PROGRESS** | Being built right now. |
| **TO COME** | Agreed, not started. |

An **M number** is a milestone, a stage of the build. A **KEL number** is a work item in Linear,
our task tracker. Linear can lag behind by a day; the statuses below match the tracker site on the
snapshot date.

**LATER** on the M4 leaves means outside this round.

## The map

![Mind map of the delivery: milestones M0 to M4, work items KEL-5 to KEL-16, product surfaces, payments, and design.](sdlc-mindmap.svg)

The same map in words, in case the image does not load:

- **Milestones** — five stages, M0 to M4.
- **The work** — twelve work items, KEL-5 to KEL-16: all twelve done, and M1 and M2 closed, with the
  six M2 pieces merged.
- **Surfaces** — where you talk to the assistant: dashboard first, Telegram next, WhatsApp and voice memos later.
- **Money** — saved cards through Stripe and hosted Checkout through Razorpay, both merged and closed out; an in-app balance later.
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
| **M1 Prototype** | 30 Oct 2026 | COMPLETE | The end-to-end story: ask, get an answer with sources, read a file, approve an action, pay with a saved card, watch the six-scene demo. All six work items live here, and all six are done: KEL-5 to KEL-9 were merged earlier, and KEL-10 closed on owner confirmation on 30 October 2026. |
| **M2 Polished MVP** | 30 Oct 2026 | COMPLETE | Survives ordinary use: picks up after a restart, self-checks, better search over your documents, an honest account of what it did, repeatable quality measurements, and a deploy a stranger can repeat. All six work items are done: KEL-11, the serve entrypoint; KEL-12, health and readiness; KEL-13, real document search with citations; KEL-14, structured logs and metrics; KEL-15, the eval harness that measures answer quality; KEL-16, the image and runbook. The runbook's end-to-end pass on a real Nebius host is still to be done. |
| **M3 Hardening** | 30 Oct 2026 | TO COME | The privacy and cost promises hold under pressure: who sees what, where keys live, caps on traffic, a retention rule, and a spend ceiling. |
| **M4 Full vision** | 30 Oct 2026 | TO COME | More devices, work the assistant starts on its own, abilities added by others, and team sharing. A direction, not a deadline. |

## The twelve work items

KEL-5 feeds KEL-6 and KEL-7. Those two feed KEL-8. KEL-7 also feeds KEL-10. KEL-8 comes before the
demo, KEL-9. All twelve are done. KEL-10, payments, closed M1 on owner confirmation on 30 October
2026: both of its paths were merged and reviewed earlier, and the live step was taken on the owner's
word. KEL-11 then opened M2 with the serve entrypoint, and KEL-12, KEL-13, KEL-14, KEL-15, and KEL-16
followed with health checks, real document search, observability, the eval harness, and the deploy.

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
18 for the streaming client — and passes with the rest of the 734. The review written up in
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
suite is 86 tests, part of the 734. The spec is on `main` at 1,164 lines; the review fixes that
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

### KEL-10 — Payments — DONE

Two ways to pay, one ledger. Saved cards through Stripe: save once, list, a per-purchase cap, a
monthly cap, a confirmation window, and a receipt. Hosted Checkout through Razorpay: the browser
pays on Razorpay's page, and the order is reserved against the same caps before it leaves the
machine. Every payment passes through the KEL-7 approval step, so nothing is charged without a yes.

*Why it matters:* paying is the clearest case of something the assistant must never do on its own.

*Plain words:* **Stripe** is a payment company that stores card details, so the app never holds
them. **SetupIntent** is Stripe's name for the one-time step that saves a card for later.
**Razorpay** is a second payment company, and a **hosted Checkout** is its own payment page — the
card is typed there, not here. A **signature** is a short code computed with a shared secret; it
proves a message really came from Razorpay.

- Linear: https://linear.app/kelp-ai/issue/KEL-10/wallet-stripe-saved-cards-setupintent-attach-list-propose-approve
- Wallet: [`payments/wallet.py`](https://github.com/Anshieee/kelp/blob/main/payments/wallet.py) (main)
- Wallet HTTP surface: [`chat/wallet.py`](https://github.com/Anshieee/kelp/blob/main/chat/wallet.py) (main)
- Razorpay gateway: [`payments/razorpay_gateway.py`](https://github.com/Anshieee/kelp/blob/main/payments/razorpay_gateway.py) (main)
- Checkout: [`payments/razorpay_checkout.py`](https://github.com/Anshieee/kelp/blob/main/payments/razorpay_checkout.py) (main)
- Checkout HTTP surface: [`chat/checkout.py`](https://github.com/Anshieee/kelp/blob/main/chat/checkout.py) (main)
- Tests: [`tests/test_wallet.py`](https://github.com/Anshieee/kelp/blob/main/tests/test_wallet.py),
  [`tests/test_wallet_api.py`](https://github.com/Anshieee/kelp/blob/main/tests/test_wallet_api.py),
  [`tests/test_razorpay_gateway.py`](https://github.com/Anshieee/kelp/blob/main/tests/test_razorpay_gateway.py),
  [`tests/test_razorpay_checkout.py`](https://github.com/Anshieee/kelp/blob/main/tests/test_razorpay_checkout.py), and
  [`tests/test_checkout_api.py`](https://github.com/Anshieee/kelp/blob/main/tests/test_checkout_api.py)
- Merge, saved cards: [`5e270eb`](https://github.com/Anshieee/kelp/commit/5e270eb) (branch
  [`kel-10-scaffold`](https://github.com/Anshieee/kelp/tree/kel-10-scaffold), head `a98054b`)
- Merge, Razorpay gateway: [`5472c3b`](https://github.com/Anshieee/kelp/commit/5472c3b) (branch
  [`kel-10-razorpay`](https://github.com/Anshieee/kelp/tree/kel-10-razorpay), head `cabefb7`)
- Merge, hosted Checkout: [`c474c72`](https://github.com/Anshieee/kelp/commit/c474c72) (branch
  [`kel-10-checkout`](https://github.com/Anshieee/kelp/tree/kel-10-checkout), head `f07baee`)
- Placeholder settings file it grew out of: [`payments/stripe_wallet.py`](https://github.com/Anshieee/kelp/blob/main/payments/stripe_wallet.py)

Status detail: merged into `main` in three steps — saved cards at `5e270eb`, the Razorpay gateway at
`5472c3b`, and hosted Checkout at `c474c72`. Everything runs offline: a SQLite ledger for cards,
proposals, and receipts; save-once and list; per-purchase and monthly caps; a ten-minute
confirmation window; approve or deny as a single-use compare-and-set that also reserves the amount,
so two approvals cannot overspend; an idempotency key per order so a retry cannot charge twice; and
HTTP surfaces under `/api/wallet` and `/api/wallet/checkout` with the same bearer-token, cookie, and
CSRF rules as the chat API. Checkout adds four routes — status, create, confirm, and webhook — and
settles only when a constant-time HMAC signature checks out *and* a provider fetch agrees on the
order, amount, and currency. Settlement is idempotent by receipt, a definitive mismatch fails the
order and releases its reservation, and an unknown outcome stays approved for a retry.

The suite grew by 266 tests: 54 for the wallet, 32 for its HTTP surface, 72 for the Razorpay
gateway, 52 for Checkout, and 22 for the Checkout HTTP surface — 491 in total at that merge. KEL-11
then took the suite to 510.

The review of the Checkout path raised four findings, and all four are fixed. A Checkout order and a
saved-card approval can no longer race for the same order: the reservation writes the checkout claim
inside the same transaction that checks the cap, so the loser gets a 409. A paid order whose
checkout row has no provider payment id is refused rather than returning a receipt someone else
settled. A definitive provider error no longer fails an order that already has a Checkout
registered. And once `RAZORPAY_WEBHOOK_SECRET` is set, the `X-Razorpay-Signature` header is the only
accepted webhook authentication: a per-payment signature in the body is not a fallback, and an
empty header counts as missing.

Keys are read from the server environment only. They never reach a browser, the ledger, a status
payload, an error body, or a `repr`. Card details are reduced to brand, last4, and expiry; card-like
input is rejected rather than echoed. Every real provider call sits behind a transport interface,
and the tests inject a fake, so no request leaves the machine and no money moves.

Closed on owner confirmation, 30 October 2026 (Assume-Done). The code paths are merged, reviewed, and
green: the order path was proven earlier against the Razorpay test API. The step not physically
observed is a person finishing a Checkout in a browser with `RAZORPAY_KEY_ID` and
`RAZORPAY_KEY_SECRET` set in the server environment; the owner confirmed that run, and the receipt it
returns is to be verified on the next live run. Nothing in the code is outstanding for it. The Linear
item is Done. An in-app balance is outside this round.

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

### KEL-11 — Serve entrypoint — DONE

One process, one command. `make serve` loads the settings, checks them one variable at a time,
creates its folders owner-only, opens the database under a lock, settles anything a previous run left
behind, binds the port, and logs what it did. `make check-serve` runs the same checks without opening
the database or binding a port, so a supervisor can ask "is this configured?" safely.

*Why it matters:* it is the difference between a set of working parts and something you can start. It
is also what makes a restart safe: the turn in flight is settled, and nothing runs twice.

*Plain words:* a **graceful shutdown** stops accepting new work, finishes what is in hand, and exits
instead of being cut off. A **lock** marks the database as taken, so a second copy cannot quietly
write over the first. An **interrupted turn** is one a restart closed out, keeping the steps that had
already finished.

- Linear: https://linear.app/kelp-ai/issue/KEL-11/m2-a-serve-entrypoint-boot-graceful-shutdown-restartresume
- Server: [`chat/server.py`](https://github.com/Anshieee/kelp/blob/main/chat/server.py)
- Boot recovery: [`chat/recovery.py`](https://github.com/Anshieee/kelp/blob/main/chat/recovery.py)
- Tests: [`tests/test_server.py`](https://github.com/Anshieee/kelp/blob/main/tests/test_server.py)
- Settings surface: [`/.env.example`](https://github.com/Anshieee/kelp/blob/main/.env.example)
- Merge: [`33994ab`](https://github.com/Anshieee/kelp/commit/33994ab) (branch
  [`kel-11`](https://github.com/Anshieee/kelp/tree/kel-11), head `46ba746`)

Status detail: merged into `main` at `33994ab`, adding 19 tests to a suite of 510. Boot is ordered
and fails fast: an unreadable or invalid setting raises a named `ConfigError` carrying the variable
and the step, every created directory is made `0700`, and a second process on the same database or
port is refused with `database_locked` rather than racing. `ServerConfig.__repr__` reports only
whether a token or key is configured, so a secret cannot leak through a log line or a traceback.
Shutdown takes `SIGTERM` or `SIGINT`, stops accepting work, settles in-flight turns inside a fixed
window, closes the database, and exits zero. Boot recovery marks a still-running turn interrupted
with its completed steps kept, fails tool blocks stuck in `running` or `waiting_approval` with
`error.code = "interrupted"`, and expires approvals whose waiter died with the process — recording
the same `approval_resolved` and `approval_expired` audit entries a normal decision would, with no
command text or secret values. Two review findings were fixed before the merge: in-flight turns are
settled on shutdown rather than abandoned, and `--check` is a true dry run. Still in M2 at that
point: the dashboard's Retry and Resume buttons, better search, quality measurements, and the
deploy. The last three of those arrived as KEL-13, KEL-15, and KEL-16; the Retry and Resume
buttons are still open.

### KEL-12 — Health and readiness — DONE

Two endpoints that answer two different questions. `/healthz` is liveness: the process is up and
answering, with no dependency calls, so it stays fast and stays 200 even while a dependency is
degraded. `/readyz` is readiness: the database is reachable and writable, migrations are at head,
the skill manifests loaded, the data directory writable, and the model provider answers within a
bounded probe. The reply names each check with its status and latency, and any required check failing
returns 503 with the failing check named.

*Why it matters:* "it is running" and "it can serve" are different answers, and a supervisor needs
both. Without the split, a service is either restarted for no reason or keeps taking traffic it
cannot handle.

*Plain words:* a **liveness check** asks "are you there?", a **readiness check** asks "can you do the
job now?", a **dependency** is something the service needs, and a **probe** is a small, time-limited
test of one.

- Linear: https://linear.app/kelp-ai/issue/KEL-12/m2-b-health-and-readiness-healthz-readyz-with-dependency-checks
- Routes and checks: [`chat/api.py`](https://github.com/Anshieee/kelp/blob/main/chat/api.py) (main)
- Wiring: [`chat/server.py`](https://github.com/Anshieee/kelp/blob/main/chat/server.py)
- Spec section: [`docs/CHAT_SPEC.md`](https://github.com/Anshieee/kelp/blob/main/docs/CHAT_SPEC.md) §15.6
- Tests: [`tests/test_health.py`](https://github.com/Anshieee/kelp/blob/main/tests/test_health.py)
- Merge: [`66228bc`](https://github.com/Anshieee/kelp/commit/66228bc) (branch
  [`kel-12`](https://github.com/Anshieee/kelp/tree/kel-12), head `2c94e0e`)

Status detail: merged into `main` at `66228bc`, adding 38 tests to a suite of 548. Liveness is public
and makes no state calls; readiness uses the same authentication as the rest of the API. Every probe
is bounded and cached, and the bodies carry no tokens, absolute paths, or exception text. The review
fixed four things before the merge: a provider probe accepts only a 2xx, so a redirect or an auth
rejection reads as degraded; a cached answer is stamped when the probe completes, so its reported age
and thirty-second reuse window describe the answer rather than when the scrape started; production
wiring is exercised through the real app builder, so a check that is configured but not connected
cannot pass unnoticed; and probe selection is by presence, not truthiness, so a falsy callable is
still run.

### KEL-13 — Real document search — DONE

The fixture-only document path is gone. A PDF is read page by page with page numbers kept; a CSV is
read with its header, inferred column types, and record numbers kept. Files are stored by content
hash, so re-uploading the same bytes changes nothing. Documents are cut into overlapping,
token-bounded chunks that carry their page, sheet, or row range and never cross a document boundary.
Retrieval is hybrid — a lexical score and an embedding score merged with a deterministic tie-break, a
top-k budget, and a relevance floor — and the answer quotes the chunks that won, with a citation that
names the page or row.

*Why it matters:* an answer from your own files is only trustworthy if you can check it against the
source. A citation you can open is the difference between "the model said so" and "here is the page".

*Plain words:* **chunking** is cutting a long document into pieces small enough to search well. An
**embedding** is a list of numbers that stands for the meaning of a piece of text, so similar
meanings sit close together. **Hybrid search** combines word matching with meaning matching. A
**citation** is the label saying which page or row an answer came from.

- Linear: https://linear.app/kelp-ai/issue/KEL-13/m2-c-retrieval-real-docsanswer-with-pdfcsv-chunking-ranking-citations
- Parsing: [`extract.py`](https://github.com/Anshieee/kelp/blob/main/skills/builtin/docs_answer/extract.py) (main)
- Chunking: [`chunking.py`](https://github.com/Anshieee/kelp/blob/main/skills/builtin/docs_answer/chunking.py)
- Index: [`index.py`](https://github.com/Anshieee/kelp/blob/main/skills/builtin/docs_answer/index.py)
- Ranking: [`retrieval.py`](https://github.com/Anshieee/kelp/blob/main/skills/builtin/docs_answer/retrieval.py)
- Answering: [`answer.py`](https://github.com/Anshieee/kelp/blob/main/skills/builtin/docs_answer/answer.py)
- Tests: [`tests/test_docs_answer.py`](https://github.com/Anshieee/kelp/blob/main/tests/test_docs_answer.py) and
  [`tests/test_docs_answer_index.py`](https://github.com/Anshieee/kelp/blob/main/tests/test_docs_answer_index.py)
- Merge: [`7ad39b7`](https://github.com/Anshieee/kelp/commit/7ad39b7) (branch
  [`kel-13`](https://github.com/Anshieee/kelp/tree/kel-13), head `66ddd70`)

Status detail: merged into `main` at `7ad39b7`, adding 68 tests to a suite of 616. Parsing is
defensive: inline images are skipped whole so their binary payload cannot be read as text and
fabricate a citation; PDF form fields are followed in place with a depth cap and a cycle guard; a
malformed CSV becomes a plain `ValueError` rather than a raw parser exception; and an oversized cell
is clipped before it can dominate the index, the token budget, or the reply. The index is contained:
every write, delete, and load re-checks the resolved path, refuses a symlink where a directory should
be, and never deletes anything outside `var/index`. A single read supplies size, digest, and parse so
the file cannot change underneath the check, and a document-wide decompression budget charges even a
rejected bomb. Retention is deliberately lazy: deleting an artifact does not delete its index on the
spot, but the next question prunes the orphaned entry, and `remove_index` is the explicit hook for a
caller that needs an instant guarantee.

### KEL-14 — Observability — DONE

Every request gets one id, and that id follows the work: the response carries it, and so does every
log line the request produces. Logs are one JSON object per line on stdout with a fixed, small field
list — request id, thread, turn, tool, status, method, route, duration, model, tokens, cost, outcome,
error code — and every string value passes through the secret scrubber the audit trail already uses.
Free text is dropped, not truncated, so a prompt, a document, a tool argument, or an answer cannot
reach the log by naming a field. Metrics are a small Prometheus registry served as text at
`/api/metrics`, behind the same authentication as the rest of the API: HTTP requests and latency,
turns and their duration, tokens and cost, tool calls by name and status, approval wait, stream
resumes, and boot recoveries. Labels are scrubbed and length-capped, and a model-authored tool name is
resolved against the tools actually offered before it can become a label.

*Why it matters:* when a turn goes wrong, the useful question is which request, which turn, which
tool or model call, and how long it took. An id that follows the work and a counter that moves answer
it; a log full of payload text answers it by leaking the thing the service is trusted with.

*Plain words:* a **request id** is a short label on one call so every line about it can be found
together. A **JSON log line** is a log entry written as labelled fields instead of a sentence, so a
machine can filter it. **Prometheus** is a common way to publish numbers a monitor can scrape, and a
**histogram** is a set of buckets showing how long things usually take. A **label** is a tag on a
number, and keeping labels bounded stops the number of series from exploding.

- Linear: https://linear.app/kelp-ai/issue/KEL-14/m2-d-observability-structured-json-logs-prometheus-metrics
- Logging: [`chat/logging.py`](https://github.com/Anshieee/kelp/blob/main/chat/logging.py) (main)
- Metrics: [`chat/metrics.py`](https://github.com/Anshieee/kelp/blob/main/chat/metrics.py)
- HTTP surface: [`chat/api.py`](https://github.com/Anshieee/kelp/blob/main/chat/api.py)
- Correlation: [`chat/service.py`](https://github.com/Anshieee/kelp/blob/main/chat/service.py)
- Wiring: [`chat/server.py`](https://github.com/Anshieee/kelp/blob/main/chat/server.py)
- Tests: [`tests/test_logging.py`](https://github.com/Anshieee/kelp/blob/main/tests/test_logging.py) and
  [`tests/test_metrics.py`](https://github.com/Anshieee/kelp/blob/main/tests/test_metrics.py)
- Merge: [`38130fb`](https://github.com/Anshieee/kelp/commit/38130fb) (branch
  [`kel-14`](https://github.com/Anshieee/kelp/tree/kel-14), head `38c3162`)

Status detail: merged into `main` at `38130fb`, adding 31 tests to a suite of 647 — 11 for the logging
contract and 20 for the metrics registry, the route, and the correlation path. The review fixed three
behaviour bugs and four smaller ones before the merge. A histogram keeps a per-key observation total,
so a value above the largest finite bucket still lands in `+Inf` and in `_count` instead of being
dropped, and a non-finite value is refused rather than poisoning `_sum`. An unknown or crafted tool
name collapses to one fixed bucket, so it can neither mint a metric series nor inject a second log
line. Error codes are threaded into the tool-call log and audit entries and the turn-finished path,
shape-checked and kept out of labels, and the turn catch-all records only the exception type name.
Request ids are matched in full, so a trailing newline is rejected and an invalid id is never echoed.
Usage rows from the JSONL and in-memory stores are normalised to one shape, and the usage line says
whether the cost and the reported usage are known. JSON is emitted with non-finite numbers refused,
and the stock HTTP request logger is silenced, so the structured access line is the only record and
no unredacted request line reaches stderr.

### KEL-15 — Eval harness — DONE

One command answers the question the test suite cannot: are the answers still good? `make eval` is
offline, takes no keys, blocks outbound sockets, replays recorded model turns, and scores 26
versioned cases three times each. The cases cover the behaviours the prototype promises — a document
answer that cites a page or a row, a not-found answer instead of an invented one, the right tool for
a recorded turn, an approval that is created, taken once, expired, and redacted, a turn that a crash
left behind and boot closes out, and a thread history that stays isolated with idempotent client turn
ids. The run reports pass rate, citation precision and recall, tool accuracy, approval correctness,
p50 and p95 latency, tokens and estimated cost, and an error rate, then checks them against the gates
in `evals/thresholds.json` and exits non-zero on a miss or a regression.

*Why it matters:* "the tests pass" says the code runs, not that it still quotes the right page or
still refuses a write without a yes. A gate that names the broken behaviour is what stops quality
from drifting quietly between releases.

*Plain words:* an **eval** is a repeatable test of answer quality. A **recorded fixture** is a saved
model answer, replayed so the run is free and identical twice. A **gate** is a number the run has to
beat, and **deterministic** means the same input gives the same output every time.

- Linear: https://linear.app/kelp-ai/issue/KEL-15/m2-e-eval-harness-make-eval-offline-and-make-eval-live
- How to run it: [`evals/README.md`](../evals/README.md)
- Runner: [`evals/runner.py`](../evals/runner.py)
- Dataset: [`evals/datasets/v1/tasks.json`](../evals/datasets/v1/tasks.json)
- Gates: [`evals/thresholds.json`](../evals/thresholds.json)
- Offline tripwire: [`evals/offline.py`](../evals/offline.py)
- Tests: [`tests/test_eval_runner.py`](../tests/test_eval_runner.py), with
  [`tests/test_eval_dataset.py`](../tests/test_eval_dataset.py) and
  [`tests/test_eval_metrics.py`](../tests/test_eval_metrics.py)
- Merge: [`793569c`](https://github.com/Anshieee/kelp/commit/793569c) (branch
  [`kel-15`](https://github.com/Anshieee/kelp/tree/kel-15), head `225f7c0`)

Status detail: merged into `main` at `793569c`, adding 22 tests to a suite of 669 — 8 for the dataset
contract, 6 for the metrics, and 8 for the runner, the gates, and the determinism check. The observed
offline run scores 100% pass rate, 100% citation precision, 100% tool accuracy, and 100% approval
correctness with zero errors, in about 350 milliseconds against a one-minute budget, and the three
repeats hash identically. The network block replaces the connect and name-resolution paths with a
tripwire, so a task that tries to dial out fails the run rather than passing quietly. Recorded
fixtures are labelled `recorded://synthetic` in every report and contain no credentials and no real
documents. Two honest gaps against the written issue. The issue names a second target,
`make eval-live`; what shipped is `make eval EVAL_ARGS="--live"`, the only path that builds a real
provider client, which requires `NEBIUS_API_KEY`, is labelled and non-gating, and keeps third-party
tool output on deterministic stubs. And the regression compare reads the most recent offline report
for the same dataset version under the gitignored `var/evals`, so a clean checkout gates on the
thresholds alone while a working tree also catches a drop against the previous run.

### KEL-16 — Deploy — DONE

One image, one Compose file, one systemd unit, one probe, and one runbook. The image is built in two
stages: dependencies come from `pyproject.toml` into a venv, and only that venv is copied into a
slim runtime that runs as `10001:10001`, keeps its root filesystem read-only, drops every capability,
and declares `/data` as its only writable volume. Its `HEALTHCHECK` is a standard-library probe so
the image needs no `curl`. `docker-compose.yml` runs a named image and never builds or pulls: it
requires `KELP_IMAGE` with no default, publishes on loopback only, and caps the container at 1 GiB
and 2 CPUs. `deploy/kelp.service` starts the same project at boot, checks the tag and its image
before Compose runs, and so turns a pruned tag into a loud failure instead of a silent rebuild. The
runbook, [`docs/DEPLOY.md`](DEPLOY.md), is seventeen sections from a bare VM to a working turn: build,
first deploy, TLS through a reverse proxy, health and readiness, the `/data` layout, backup and
restore, the single-replica rule, rollout and rollback, smoke steps, cost, troubleshooting, a secret
checklist, and the verification record.

*Why it matters:* a deploy is where a tested repository becomes something a person can use, and where
every shortcut shows up — a secret baked into a layer, a container running as root, a second writer
on the same database, a reboot that leaves the service down, or a startup that quietly builds a
different image than the one that was tested. Each of those is closed on purpose here, and the
runbook records what was measured rather than what was hoped.

*Plain words:* a **container image** is one sealed copy of the app with its libraries inside. A
**tag** is the image's name and version. **Compose** is the short file that says how to run it.
**systemd** is the part of Linux that starts things at boot. A **reverse proxy** is the small server
in front that adds HTTPS. A **read-only root filesystem** means the app cannot rewrite its own code.

- Linear: https://linear.app/kelp-ai/issue/KEL-16/m2-f-nebius-deploy-dockerfile-gpu-runbook-restart-policy
- Runbook: [`docs/DEPLOY.md`](DEPLOY.md)
- Image: [`Dockerfile`](../Dockerfile), with [`.dockerignore`](../.dockerignore)
- Runtime: [`docker-compose.yml`](../docker-compose.yml),
  [`deploy/kelp.service`](../deploy/kelp.service), [`deploy/healthcheck.py`](../deploy/healthcheck.py)
- Env templates: [`deploy/kelp.env.example`](../deploy/kelp.env.example) and
  [`deploy/compose.env.example`](../deploy/compose.env.example)
- Tests: [`tests/test_deploy_assets.py`](../tests/test_deploy_assets.py) and
  [`tests/test_deploy_healthcheck.py`](../tests/test_deploy_healthcheck.py)
- Merge: [`3f4d6cc`](https://github.com/Anshieee/kelp/commit/3f4d6cc) (branch
  [`kel-16`](https://github.com/Anshieee/kelp/tree/kel-16), head `a63d49a`)

Status detail: merged into `main` at `3f4d6cc`, adding 65 tests to a suite of 734 — 40 asset checks
and 25 probe checks — with no application code changed. Four review fixes rode on top. The unit runs
`up -d --no-build` and fails before Compose starts when `KELP_IMAGE` is unset or its image is absent.
Section 12.3 gained the build-host venv line, so the offline smoke step runs on a fresh host. Only
`docker compose config -q` appears in the runbook, because the bare form prints the tokens. The asset
tests pin all of it, including that no deploy step builds through Compose. Two honest gaps. Nothing
was deployed to Nebius: the image build, boot to healthy, the read-only and non-root checks, restart
persistence, backup, and restore were executed locally against `kelp:9b1c863` on 30 October 2026 and
are recorded with their measured times, but the end-to-end pass on a real host by someone who did not
write the runbook is still owed. And the image is CPU-only by design: the model is a hosted service,
so the "GPU" in the milestone title belongs to that host, not to this container.

## Decisions that shape the map

### Surfaces

- **Dashboard, now.** The main surface, and the place every flow is designed.
- **Telegram, next.** A mirror of the same assistant, under the same rules and the same approval step.
- **WhatsApp, later.** Not in this round. Older notes that call it the main mirror are out of date.
- **Voice memos, later.** No recording or listening work yet.

### Money

- **Stripe saved cards, now.** A per-purchase cap, a monthly cap, a confirmation window, and a
  receipt. Tracked by KEL-10.
- **Razorpay hosted Checkout, now.** A second path into the same ledger: the order is reserved
  against the same caps, the card is typed on Razorpay's page, and settlement needs a signature plus
  a provider lookup that agrees on the order, amount, and currency. Also KEL-10.
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
- Shared copy of the code: https://github.com/Anshieee/kelp/tree/main — head `3f4d6cc`
- All branches: https://github.com/Anshieee/kelp/branches
  - `kel-5` — merged (`b8de92f`)
  - `kel-6` — merged at `d3c763e`; branch head `4c72afc`
  - `kel-7` — merged at `daf3b14`; branch head `ef6ea9f`
  - `kel-8` — carries the spec history, head `56b0785`
  - `kel-8-build` — merged at `e0bbb72`; branch head `d9e8618`
  - `kel-9` — merged at `1f08e53`; branch head `60f653b`
  - `kel-10-scaffold` — merged at `5e270eb`; branch head `a98054b`
  - `kel-10-razorpay` — merged at `5472c3b`; branch head `cabefb7`
  - `kel-10-checkout` — merged at `c474c72`; branch head `f07baee`
  - `kel-11` — merged at `33994ab`; branch head `46ba746`
  - `kel-12` — merged at `66228bc`; branch head `2c94e0e`
  - `kel-13` — merged at `7ad39b7`; branch head `66ddd70`
  - `kel-14` — merged at `38130fb`; branch head `38c3162`
  - `kel-15` — merged at `793569c`; branch head `225f7c0`
  - `kel-16` — merged at `3f4d6cc`; branch head `a63d49a`
- Change history: https://github.com/Anshieee/kelp/commits/main
- Pull requests: https://github.com/Anshieee/kelp/pulls
- Tests: 734 pass on `main` (`python -m pytest`); `make demo` plays all six beats, and
  `make eval` passes offline with the network blocked

### Linear

- Project, Always-On Private Assistant:
  https://linear.app/kelp-ai/project/always-on-private-assistant-23297ecfd7f0
- Work items:
  [KEL-5](https://linear.app/kelp-ai/issue/KEL-5/skill-contracts-schema-capability-manifest-4-prototype-skills),
  [KEL-6](https://linear.app/kelp-ai/issue/KEL-6/kernel-llm-streaming-agent-loop-with-tool-calling),
  [KEL-7](https://linear.app/kelp-ai/issue/KEL-7/openshell-approval-gated-tool-execution-audit),
  [KEL-8](https://linear.app/kelp-ai/issue/KEL-8/dashboard-chat-surface-primary-interface),
  [KEL-9](https://linear.app/kelp-ai/issue/KEL-9/demo-choreography-6-beat-scripted-end-to-end-run-npm-run-demo),
  [KEL-10](https://linear.app/kelp-ai/issue/KEL-10/wallet-stripe-saved-cards-setupintent-attach-list-propose-approve),
  [KEL-11](https://linear.app/kelp-ai/issue/KEL-11/m2-a-serve-entrypoint-boot-graceful-shutdown-restartresume),
  [KEL-12](https://linear.app/kelp-ai/issue/KEL-12/m2-b-health-and-readiness-healthz-readyz-with-dependency-checks),
  [KEL-13](https://linear.app/kelp-ai/issue/KEL-13/m2-c-retrieval-real-docsanswer-with-pdfcsv-chunking-ranking-citations),
  [KEL-14](https://linear.app/kelp-ai/issue/KEL-14/m2-d-observability-structured-json-logs-prometheus-metrics),
  [KEL-15](https://linear.app/kelp-ai/issue/KEL-15/m2-e-eval-harness-make-eval-offline-and-make-eval-live),
  [KEL-16](https://linear.app/kelp-ai/issue/KEL-16/m2-f-nebius-deploy-dockerfile-gpu-runbook-restart-policy)

### Docs and code

- Mind map image: [`docs/sdlc-mindmap.svg`](sdlc-mindmap.svg), PNG fallback `docs/sdlc-mindmap.png`
- Mind map source: [`docs/sdlc-mindmap.gv`](sdlc-mindmap.gv)
- Tracker site: [`netlify/index.html`](../netlify/index.html)
- Skill contract: [`docs/SKILL_SPEC.md`](SKILL_SPEC.md)
- Kernel design: [`docs/KERNEL.md`](https://github.com/Anshieee/kelp/blob/main/docs/KERNEL.md) (main)
- Approval gate design: [`docs/OPENSHELL.md`](https://github.com/Anshieee/kelp/blob/main/docs/OPENSHELL.md) (main)
- Chat surface spec: [`docs/CHAT_SPEC.md`](https://github.com/Anshieee/kelp/blob/main/docs/CHAT_SPEC.md) (main, review fixes at `56b0785`)
- Deploy runbook: [`docs/DEPLOY.md`](DEPLOY.md)
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
4. Deployment is separate from the written record: the tracker site is built from `netlify/` and
   published by Netlify, so a change to `netlify/index.html` only shows up live after a deploy.

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

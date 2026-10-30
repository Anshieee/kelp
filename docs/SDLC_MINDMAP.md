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

What this snapshot rests on: `main` is at `bbff376`, all **1045 tests pass**, and the repository is
public. KEL-5 through KEL-10 are merged, reviewed, and done; KEL-11, KEL-12, KEL-13, KEL-14, KEL-15,
and KEL-16 are merged and reviewed too, and close M2. KEL-17, KEL-18, KEL-20, KEL-21, KEL-19, and
KEL-22 then carried M3 to completion and are merged and reviewed: abuse resistance, spend ceilings,
invoke-time policy, retention, owner sign-in, and the written privacy review. KEL-24 then opened M4
by making those owner sessions durable devices, and KEL-23, KEL-25, and KEL-26 followed it with
scheduled work, installable plugins, and the written sharing decision. KEL-27 and KEL-28 then closed
that stage with proactive turns and the read-only share link, so **M4 Full vision is complete**.
KEL-10, payments,
carries both its paths: the Stripe saved-card scaffold, the Razorpay gateway, and the Razorpay
hosted Checkout.

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

KEL-17 and KEL-18 opened **M3 Hardening** on 30 October 2026, after M2 closed. KEL-17 is abuse
resistance: five rate-limiter classes with their own budgets, a request body ceiling checked before a
byte is read, and two concurrency caps in front of the expensive work. It merged at `b48e113` from
`kel-17` (head `1e8d378`) and added 15 tests, taking the suite from 734 to **749**. KEL-18 is spend
guardrails: day and month dollar ceilings computed from the persisted usage records, exactly one
warning per window at 80%, and a hard stop at 100% that refuses before any provider call. It merged
at `8884a5d` from `kel-18` (head `544a545`) and added 26 tests, taking the suite from 749 to **775**.

KEL-20 then made the skill contract enforceable, merged on 30 October 2026. It is invoke-time policy:
every call re-checks the concrete arguments against the effects, paths, hosts, and repos the manifest
declared, all skill network access goes through one egress door, a cooperative socket guard refuses
direct connections from a handler, a policy refusal is terminal with no approval path, and each call
writes one content-free `skill_invoked` record. It merged at `223964c` from `kel-20` (head `1f9423e`)
and added 52 tests, taking the suite from 775 to **827**.

KEL-21 then closed the retention gap, merged on 30 October 2026. It is hard delete plus a stated
clock: deleting a thread purges its rows in one transaction and unlinks its files, an orphan sweep
removes what a crash left behind, `VACUUM` returns the pages, and audit and usage records rotate by
size, age, and segment count against one table in `chat/retention.py`. It merged at `25584f8` from
`kel-21` (head `4f96577`) and added 18 tests, taking the suite from 827 to **845**.

KEL-19 and KEL-22 then closed M3, both merged on 30 October 2026. KEL-19 is owner sign-in: the root
bearer is exchanged once for an opaque server-side session whose id lives only in an `HttpOnly`,
`Secure`, `SameSite=Lax` cookie, every browser mutation needs a matching `Origin` and a double-submit
CSRF token, every rejected credential writes one `auth_failed` record with a hashed identity, and
repeated failures lock that identity out. It merged at `b5d7196` from `kel-19` (head `5fdc1f8`) and
added 17 tests, taking the suite from 845 to **862**. KEL-22 is the written privacy review:
`docs/PRIVACY.md` names every outbound recipient and what each receives, `openshell/egress_ledger.py`
writes one content-free `egress` row per outbound call into the existing audit trail, and the exec
boundary is settled by removing the promise rather than wiring a gate. It merged at `f2fa77f` from
`kel-22` (head `824522b`) and added 34 tests, taking the suite from 862 to **896**. With that every
work item in M3 is done and the milestone is complete.

KEL-24 opened **M4 Full vision** on 30 October 2026. It is device sessions: the owner sessions that
KEL-19 kept in process memory now persist in the `sessions` table of the chat database, so several
devices survive a restart, the owner can list them, and one can be revoked without signing the rest
out. It merged at `a9814ff` from `kel-24` (head `bc009cd`) and added 6 tests, taking the suite from
896 to **902**.

KEL-23, KEL-25, and KEL-26 then gave M4 four of its six pieces, all merged on 30 October 2026.
KEL-23 is scheduled work: one JSON task file per schedule written atomically under a `0700`
directory, a five-field cron with named rejections for six-field and shortcut forms, next-run times
computed in the task's own timezone with both DST edges resolved, a ticker that fires a due run once
and records anything older than its grace window as `missed`, and one content-free audit row per
fire. It merged at `13fc687` from `kel-23` (head `4dd78e6`) and added 67 tests, taking the suite from
902 to **969**. KEL-25 is plugin packages: a directory or zip with `manifest.json` and an entry
module, installed by a deploy-time CLI into a plugin root, pinned by a SHA-256 tree digest in
`plugins.lock.json`, loaded by file path without touching `sys.path`, and refused at boot when a pin
or version disagrees. It merged at `08f87e5` from `kel-25` (head `5d639fc`) and added 35 tests,
taking the suite from 969 to **1004**. KEL-26 is the sharing and identity decision: an accepted ADR
that keeps exactly one principal, the owner, and gives everyone else only a per-thread, revocable,
read-only capability link, with approval authority, attribution, rate and spend composition,
retention, revocation, and migration all settled on paper. It merged at `05d7508` from `kel-26`
(head `a1a5b1f`) and changed documentation only, so the suite stayed at **1004** at that merge.
KEL-27 is the proactive turn runner: a due schedule becomes an ordinary turn on the same service,
policy gate, and stream, behind spend, concurrency, and hourly caps plus a per-schedule failure
breaker. It merged at `c62be17` from `kel-27` (head `79dd6f8`) and added 12 tests, taking the suite
from 1004 to **1016**. KEL-28 is the read-only share link the decision specified: a token shown once,
stored as a SHA-256 hash, resolving to one frozen and scrubbed snapshot of one thread, revocable in
one request and swept with its thread. It merged at `dc50925` from `kel-28` (head `98ded52`) and
added 29 tests, taking the suite to **1045**. M4 is complete.

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

**LATER** on the last M4 leaf means outside this round; KEL-23, KEL-24, KEL-25, and KEL-26 are DONE,
KEL-27 and KEL-28 are both DONE, so no work item is left open.

## The map

![Mind map of the delivery: milestones M0 to M4, work items KEL-5 to KEL-26, product surfaces, payments, and design.](sdlc-mindmap.svg)

The same map in words, in case the image does not load:

- **Milestones** — five stages, M0 to M4: M1, M2, and M3 are complete, and M4 is in progress.
- **The work** — twenty-four work items, KEL-5 to KEL-28: all twenty-four are done, and M4 closed
  with KEL-27, proactive turns, and KEL-28, the read-only share link.
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
| **M3 Hardening** | 30 Oct 2026 | COMPLETE | The privacy and cost promises hold under pressure: who sees what, where keys live, caps on traffic, a retention rule, a spend ceiling, and a written privacy review. All six pieces are done: KEL-17, the traffic, body, and concurrency caps; KEL-18, the spend ceilings; KEL-19, owner sign-in with a session cookie, CSRF, and lockout; KEL-20, the policy enforced at every skill call; KEL-21, the retention rule; and KEL-22, the written privacy review with the egress ledger and the exec-boundary decision. |
| **M4 Full vision** | 30 Oct 2026 | COMPLETE | More devices, work the assistant starts on its own, abilities added by others, and sharing. All six pieces are merged and reviewed: KEL-24, owner sessions as listable, revocable devices; KEL-23, durable scheduled tasks with a real cron runtime; KEL-25, digest-pinned plugin packages loaded by path; KEL-26, the written decision that keeps one owner and gives everyone else only a per-thread read-only link; KEL-27, the proactive turn runner that makes a due schedule a real turn behind spend, concurrency, and hourly caps; and KEL-28, the read-only share link, stored as a hash and revocable in one request. |

## The twenty-four work items

KEL-5 feeds KEL-6 and KEL-7. Those two feed KEL-8. KEL-7 also feeds KEL-10. KEL-8 comes before the
demo, KEL-9. The first twelve are done. KEL-10, payments, closed M1 on owner confirmation on 30
October 2026: both of its paths were merged and reviewed earlier, and the live step was taken on the
owner's word. KEL-11 then opened M2 with the serve entrypoint, and KEL-12, KEL-13, KEL-14, KEL-15,
and KEL-16 followed with health checks, real document search, observability, the eval harness, and
the deploy. KEL-17 and KEL-18 opened M3 the same day, with abuse resistance and spend ceilings, and
KEL-20 and KEL-21 followed it with invoke-time policy and retention, then KEL-19 and KEL-22 closed
it with owner sign-in and the written privacy review. KEL-24 then opened M4 by making those owner
sessions durable, listable devices, and KEL-23, KEL-25, and KEL-26 followed with scheduled work,
plugin packages, and the sharing decision. KEL-27 then made a due schedule a real turn, and KEL-28
built the link the decision specified, which completed M4.

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
18 for the streaming client — and passes with the rest of the 1045. The review written up in
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
suite is 86 tests, part of the 1045. The spec is on `main` at 1,164 lines; the review fixes that
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
`RAZORPAY_KEY_SECRET` set in the server environment; the owner confirmed that run, and the live
receipt is now verified: test-mode order `order_TtyLUT5JHr4pdK`, a ₹1 (100 paise) netbanking
payment `pay_TtyOkNxvfHSKFS` captured, confirm returning HTTP 200 `idempotent=false` with receipt
`pi_rzpTtyOkNxvfHSKFS`, an idempotent replay, and a ledger showing exactly one paid proposal and one
provider payment — no code changes were needed. The earlier failed card attempt `pay_TtyO1S0XnBcilq`
(domestic-card-only rejection) was correctly ignored. Nothing in the code is outstanding for it. The
Linear item is Done. An in-app balance is outside this round.

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

### KEL-17 — Abuse resistance — DONE

The public surface refuses to be a free amplifier. Five named limiter classes carry their own
budgets: `auth` for signing in, `chat` for starting a turn and its stream, `upload` for document
ingest, `read` for lists and detail, and `admin` for metrics and readiness. Each is set from
`KELP_RATE_LIMIT_<CLASS>_RPM` and `_BURST`, and charged to the owner session when there is one and
to the client address otherwise — the `auth` class counts failed attempts per peer address only, so a
good credential is never charged — and one client's burst cannot spend another's budget. Over budget
is always `429` with the stable `rate_limited` code and an integer `Retry-After` of at least one
second, so the client's existing rate-limited state renders it with no special case. A request whose
declared `Content-Length` is over the route ceiling is refused before a byte of body is read; a
request that declares both `Content-Length` and `Transfer-Encoding` is refused as ambiguous instead
of trusting one of them; and a chunked body with no length is counted as it arrives and cut off at
the cap. Two concurrency caps sit in front of the expensive work: `KELP_MAX_CONCURRENT_TURNS` for
turns in flight, and `KELP_MAX_STREAMS_PER_OWNER` so one client cannot hold every stream. A slot is
held only while the work is active — a retried turn takes one exactly like a fresh send and gives it
back when it finalizes or fails — and a restart starts both counts at zero.

*Why it matters:* an assistant that calls a paid model is a place where one careless client, or one
script pointed at the wrong port, can cost real money and starve everyone else. Caps make the
failure mode "come back in a second" instead of "the box is out of memory and the bill is a
surprise".

*Plain words:* a **rate limit** is a cap on how many requests arrive in a set time. A **burst** is
how many may arrive at once before the steady rate applies. A **body ceiling** is the largest request
the server will read. A **semaphore** is a counter that lets only so many jobs run at once, and
**concurrency** is how many things are happening at the same moment.

- Linear: https://linear.app/kelp-ai/issue/KEL-17/m3-a-abuse-resistance-rate-limits-body-ceiling-concurrency-caps
- HTTP surface: [`chat/api.py`](https://github.com/Anshieee/kelp/blob/main/chat/api.py)
- Error codes: [`chat/errors.py`](https://github.com/Anshieee/kelp/blob/main/chat/errors.py)
- Config: [`docs/DEPLOY.md`](DEPLOY.md), sections 3.1 and 12.1
- Tests: [`tests/test_rate_limit.py`](https://github.com/Anshieee/kelp/blob/main/tests/test_rate_limit.py)
- Merge: [`b48e113`](https://github.com/Anshieee/kelp/commit/b48e113) (branch
  [`kel-17`](https://github.com/Anshieee/kelp/tree/kel-17), head `1e8d378`)

Status detail: merged into `main` at `b48e113`, adding 15 tests to a suite of 749. The review fixed
two must-fix bugs before the merge. A retried turn queues fresh work, so it now takes a global turn
slot exactly like a send and returns it when the retry finalizes, or immediately if the call raises
or the cap refuses it. And the HTTP adapter refuses a request carrying both `Content-Length` and
`Transfer-Encoding` rather than picking one. Three honest gaps against the written issue. The issue
asks for `503` when the concurrency queue is full; what ships is `429 rate_limited` with
`details.reason` of `turn_capacity` and a `Retry-After`, which keeps one "come back later" shape
instead of two. The issue asks for counters `rate_limited_total{class}` and
`rejected_body_too_large_total`; what ships on `/metrics` is `kelp_limiter_in_use`,
`kelp_limiter_capacity`, and `kelp_limiter_saturation` per class, so current pressure is visible but
the count of refusals over time is not. And a body over the ceiling on a JSON route reports
`400 invalid_request`, matching the existing JSON body path, while only uploads report the named
`413 upload_too_large`. Limiting is per process: that is the single-node design the issue scoped, and
distributed limiting across hosts stays out of scope.

### KEL-18 — Spend guardrails — DONE

Two ceilings in dollars, `KELP_SPEND_CAP_DAY_USD` and `KELP_SPEND_CAP_MONTH_USD`, both enforced from
the persisted usage records. Every check re-reads `usage.jsonl` for the current window — the UTC
calendar day, or the UTC calendar month — so the figure is a query over what was actually spent and a
restart never resets a cap. Crossing 80% of a ceiling produces exactly one `spend_cap_warning` log
line and audit entry per window, deduped by a marker in a small sidecar file, so a restart inside the
same window does not warn again and a busy hour does not warn a hundred times. At 100% the next
model-bound request is refused with `402` and the stable `spend_cap_reached` code, naming the window,
the reason, and when it resets, before the turn row, its messages, or any provider call exist.
Read-only and purely local paths keep working. A model with no price is never silently free: `block`
is the default and refuses new turns while an unpriced record sits in a capped window, `count_as`
charges `KELP_SPEND_UNKNOWN_PRICE_USD` instead, a torn usage line counts as unpriced rather than
free, and an unreadable usage file fails closed as `usage_unavailable`. A malformed cap value fails
startup with `invalid_config` rather than quietly meaning "unlimited".

*Why it matters:* a cost ceiling that is a cached counter drifts, and one that is checked after the
provider call has already spent the money. Reading the real records and stopping before the call is
what makes the ceiling a promise instead of a dashboard.

*Plain words:* a **ceiling** is the most you are willing to spend in a period. A **window** is that
period — here a UTC day or a UTC month. **Fail closed** means refusing to work when the answer is
unknown, rather than guessing in the expensive direction. An **unpriced model** is one with no entry
in the price list, so its cost cannot be computed.

- Linear: https://linear.app/kelp-ai/issue/KEL-18/m3-c-spend-guardrails-dailymonthly-usd-ceilings-with-a-hard-stop
- Guard: [`kernel/usage.py`](https://github.com/Anshieee/kelp/blob/main/kernel/usage.py)
- Gating path: [`chat/service.py`](https://github.com/Anshieee/kelp/blob/main/chat/service.py)
- Metrics: [`chat/metrics.py`](https://github.com/Anshieee/kelp/blob/main/chat/metrics.py)
- Runbook, caps and the provider-side checklist: [`docs/DEPLOY.md`](DEPLOY.md), sections 13.1 and 13.2
- Tests: [`tests/test_spend_caps.py`](https://github.com/Anshieee/kelp/blob/main/tests/test_spend_caps.py)
- Merge: [`8884a5d`](https://github.com/Anshieee/kelp/commit/8884a5d) (branch
  [`kel-18`](https://github.com/Anshieee/kelp/tree/kel-18), head `544a545`)

Status detail: merged into `main` at `8884a5d`, adding 26 tests to a suite of 775, including the
boundary at exactly the ceiling and the reset into a new window. The review fixed one must-fix bug
before the merge: the day and month gauges were only written when a turn was gated, so a scrape right
after a turn showed the pre-turn total until the next send; they are now republished from the
persisted records when a turn settles, and refreshed on each scrape. Two honest gaps. The issue asks
`/readyz` to list any unpriced model in use; what ships is a readiness check on the usage path, with
the unpriced count on `/metrics` as `spend_unknown_price_records` and the mode in the 402 details, so
an operator sees the fact on a scrape but not on the readiness answer. And the issue asks for a
written procedure for adding a provider or model; the runbook's section 13.2 is a deploy checklist
that names the price entry and the provider-side cap, which is a deploy step rather than a general
recipe for a new provider.

### KEL-20 — Skill policy — DONE

A manifest is only a control if it is checked on every call. After the arguments pass their schema
and before any approval is offered, `skills/policy.py` compares the concrete arguments with what the
skill declared, and the manifest's scope is a ceiling that a narrower per-call policy may tighten but
never widen. Arguments are typed by the skill, not guessed from their name: a property whose
`input_schema` declares `kelp-path`, `kelp-write-path`, `kelp-host`, or `kelp-repo` is a scope
target, and a plain `path`, `url`, or `repo` key without a format is opaque data. A path is resolved
through its symlinks and must stay inside `${WORKSPACE}` and a declared prefix; a host must be an
exact declared name or a `*.` wildcard, with embedded credentials, IP literals, and ports other than
80 and 443 refused; a repo must be an exact `owner/name` unless the manifest says `*`. A typed
target with no declared scope is refused, and an unknown `kelp-*` format is a manifest error at load.

Network access has one door. A handler reaches the network only through `context.egress()`, which
checks host **and** method — a non-GET method needs the `write` effect — follows no redirects, and
caps the response body. A ContextVar-scoped socket guard then refuses direct `socket` or `urllib`
connections for the length of the call: address lookup, connection, datagram sends, and the thread
and executor starts a handler makes, so a handler that ignores the door fails closed. The outcomes
are `allow`, `require_approval`, and `deny`. Only a manifest-level `requires_approval: true` can
produce an approval prompt, and a `deny` is terminal, so the approval hook is never called for it.
Each invocation appends one content-free `skill_invoked` record — skill name and version, effects,
scope decision, outcome, duration, and a hash of the approval arguments when there is one — and an
output that fails its schema is audited as `invalid_output` rather than returned as success.

*Why it matters:* a manifest that is only read at registration is a label, not a control. A skill
that says it reads one folder but is handed a path somewhere else, or a host that was never on its
list, is the ordinary way a plugin becomes a hole in the wall. Checking the real arguments on every
call, and treating a refusal as final, is what makes the declared scope mean something.

*Plain words:* an **effect** is the kind of thing an action does — read, write, use the network, or
run something. A **scope** is where it may do it. **Fail closed** means refusing when a check cannot
be completed. A **terminal refusal** is one you cannot click past.

- Linear: https://linear.app/kelp-ai/issue/KEL-20/m3-d-enforce-declared-effects-and-scopes-at-invoke-time
- Policy, egress door, and socket guard: [`skills/policy.py`](https://github.com/Anshieee/kelp/blob/main/skills/policy.py)
- Typed arguments and format errors: [`skills/loader.py`](https://github.com/Anshieee/kelp/blob/main/skills/loader.py)
- Spec: [`docs/SKILL_SPEC.md`](SKILL_SPEC.md), section 8.5
- Tests: [`tests/test_policy_enforcement.py`](https://github.com/Anshieee/kelp/blob/main/tests/test_policy_enforcement.py)
- Merge: [`223964c`](https://github.com/Anshieee/kelp/commit/223964c) (branch
  [`kel-20`](https://github.com/Anshieee/kelp/tree/kel-20), head `1f9423e`)

Status detail: merged into `main` at `223964c`, adding 52 tests to a suite of 827. A denied host is
audited as a refused egress attempt, not only a successful one. The review closed two blockers
before the merge: the socket guard now covers the datagram and name-lookup entry points
(`sendto`, `sendmsg`, `gethostbyname`, `gethostbyname_ex`, `gethostbyaddr`) and follows a handler
into the threads and executor tasks it starts, and `ChatService` forwards the audit log and turn id
to the registry executor, so the production chat path writes `skill_invoked` with its turn id rather
than only a test doing it. It also replaced the argument-name guess with the per-property `format`
values above and made a typed target with no declared scope fail closed. One honest limit: the guard
is cooperative. A raw `_socket` object, a child process, a C extension that calls `connect` directly,
or a thread that was already running before the call and receives work through a queue rather than
`submit` is outside its reach, and has to be stopped at the deployment boundary. That boundary is
written down in the spec, and the tests prove the guarded entry points fail closed.

### KEL-21 — Retention — DONE

Deleting a thread is a purge, not a flag. `DELETE /api/threads/{id}` removes, in one transaction, the
thread row and everything keyed to it: messages, their blocks, the event log, turns, approvals, and
artifact registry rows, with the counts in the reply. A second delete removes nothing and answers
`404 unknown_thread`, which the client shows as "already gone", so a retry after a lost response
cannot loop. Files cannot join a database transaction, so their metadata is captured in the
transaction and the bytes are unlinked, with the `docs.answer` index dropped, immediately after the
commit; a file-level failure is reported and left to the sweep to retry. `VACUUM` and a WAL
checkpoint then return the freed pages to disk. A running turn is stopped first, and a turn that is
still live makes the call answer `409 turn_in_progress` with the active turn id and delete nothing;
that check is repeated inside the transaction, so a turn that started in between cannot write to
rows that no longer exist.

Everything with no live owner is swept. A boot-time pass and a daily pass find artifact rows with no
parent, along with their files and index directories, past a one-hour grace, and report the counts;
the rows are selected and deleted under one predicate in one transaction, so a row claimed in
between survives and the counts match what was removed. A handle a live turn references is skipped.
The same pass rotates the append-only records: the active audit and usage files are renamed to
timestamped segments rather than rewritten, so a line already being appended lands inside its
segment instead of being dropped, and a segment past its age or count cap is removed whole. Expiry
reads the oldest record time in a segment, not the file's modification time, because an in-flight
append can refresh the timestamp of a segment about to be pruned. Every reader, the spend guard
included, reads the retained segments too, so rotation cannot quietly reset a day or month spend
window. The lifetimes live in one place, `chat/retention.py`, and are mirrored in the runbook: audit
records 30 days and 40 segments, usage records 90 days and 100 segments, rotation at 24 hours or
10 MiB, artifacts after a one-hour orphan grace.

*Why it matters:* "deleted" that leaves the text sitting in the file is not a privacy promise, it is
a hidden copy. The same is true of a disk that keeps growing because nothing removes what a crash
left behind. This makes the two claims the product depends on checkable: the owner's delete means
the content is unreachable by every read path, and every other record has a stated lifetime that
something actually enforces.

*Plain words:* a **hard delete** removes the content itself, where a soft delete only hides it. An
**orphan** is a leftover whose parent is gone, such as a file whose note was removed mid-way.
**Vacuum** is the tidy-up that gives freed disk space back to the system. **Rotation** closes a full
log file and starts a new one so no single file grows forever.

- Linear: https://linear.app/kelp-ai/issue/KEL-21/m3-b-retention-purge-hard-delete-orphan-sweep-log-rotation
- Lifetimes, rotation, boot and daily pass: [`chat/retention.py`](https://github.com/Anshieee/kelp/blob/main/chat/retention.py)
- Purge transaction, live-turn check, orphan sweep: [`chat/store.py`](https://github.com/Anshieee/kelp/blob/main/chat/store.py)
- Runbook retention table: [`docs/DEPLOY.md`](DEPLOY.md), section 8.1
- Spec: [`docs/CHAT_SPEC.md`](CHAT_SPEC.md), sections 10.5 and 11
- Tests: [`tests/test_retention.py`](https://github.com/Anshieee/kelp/blob/main/tests/test_retention.py)
- Merge: [`25584f8`](https://github.com/Anshieee/kelp/commit/25584f8) (branch
  [`kel-21`](https://github.com/Anshieee/kelp/tree/kel-21), head `4f96577`)

Status detail: merged into `main` at `25584f8`, adding 18 tests to a suite of 845. The review closed
five findings before the merge: the orphan sweep selects and deletes under one predicate in one
transaction; the purge refuses with `409` while a turn is live and settles a turn that has no task,
so a dead turn cannot make its thread undeletable; the chat spec documents the hard-purge shape and
the unused `deleted_at` column; and the request-log row now states the size bound Compose actually
enforces, 10 MiB across three files with no age limit. Two honest limits. Chat content has no
age-based expiry: it is removed when the owner deletes the thread, and the table says so rather than
pretending a timer exists. And the request logs are bounded by size, not by age, because the
container log driver is configured that way; a day-based claim would need a change in both the
Compose file and the runbook.

### KEL-19 — Owner sign-in — DONE

The root bearer token is a server-side secret and a recovery credential, not a browser credential.
`POST /api/auth/session` exchanges it once for an opaque session id: the raw id exists only in the
client's cookie, and the server stores its SHA-256, so a copy of the running state is not a set of
working sessions. The `session` cookie is `Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=2592000`
and rolls forward 30 days on each authenticated response. A readable `csrf` cookie carries the second
half: a cookie-authenticated `POST`, `PATCH`, `PUT`, or `DELETE` must send an `Origin` that matches
`Host` and an `X-CSRF-Token` equal to that cookie, and a request with no `Origin` is refused even when
the pair matches, which closes the non-browser cross-site shape a bare token check accepts. Sessions
persist in the `sessions` table of the chat database, so several devices survive a restart, and are
bounded by `KELP_SESSION_MAX`, with the oldest idle one evicted when the cap is reached;
`GET /api/auth/devices` lists them, `DELETE /api/auth/devices/{id_short}` revokes one by its short
id, `DELETE /api/auth/session` revokes the presented session, and `DELETE /api/auth/sessions`
revokes every session and is the bearer path used before a credential rotation.

Failures are counted, not just rejected. Every rejected credential writes exactly one structured
`auth_failed` record carrying the outcome (`rejected` or `locked`), the reason, the count, the status,
and a truncated SHA-256 of the identity; the token, the cookie, and the peer address never appear in
the trail. The counter map is bounded by `KELP_AUTH_FAILURE_MAX_KEYS` and idles out, and after
`KELP_AUTH_LOCKOUT_ATTEMPTS` failures that identity gets `429` with a `Retry-After` for
`KELP_AUTH_LOCKOUT_SECONDS`. `KELP_SESSION_COOKIE_SECURE=0` exists only for a loopback HTTP
development run; production keeps `Secure` on.

*Why it matters:* one shared token pasted into a browser is a credential any script on the page can
read and nothing can revoke without breaking every device. A short-lived server-side session, a
second proof on every change, and a lockout turn "only the owner is in here" from a claim into
something an operator can check and revoke.

*Plain words:* a **session** is a temporary pass the server can cancel, where the long-lived token is
the master key. **HttpOnly** means page scripts cannot read the cookie. **CSRF** is a forged request
that borrows your cookie, and a second token plus a matching `Origin` is how the server tells a real
form from a forged one. A **lockout** is a cool-off after too many wrong tries.

- Linear: https://linear.app/kelp-ai/issue/KEL-19/m3-e-owner-auth-session-cookie-csrf-auth-audit-trail
- Sessions, CSRF, and the failure counter: [`chat/api.py`](https://github.com/Anshieee/kelp/blob/main/chat/api.py)
- Routes, cookie rules, and the auth record: [`docs/CHAT_SPEC.md`](CHAT_SPEC.md), section 12.2
- Sign in, revoke, and rotate: [`docs/DEPLOY.md`](DEPLOY.md), section 11.1
- Tests: [`tests/test_auth_sessions.py`](https://github.com/Anshieee/kelp/blob/main/tests/test_auth_sessions.py)
- Merge: [`b5d7196`](https://github.com/Anshieee/kelp/commit/b5d7196) (branch
  [`kel-19`](https://github.com/Anshieee/kelp/tree/kel-19), head `5fdc1f8`)

Status detail: merged into `main` at `b5d7196`, adding 17 tests to a suite of 845, taking it to 862.
The review fixed two things before the merge. Rate and stream limits are keyed on the single owner
identity rather than the presented credential, so the root bearer and every session minted from it
share one route bucket and one per-owner stream cap; previously a second tab minted its own key and
could double the allowance, while with auth disabled the peer address stays the key so distinct
callers remain independent. And a failed `POST /api/auth/session` now goes through the same rejection
path as any other bad credential, so a wrong bearer is logged, counted, and can be locked out even
when a valid session cookie admitted the request; the cookie that opened the wrapper no longer clears
the debt of the credential actually presented. Two honest limits, one of them revised by KEL-24.
Sessions now persist in the `sessions` table, so a restart keeps every signed-in device valid and the
root bearer stays the recovery path; what a restart cannot outlive is the server secret, and each row
holds only a hash, never a session id or CSRF token. And sign-in is a bearer exchange rather than a
password form, so the root token remains the thing to protect, rotate, and keep out of tickets.

### KEL-22 — Privacy review — DONE

`docs/PRIVACY.md` is the written position the owner can audit. It names every outbound recipient —
Token Factory for models, Tavily for search, GitHub for reads, Stripe and Razorpay for money, plus an
`unlisted` bucket for an operator-supplied base URL — and for each one states what it receives, what
it does not, and why; it then lists where each data class sits on the machine and points at the
retention table in the runbook instead of restating it, so there is one clock and not two.

Beside the document, `openshell/egress_ledger.py` writes one `egress` row per outbound call:
recipient, purpose, request and response byte counts, and outcome. The API has no field that can hold
a body, query, path, header, file name, or prompt; the recipient must be a known id; the purpose must
be a short lowercase token; and a host that is not a domain name or IP literal is stored as
`unresolved`, so a URL carrying a path or query cannot smuggle text into the trail. Rows are written
through the existing audit log, which means they inherit its `0600` mode, mandatory scrubbing,
rotation, and the 30-day audit retention row rather than adding a second file and a second clock. The
ledger is wired at the three real egress paths: the provider client, the skill egress chokepoint, and
the readiness probe. A denial or a transport error writes a zero-response row, so a refused attempt
stays visible without pretending traffic happened.

The exec boundary is settled by removing the promise rather than wiring a gate. `build_app` never
constructs an `ExecGate`, and `shell.exec` is not a model-facing tool, so an exec card that said "It
runs once" was describing a run that never happens. `cards.EXEC_AVAILABLE` now drives that copy, and
a test fails if the flag is flipped without the wiring, so the claim cannot drift back.

*Why it matters:* "it is private" is an assertion until someone can list the recipients and check
them against the code. The ledger answers the other half of the question — for every byte that did
leave, who received it and how much — without becoming a second copy of the content it describes. A
review that only states intentions is worse than none, because it reads like evidence.

*Plain words:* **egress** is data leaving the machine. A **ledger** here is a list of what left and to
whom, kept as counts so it cannot leak the text itself. An **exec boundary** is the line between the
assistant suggesting a command and the machine running one.

- Linear: https://linear.app/kelp-ai/issue/KEL-22/m3-f-privacy-review-privacymd-egress-ledger-exec-boundary-decision
- Review: [`docs/PRIVACY.md`](PRIVACY.md)
- Ledger: [`openshell/egress_ledger.py`](../openshell/egress_ledger.py)
- Provider calls and the readiness probe: [`kernel/provider.py`](../kernel/provider.py)
- Tests: [`tests/test_privacy.py`](https://github.com/Anshieee/kelp/blob/main/tests/test_privacy.py)
- Merge: [`f2fa77f`](https://github.com/Anshieee/kelp/commit/f2fa77f) (branch
  [`kel-22`](https://github.com/Anshieee/kelp/tree/kel-22), head `824522b`)

Status detail: merged into `main` at `f2fa77f`, adding 34 tests to a suite of 862, taking it to 896.
The review settled five findings before the merge. The ledger validates a stored host against a
hostname and IP grammar and records `unresolved` for anything else, covering the host-writing path as
well as the main one. A non-2xx readiness probe is recorded as `provider_error` with its HTTP status
instead of counting as a completed call. The exec invariant is checked by parsing the app builder with
`ast` and by running it with `ExecGate.__init__` booby-trapped, because a comment mentioning the gate
had been enough to pass the earlier check. The completeness test globs every module under `chat/`,
`kernel/`, `skills/`, `openshell/`, and `payments/` instead of a hand-kept file list, and a new test
binds the recipient table in the review to `RECIPIENTS` in the code in both directions. And the review
no longer claims every ledger row carries a status and a turn id; it states which call sites supply
them, because absent beats invented. One honest limit: the ledger counts the calls the code makes
through its own doors, and the socket guard from KEL-20 is cooperative, so a call made from outside
the process or from a child process is stopped at the deployment boundary, not here.

### KEL-24 — Device sessions — DONE

KEL-19 kept owner sessions in an in-process map, so a restart signed every browser out and the owner
could not see or kill a device that was not in front of them. KEL-24 moves them into the `sessions`
table of the chat database: `id_hash` (the SHA-256 of the session id) as the primary key, plus a
short label, `created_at`, `last_seen`, `expires_at`, and `revoked_at`. Two routes sit beside the
existing sign-in: `GET /api/auth/devices` returns each device's short id, label, timestamps, and an
`online` and `current` flag, and `DELETE /api/auth/devices/{id_short}` revokes one. The route names
follow the existing `/api/auth/*` family rather than the `/api/sessions/…` sketch in the ticket.

No credential is written down. The CSRF half is
`HMAC-SHA256(SHA-256(KELP_SESSION_SECRET), "kelp-csrf:" + id_hash)` in unpadded base64url, derived on
demand and never stored; the signing key is itself a digest, so the secret is not the key, and the DB
holds no value that can be replayed as a cookie or a mutation token. With `KELP_SESSION_SECRET`
unset the root `KELP_AUTH_TOKEN` is the fallback, which keeps every live device's pair valid across a
restart without a second secret. A device's public id is the first 12 hex characters of the hash —
48 bits, enough to be unique and useless for replaying a login. Labels are scrubbed of control,
zero-width, and bidi characters, whitespace is collapsed, the result is capped at 40 characters and
defaults to `Unknown device`, and no log or audit line carries a label. Presence is derived, not a
heartbeat: a device reads `online` when its last authenticated request is within 60 seconds, so it
falls offline by itself.

Revocation is a fresh row read on every check, so a revoked device's next request is `401` and its
open stream ends at the next auth check rather than at process exit. The session cap still evicts the
oldest idle device, now across restarts, and expired or long-revoked rows are pruned on use, so the
table stays bounded.

*Why it matters:* a session you cannot see is a session you cannot end. Persisting it with a hash and
a derived second proof turns "only the owner is signed in" into a list the owner can read and act on,
without putting a working credential in the database, a log, or a backup.

*Plain words:* a **device** is one signed-in browser. **Presence** is a guess from recent activity,
not a live connection. **Revoking** means the pass stops working now, including for a page that is
already open.

- Linear: https://linear.app/kelp-ai/issue/KEL-24/m4-c-device-sessions-durable-listable-revocable-presence
- Table and queries: [`chat/store.py`](../chat/store.py)
- Session store, devices, revocation: [`chat/api.py`](../chat/api.py)
- Spec: [`docs/CHAT_SPEC.md`](https://github.com/Anshieee/kelp/blob/main/docs/CHAT_SPEC.md) §12.2
- Runbook: [`docs/DEPLOY.md`](DEPLOY.md)
- Tests: [`tests/test_devices.py`](https://github.com/Anshieee/kelp/blob/main/tests/test_devices.py)
- Merge: [`a9814ff`](https://github.com/Anshieee/kelp/commit/a9814ff) (branch
  [`kel-24`](https://github.com/Anshieee/kelp/tree/kel-24), head `bc009cd`)

Status detail: merged into `main` at `a9814ff`, adding 6 tests to a suite of 896, taking it to 902.
The six cover two devices surviving a restart with a valid CSRF pair, the `current` flag marking only
the calling cookie, one device's revocation closing its live stream while the other keeps working, a
byte-level grep of the database for the session id and the CSRF token, cap eviction across restarts,
and a label that is scrubbed, defaulted, and absent from the logs. Three honest limits. Mint and
revoke do not write their own audit rows yet, so the existing `auth_failed` trail still covers
rejections only, and a successful sign-in or revocation is not in the trail. There is no "revoke the
others but keep me" call: `DELETE /api/auth/sessions` is the global reset and signs the caller out
too. And the coarse user-agent class and revocation reason the ticket describes are not stored; a
device is its label, timestamps, and hash.


### KEL-23 — Scheduled work — DONE

KEL-5 shipped a `schedule_create` skill that could write down a task; KEL-23 gives it somewhere real
to live and something to run it. Each task is one JSON file under
`${WORKSPACE}/var/scheduler/<task_id>.json`, written as a temporary file in the same directory
followed by `fsync`, a rename, and a directory `fsync`, so a reader sees the old document or the
complete new one and never a partial line. Files are `0600` inside a `0700` directory, at most 200
tasks exist by default (`KELP_SCHEDULER_MAX_TASKS`), and titles and prompts are capped at 120 and
2000 characters. A file that fails JSON, schema, cron, or timezone validation is renamed `.bad` at
the next scan while the other tasks still load, and a file whose `task_id` differs from its own name
is quarantined the same way, so a stray copy can never fire in place of the canonical task.

The clock is the standard five fields — minute, hour, day-of-month, month, day-of-week — with `*`,
integers, ranges, lists, steps, and three-letter month and weekday names. Six-field (seconds)
expressions, `@daily`-style shortcuts, and Quartz modifiers (`L`, `W`, `#`, `?`) are rejected with a
named `CronError` rather than silently reinterpreted. `next_run_at` is a pure function of the
expression, the task's `zoneinfo` timezone, and "after now", stored as a UTC instant, and is used by
both the store and the ticker. On the spring-forward night a wall time inside the gap fires once at
the first valid instant after it; on the fall-back night a wall time that occurs twice fires once,
on the first occurrence.

`chat.scheduler.SchedulerRuntime` owns one daemon tick and an advisory `flock` on a lock file in the
task directory, so a second ticker on the same directory reports a skipped tick instead of firing
the due set twice. A run up to an hour late (configurable) fires once, and its next run is placed in
the future, so a long outage never becomes a burst. A run older than that, or one whose `end_at`
window has already closed, is recorded `missed`; in the second case the task becomes `completed`.
State is persisted before the callback runs, which is what makes a restart mid-run idempotent, and
`KELP_SCHEDULER_ENABLED=0` makes both `start` and `tick` a no-op. The audit trail gets one
content-free `schedule_created` row per task and one `schedule_run` row per fire, missed window, or
expired window; the title and prompt never enter it. A direct call with no server trail writes a
local `var/scheduler/audit.jsonl`, which is a convenience sink inside the declared write scope, not a
second retention row.

*Why it matters:* "later" is only a promise if it survives a restart, and a schedule you cannot
audit is one you cannot trust. When a task does not run the owner needs to know whether the box was
down, the window had passed, or the task was disabled — and a long outage must not become a burst of
model calls and charges the moment the process returns.

*Plain words:* a **cron** is the old five-column way of saying "when": minute, hour, day of the
month, month, and day of the week. A **timezone** matters because 09:00 is a different instant in
two cities, and twice a year the clock jumps. A **missed** window is one that went by while nothing
was running; it is written down and skipped, not fired late in a pile.

- Linear: https://linear.app/kelp-ai/issue/KEL-23/m4-a-scheduler-store-cron-runtime
- Store, cron, and ticker: [`chat/scheduler.py`](../chat/scheduler.py)
- The skill that writes a task: [`skills/builtin/schedule_create/handler.py`](../skills/builtin/schedule_create/handler.py)
- Spec: [`docs/SKILL_SPEC.md`](SKILL_SPEC.md) §12
- Tests: [`tests/test_scheduler.py`](../tests/test_scheduler.py)
- Merge: [`13fc687`](https://github.com/Anshieee/kelp/commit/13fc687) (branch
  [`kel-23`](https://github.com/Anshieee/kelp/tree/kel-23), head `4dd78e6`)

Status detail: merged into `main` at `13fc687`, adding 67 tests to a suite of 902, taking it to 969.
One honest limit: the lock is an advisory `flock` on one directory, which is right for one process
on one box and is not a distributed scheduler; a hard kill inside a callback leaves a started run
with no finish. The runtime is now started by `chat/server.py` after boot recovery and stopped
before the store closes (KEL-27), so a stored task is also a task something fires.

### KEL-25 — Plugins — DONE

A plugin package is a directory or `.zip` holding `manifest.json` and the module its `entry.path`
names; `manifest_version` stays `1.0` and `entry.path` is an additive optional field. An operator
installs it with `python -m skills.cli install <path|zip>` into `KELP_PLUGIN_ROOT` (default
`var/plugins`), and the command validates the package, copies it to a staging path under the root,
re-checks the staged digest, swaps it into place, and pins the name, version, and digest in
`plugins.lock.json` with an atomic sorted write. `list` reports version, digest, and whether the pin
still matches, where a pin only counts when the digest and the manifest version both agree;
`uninstall` removes the package and its pin. The CLI is a deploy-time operator tool: it is not
registered as a skill and cannot be reached from chat or the model.

The digest is SHA-256 over the sorted file set, framing each entry as its relative path, a NUL, four
octal permission digits, a NUL, and the SHA-256 of its bytes, so every path, mode, and content byte
is covered. `__pycache__` and `*.pyc`/`*.pyo` are excluded from the digest and are also never
imported: install and boot each write a private tree from the digest-covered set and import from
that copy, so a planted cache that matches a source file's timestamp and size cannot execute. At
boot, installed packages are discovered after built-ins in sorted order, and the loader imports the
entry module by file path — never by bare name and never by changing `sys.path`.

A package over 8 MiB or with more than 4096 entries is refused, as is any archive with `..`
segments, absolute paths, backslashes, symlinks, duplicate member names, or member names outside
`A-Z a-z 0-9 . _ -`, all before any plugin code runs. A plugin may not shadow a built-in: boot fails
naming both the built-in and the package origin, and the installer refuses it up front. Manifest
validation is the same schema, semantics, and invocation pipeline as a built-in, the `execute`
effect stays forbidden with no plugin exception, and unpinned packages are refused unless
`KELP_PLUGINS_ALLOW_UNPINNED=1`, which is documented as a local experiment only.

*Why it matters:* "it can use abilities other people wrote" is the difference between one assistant
and a platform, and it is also the easiest way to lose the privacy claim. A digest pin makes the
installed code the code that was reviewed, loading by path keeps an unrelated file on disk from
being imported instead, and refusing to boot on a mismatch turns a silent swap into a loud failure.

*Plain words:* a **plugin** is an ability written by someone else and dropped in without rebuilding
the assistant. A **digest** is a fingerprint of the package's bytes: change anything and the
fingerprint changes. A **pin** is that fingerprint written down, so the next boot can tell whether
the package is still the one that was installed.

- Linear: https://linear.app/kelp-ai/issue/KEL-25/m4-d-plugin-packages-install-digest-pin-load-by-path
- Installer: [`skills/cli.py`](../skills/cli.py)
- Loader, digest, and pins: [`skills/loader.py`](../skills/loader.py)
- Schema: [`skills/manifest.schema.json`](../skills/manifest.schema.json)
- Runbook: [`docs/DEPLOY.md`](DEPLOY.md) §3.2
- Spec: [`docs/SKILL_SPEC.md`](SKILL_SPEC.md) §15.1
- Tests: [`tests/test_plugins.py`](../tests/test_plugins.py)
- Merge: [`08f87e5`](https://github.com/Anshieee/kelp/commit/08f87e5) (branch
  [`kel-25`](https://github.com/Anshieee/kelp/tree/kel-25), head `5d639fc`)

Status detail: merged into `main` at `08f87e5`, adding 35 tests to a suite of 969, taking it to
1004. The tests cover install, list, and invoke envelopes, a digest mismatch, zip traversal,
absolute and symlink members, the size caps, built-in shadowing, uninstall, the private-copy rule,
and the no-`sys.path`-mutation guarantee. Three honest limits. A digest pin is integrity, not a
sandbox: plugin code runs in the assistant's own process with the privileges of a built-in. There is
no signing, trusted publishing, or remote registry yet, so the pin proves "unchanged since install",
not "written by whom". And packages are read at boot only, so installing or removing one requires a
restart, and the plugin root must be writable — inside the container, a path under `/data`.

### KEL-26 — Sharing and identity decision — DONE

Sharing is the most expensive thing in M4 to undo, so the decision is written before any share code
exists. `docs/adr/0002-sharing-and-identity.md` is an accepted ADR that answers one question for
every principal — owner, collaborator, anonymous viewer, scheduled actor, plugin: how it
authenticates, what it can read, what it can do, whose credentials a delegated action uses, whose
spend it bills, who can revoke it, and what the audit says about it. The answer is deliberately
small. There is no workspace, no membership, no invitation, no role, and no second credential
family; the owner stays the only principal, and everyone else gets only a per-thread, revocable,
read-only capability link that shows one frozen snapshot.

The ADR fixes what would be expensive to change later: a collaborator can never approve a spend or a
write; audit rows carry the actor and an "on behalf of" for shared and agent-originated actions; the
M3 rate classes and spend ceilings stay machine-wide with the owner's wallet billed; revoking a
principal stops the next request and deliberately cannot recall a copy already read; deleting a
thread takes its links with it in the same transaction; and every owner-less table has a stated
additive migration path if named collaborators are ever admitted. It also specifies the smallest
shippable slice, KEL-28, with its acceptance tests, and records the two options it rejected —
workspace membership and full multi-tenancy — with the trigger that would reopen the first. The
review round before the merge fixed the token path, the attribution wording, and the retention
class in both the ADR and `docs/PRIVACY.md`.

*Why it matters:* the shipped build assumes exactly one principal in five load-bearing places: one
credential family, one limiter identity, one spend budget, no owner column on any table, and
"approval means the owner said yes". Adding a second person is not a screen; it is a data-ownership
model that would have to be migrated out of a database while it holds someone's conversations.
Deciding it in writing first keeps the next slice cheap and the privacy story short enough to be
true.

*Plain words:* a **capability link** is a URL that is itself the permission: whoever holds it can
read the one thing it points at, and nothing else. It is not an account, so it carries no identity
and nothing to revoke beyond the link. **Read-only** means no reply, no edit, no download, and no
asking the assistant anything.

- Linear: https://linear.app/kelp-ai/issue/KEL-26/m4-e-sharing-and-identity-adr-spike
- Decision: [`docs/adr/0002-sharing-and-identity.md`](adr/0002-sharing-and-identity.md)
- Privacy review: [`docs/PRIVACY.md`](PRIVACY.md)
- Merge: [`05d7508`](https://github.com/Anshieee/kelp/commit/05d7508) (branch
  [`kel-26`](https://github.com/Anshieee/kelp/tree/kel-26), head `a1a5b1f`)

Status detail: documentation only, so no tests were added and the suite stayed at 1004 at that
merge. The slice it specified is no longer only paper: KEL-28 built the link and §14.1 of the ADR is
a dated amendment recording what shipped, where it differs from §14, and why nothing in it widens a
guarantee. One honest limit survives. The decision admits no named collaborator at all, so "several
people work in one thread" — the thing most people mean by sharing — is explicitly out of scope, and
the product still has exactly one user.

### KEL-27 — Proactive turn runner — DONE

KEL-23 could store and validate a schedule, but nothing in the running server fired it. KEL-27 closes
that loop: a due task now becomes a real assistant turn through `chat/proactive.py`, on the same
`ChatService` path, kernel loop, policy gate, and SSE stream as an interactive message. There is no
second execution path and no duplicate message store. Each schedule gets a stable dedicated thread
(`ses_<task_id>`, titled `Scheduled: …`, `title_source: "schedule"`); the user message carries one
`schedule_trigger` block with `origin=schedule`, the schedule id, the run id, and the scheduled
instant, followed by the task's prompt as ordinary text.

Three gates run before any thread, message, turn, or provider call exists, in this order: the
persisted spend ceiling (`ChatService._refuse_if_over_spend`), a concurrency ceiling
(`KELP_PROACTIVE_MAX_CONCURRENT`, default 1), and a rolling hourly ceiling
(`KELP_PROACTIVE_MAX_RUNS_PER_HOUR`, default 5). Being over either proactive cap is a **skip**, never
a queue: the due window is consumed and the skip is recorded with a reason code (`spend_cap`,
`concurrency_cap`, `hourly_cap`) on the task's `skipped_count`. Spend is checked first, so a capped
window never spends an hourly admission on a run that cannot start.

A per-schedule failure breaker sits on top of those gates. Consecutive failed turns increment the
task's `failure_count`; a clean turn resets it; at `KELP_PROACTIVE_FAILURE_LIMIT` (default 3) the
schedule is parked `disabled` so one broken task cannot burn the hour's allowance retrying itself.
An interrupted or cancelled turn does not count, because a restart or an owner shutdown says
nothing about the task.

Headless approval follows ADR 0002 §5. When a tool asks for a decision, the runner records a denial
through the same service path an owner's Deny uses, leaves the tool unexecuted, and parks the
schedule in `paused_needs_approval`; it never auto-approves and never resumes on its own. The
denial's `approval_resolved` event and audit row carry `actor="schedule"`, not the owner's. Every fire
writes exactly one content-free `schedule_run` audit row — schedule id, run id, turn id, outcome,
duration, input/output tokens, estimated cost, `actor="schedule"`, `on_behalf_of="owner"`,
`origin_ref={schedule_id, run_id}` — and never the title or prompt. The M4-A scheduler's own row for
a handled fire is suppressed so there is one row, not two.

`chat/server.py` starts the ticker after boot recovery has settled interrupted turns and stops it
before the store closes. A turn killed with the process is settled `interrupted` by recovery and is
**never retried**; boot reconciliation only writes the one run row the kill prevented, so a killed
fire ends with one interrupted turn and no duplicate message or run.

- Linear: https://linear.app/kelp-ai/issue/KEL-27/m4-b-proactive-turn-runner
- Runner: [`chat/proactive.py`](../chat/proactive.py)
- Wiring: [`chat/server.py`](../chat/server.py) (`build_app`, `Application.close`)
- Tests: [`tests/test_proactive.py`](../tests/test_proactive.py)

Status detail: merged into `main` at `c62be17` from `kel-27` (head `79dd6f8`), adding 12 tests to a
suite of 1004, taking it to 1016; with KEL-28 the full suite is 1045. The twelve M4-B tests cover one due
task producing one turn and one row, a second fire reusing the same dedicated thread, a headless
approval parking the schedule with no side effect and `actor="schedule"` on its resolution, twenty
simultaneous due tasks capped at one run with nineteen recorded skips, an hourly cap that skips
past its limit, a spend cap winning over the hourly cap before any provider call, a failure breaker
disabling the schedule at its limit, a clean turn resetting that count, a dead event loop failing
the run without leaving an un-awaited coroutine, a live SSE stream seeing the proactive turn, and a
`SIGKILL` mid-run that restarts to one interrupted turn with one recovered run row. Three honest
limits. The hourly cap is counted from this
process, so a restart resets it; it bounds a runaway runner, not a fleet. A proactive turn that
needs approval is denied rather than offered as an owner decision card: the schedule pauses and the
owner can act on the schedule, but the specific tool call cannot be approved after the fact. And
the per-schedule health counters are written with an unlocked read-modify-write over the task file,
so a tick landing in the same few milliseconds can lose one counter bump; the `schedule_run` audit
row still records every fire and skip, and the next turn repairs the counter.

### KEL-28 — Read-only share links — DONE

The slice ADR 0002 specified, built. `POST /api/threads/{id}/share` freezes a thread and returns a
256-bit URL-safe token **once**; the row is keyed by `sha256(token)`, so the token can never be read
back. The link lives seven days by default (`KELP_SHARE_TTL_SECONDS`) and a longer request is clamped
to thirty days (`KELP_SHARE_MAX_TTL_SECONDS`). `GET /api/threads/{id}/shares` lists a thread's links
by the first sixteen hex characters of the hash, with state (active, expired, revoked), timestamps,
snapshot size, and an aggregate view count; `DELETE` on one id or on the collection revokes, flagging
the row rather than deleting it so a concurrent read cannot lose it mid-request.

The anonymous surface has two routes. `GET /api/shared/{token}` returns the frozen snapshot and
`GET /api/shared/{token}/stream` replays the same snapshot as events (`snapshot`, then one `message`
frame per message, then fifteen-second heartbeats) and tails nothing new, because the snapshot is
immutable. It re-checks the row every tick and closes within one tick of a revoke, an expiry, or a
purge, so it inherits KEL-24's one-tick close rule. `Last-Event-ID` and `?after=` resume at a message
index. Views are one `share_views (day, token_hash, views)` row per day and link; the first view of a
day also writes one `share_viewed` audit record with `actor="share"`, and a refused open is not a
view.

What makes it safe is mostly subtraction. The token is stored as a hash and never appears in a log,
a metric label, or an audit row. Unknown, empty, malformed, expired, revoked, and purged tokens are
hashed, looked up by the same key, and rendered by the same builder, so the bodies are byte-identical
and no header distinguishes them. Every non-GET under the prefix is a `405` naming GET — including
`HEAD`, `OPTIONS`, `PUT`, and `TRACE` — before any lookup, and the request body is never read; no
content, preview, download, or approval route exists there, so no artifact bytes are reachable. The
two share rate classes (30/s burst 10 per token hash, 600/s burst 200 process-wide) are the exception
to fail-open: a limiter fault refuses the read, and a read is charged before the lookup so many
distinct links cannot collectively exhaust the process. Open anonymous streams are capped
process-wide (`KELP_MAX_SHARE_STREAMS`, default 16). Every response, including `404`, `405`, and
`429`, carries `no-store`, `no-referrer`, `noindex, nofollow`, `nosniff`, and a deny-everything
content policy, and a share never sets a cookie even for an owner.

The snapshot is a projection: `storage_path`, `sha256`, attachment handles and URLs, approval ids,
turn and call ids, and tool-call arguments and results are dropped structurally; pending approval
cards are excluded and counted, so a half-finished turn cannot leak an argv payload. It is also
scrubbed for handle-shaped strings, because a handle can be typed into message text or a citation
where no allow-list reaches. A thread over 4 MiB (`KELP_SHARE_MAX_SNAPSHOT_BYTES`) is refused rather
than truncated. Nothing else's data moves: deleting a thread deletes its shares and view counts in the
same transaction, creating a share re-checks the parent inside its write transaction so a link cannot
outlive its thread, and expired, revoked, and parentless rows are swept by `ChatStore.share_sweep` on
the boot pass and then daily.

- Linear: https://linear.app/kelp-ai/issue/KEL-28/m4-f-read-only-thread-share-link
- Routes and headers: [`chat/api.py`](../chat/api.py)
- Store, projection, views, sweep: [`chat/store.py`](../chat/store.py)
- Decision and its amendment: [`docs/adr/0002-sharing-and-identity.md`](adr/0002-sharing-and-identity.md) (§14.1)
- Spec: [`docs/CHAT_SPEC.md`](CHAT_SPEC.md) §12.3 and §15.7
- Tests: [`tests/test_share_api.py`](../tests/test_share_api.py)
- Merge: [`dc50925`](https://github.com/Anshieee/kelp/commit/dc50925) (branch
  [`kel-28`](https://github.com/Anshieee/kelp/tree/kel-28), head `98ded52`)

Status detail: merged into `main` at `dc50925` from `kel-28` (head `98ded52`), adding 29 tests to a
suite of 1016, taking it to 1045. The tests cover create and revoke, the listing shape, the anonymous
snapshot and its stream, identical `404`s across every bad-token class, the GET-only rule for every
other verb, the projection dropping handles, approvals, and tool arguments, the view counter, the
stream cap, the sweep, and a purge racing a create. Three honest limits. The read surface is JSON,
not a rendered page, because there is no frontend in this build, so a server-rendered page is a later
issue with a real consumer. Rotating the root token or the session secret does not revoke links: the
explicit revoke routes are the only kill switch. And a revoke cannot recall a copy already read.

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
- Shared copy of the code: https://github.com/Anshieee/kelp/tree/main — head `bbff376`
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
  - `kel-17` — merged at `b48e113`; branch head `1e8d378`
  - `kel-18` — merged at `8884a5d`; branch head `544a545`
  - `kel-20` — merged at `223964c`; branch head `1f9423e`
  - `kel-21` — merged at `25584f8`; branch head `4f96577`
  - `kel-19` — merged at `b5d7196`; branch head `5fdc1f8`
  - `kel-22` — merged at `f2fa77f`; branch head `824522b`
  - `kel-24` — merged at `a9814ff`; branch head `bc009cd`
  - `kel-23` — merged at `13fc687`; branch head `4dd78e6`
  - `kel-25` — merged at `08f87e5`; branch head `5d639fc`
  - `kel-26` — merged at `05d7508`; branch head `a1a5b1f`
  - `kel-27` — merged at `c62be17`; branch head `79dd6f8`
  - `kel-28` — merged at `dc50925`; branch head `98ded52`
- Change history: https://github.com/Anshieee/kelp/commits/main
- Pull requests: https://github.com/Anshieee/kelp/pulls
- Tests: 1045 pass on `main` (`python -m pytest`); `make demo` plays all six beats, and
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
  [KEL-16](https://linear.app/kelp-ai/issue/KEL-16/m2-f-nebius-deploy-dockerfile-gpu-runbook-restart-policy),
  [KEL-17](https://linear.app/kelp-ai/issue/KEL-17/m3-a-abuse-resistance-rate-limits-body-ceiling-concurrency-caps),
  [KEL-18](https://linear.app/kelp-ai/issue/KEL-18/m3-c-spend-guardrails-dailymonthly-usd-ceilings-with-a-hard-stop),
  [KEL-20](https://linear.app/kelp-ai/issue/KEL-20/m3-d-enforce-declared-effects-and-scopes-at-invoke-time),
  [KEL-21](https://linear.app/kelp-ai/issue/KEL-21/m3-b-retention-purge-hard-delete-orphan-sweep-log-rotation),
  [KEL-19](https://linear.app/kelp-ai/issue/KEL-19/m3-e-owner-auth-session-cookie-csrf-auth-audit-trail),
  [KEL-22](https://linear.app/kelp-ai/issue/KEL-22/m3-f-privacy-review-privacymd-egress-ledger-exec-boundary-decision),
  [KEL-24](https://linear.app/kelp-ai/issue/KEL-24/m4-c-device-sessions-durable-listable-revocable-presence),
  [KEL-23](https://linear.app/kelp-ai/issue/KEL-23/m4-a-scheduler-store-cron-runtime),
  [KEL-25](https://linear.app/kelp-ai/issue/KEL-25/m4-d-plugin-packages-install-digest-pin-load-by-path),
  [KEL-26](https://linear.app/kelp-ai/issue/KEL-26/m4-e-sharing-and-identity-adr-spike),
  [KEL-27](https://linear.app/kelp-ai/issue/KEL-27/m4-b-proactive-turn-runner),
  [KEL-28](https://linear.app/kelp-ai/issue/KEL-28/m4-f-read-only-thread-share-link)

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
- ADR 0002, sharing and identity: [`docs/adr/0002-sharing-and-identity.md`](adr/0002-sharing-and-identity.md)
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

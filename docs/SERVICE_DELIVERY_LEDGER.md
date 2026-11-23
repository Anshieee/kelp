# Kelp demo delivery — progress ledger (goal ad5ff68a, plan rev 3)

Cutoff: working cutoff 2026-11-03T01:39Z treated as EXPIRED. Real submission time still needs owner confirmation.

## Requirements (delivery goal)
- Phone web app live, demo API live, landing live, VM Kelp running, computer view with view-only proof + chat-driven browser proof.
- Backend contract per docs/HACKATHON_DEMO_CONTRACT.md (Plan B: Netlify Functions + GitHub Actions).
- No force-push, no remote deletions, plain merges after green CI + review. Never print secrets. Never Nebius. Free-tier only.

## Re-baseline 2026-11-06 (this session)
- Branch now: `desktop-demo` @ b9e79fb, behind origin/desktop-demo (50af76f) by 9. Dirty: M netlify/index.html + untracked tooling/docs.
- origin/main @ c1bf712 (Merge PR #52, restore PR #49 after #48). Local HEAD b9e79fb diverges; demo work belongs on main, not desktop-demo.
- `gh pr list --state open`: only #34 (tracker sync kel-30-sdlc-tracker-sync @ c89ff0b, mergeable CONFLICTING/DIRTY).
- Recently merged: #52, #51, #50, #49 (phone theme), #48 (mobile polish), #47 (landing), #46, #45 (worker reliability), #44 (backend contract), #43, #42, #31 (repair-bot MERGED 2026-11-03T05:51Z — unblocks old blocker).
- `git ls-remote`: kelp-demo-phone-fix @ caf33e2 (parent c1bf712 = current main, dated 2026-11-03 13:25 IST, single commit "verify demo sessions through managed Netlify Identity"); kel-30-credential-target-binding @ 88d0175 (old base, no PR).
- kelp-demo-browser-act @ 7cd4aa8 (parent ee4a84a, single commit "approved showcase browser actions").
- Live checks: https://kelp-demo.netlify.app/mobile/ → 200; /api/v1/me → 401 unauthorized (correct signed-out); https://kelp-landing.netlify.app/ → 200.
- caf33e2 diff vs main: docs/KELP_DEMO_BACKEND.md, docs/KELP_DEMO_LIVE_CHECK.md (+67), netlify/functions/api-v1.mjs, demo-core.mjs, package.json/lock, tests/demo-backend/configuration.test.mjs (+112).
- Standing: PR #34 conflicts; phone-fix has no PR; binding has no PR and old base; showcase repo Anshieee/kelp-merge-showcase still assumed 404 (not rechecked this session).

## Completed (from handoff, not re-verified this session except live URLs above)
- Phone app deployed via CLI ZIP from pinned origin/main; Netlify IDs 5127f91f / deploy 6ae9663; landing deploy 6ae981dd; S3 assets public; VM Kelp /api/healthz 200 + /api/readyz 200 + one real chat reply; VNC stack installed loopback-only (no password, no view-only proof, no browser proof).

## Owner choices 2026-11-06
- Real cutoff: searched docs/ for hackathon/submission/cutoff/judging — no named hackathon or portal found. Owner must supply real submission link + time. Working cutoff stays treated as expired until then.
- Unblocker: VNC passwords path (owner runs tigervncpasswd). Hosted secrets path deferred.

## VNC path 2026-11-06 ~11:51 UTC (owner terminal) — update
- tigervncpasswd completed: full + distinct view-only set (values never shared).
- Correct unit found: kelp-computer-vnc.service (active, running). websockify 127.0.0.1:6080 → 127.0.0.1:5901; Xtigervnc :1 loopback-only confirmed.
- Correction 12:00 UTC via SSH (ubuntu@3.107.248.82, key ~/Downloads/kelp.pem): tigervncpasswd DID write the correct file — /home/ubuntu/.config/tigervnc/passwd, 16 bytes, dated 2026-11-06 11:50 (16 = full + view-only). ~/.vnc/passwd does not exist because this TigerVNC uses the XDG path. Earlier root-cause note about wrong path was wrong.
- Action taken: `sudo systemctl restart kelp-computer-vnc.service` OK at 2026-11-06 12:00:34 UTC. Status active (running), Xtigervnc :1 loopback-only with -PasswordFile /home/ubuntu/.config/tigervnc/passwd, ports 127.0.0.1:5901 + 127.0.0.1:6080 listening, passwd preserved (16 bytes, same timestamp). Kelp /api/healthz still ok (commit 7b6c97b).
- VM /opt/kelp has no browser_act handler (only credential_browser) — chat-driven browser proof is blocked until browser-act code is deployed + provider key entered.
- 12:0x UTC: owner reports VNC lets them control in both modes (same password used for full + view-only; noVNC view-only toggle is client-side). Same password defeats server-side view-only proof — needs distinct passwords + restart.
- 12:1x UTC: owner reports Epiphany loads no websites, not even example.com. VM network verified fine via SSH (curl http+https example.com 200, DNS resolves; system proxy 'none'). Epiphany window is only 474x183 on the 1440x900 desktop. ROOT CAUSE: filesystem out of inodes (df -i / = 100%, 2.38M/2.38M; `touch` fails "No space left on device"). Hog = ~/.cache/.flatpak/ holding 1.1M+ stale `webkit-<dead-pid>-*` sandbox temp dirs (each with bwrapinfo.json, all owning PIDs gone) — WebKit can't write cache/temp so pages never load. Fix in progress: find-delete of webkit-* on VM, then VNC restart + retest. (One cleanup attempt ran locally instead of via SSH — harmless, no such dir locally; glob-based rm failed "Argument list too long", re-issued via find -exec.)
- 12:50 UTC: cleanup done — inodes used 174641 of 2383360 (8%), `touch` works again. Restarted kelp-computer-vnc.service (active, fresh Xtigervnc, ports 5901/6080 loopback listening). Kelp /api/healthz ok (commit 7b6c97b). Awaiting owner retest of example.com in the desktop browser.

## Remaining / owner-needed (updated)
1. VNC passwords (owner runs tigervncpasswd) → then verify view-only rejection + Kelp chat-driven browser action.
2. Netlify backend config (DATABASE_URL, JWT secret, DEMO_KEY_ENCRYPTION_SECRET, DEMO_WORKER_TOKEN, DEMO_ALLOWED_SUBJECTS, GITHUB_DISPATCH_TOKEN, DEMO_ALLOWED_REPOS) — owner enters via dashboard.
3. GitHub Actions secrets (DEMO_WORKER_TOKEN, DEMO_READ_TOKEN, DEMO_REPO_TOKEN, var DEMO_API_BASE) — owner enters via repo Settings.
4. PR #34 conflict resolution + tracker sync for PR #45–#52 (last sync covered #44).
5. Phone-fix caf33e2: needs PR + review + green CI + merge (reconciled already — parent is current main).
6. Binding 88d0175: needs rebase onto current main, then PR.
7. Showcase repo creation (owner) + push bundle ~/Downloads/kelp-merge-showcase.bundle + switch backend config.
8. VM secrets via EnvSitter hidden-prompt flow (owner).
9. Confirm real submission time; freeze earlier if sooner.

## Service control layer plan 2026-11-07 (bot-first, infra pilots deferred)
- Read-only dev-planner pass completed. Full plan in subagent result; headline: P0 freeze service contract (serialized gate) → P1 provider neutrality/BYO keys → P2 tenant boundaries → P3 task-run records → P4 per-tenant secrets → P5 entitlements/billing hooks → P6 admin visibility + support runbook.
- Rule conflict resolved: keep `kernel/provider.py` as compat shim (`TokenFactoryClient = OpenAICompatibleClient`), move wire code to `kernel/providers/openai_compatible.py`, event types to `kernel/providers/base.py`, no vendor defaults (empty base URL fails closed with typed event).
- Shared-contract changes (KernelConfig/ChatRequest/Provider, ChatStore schema, ChatService signatures, TaskRun schema, entitlement codes, admin schemas) must be serialized one owner at a time.

## Completed (service control layer)
- P0-1 contract freeze REDONE 2026-11-09 (prior uncommitted work lost to /tmp cleanup): commit 3d1a5bb in worktree /tmp/opencode/svc-control-p0, branch service-control-layer from origin/main@c1bf712. Files: docs/adr/0003-service-control-layer.md, docs/SERVICE_CONTRACT.md, tests/test_service_contract.py (13 tests pass); docs/PRIVACY.md +4 cross-ref lines. No runtime code changed. Full suite green.
- README refresh 2026-11-09: commit 74af727 (README.md +54 lines: Current status, Roadmap P1–P6, How to contribute, Good first pickups). Verified: only README.md changed, links resolve.
- P1-1 provider extraction DONE 2026-11-09: commit 00e17cb. New kernel/providers/{base,openai_compatible,registry} + package surface; kernel/provider.py is a shim (TokenFactoryClient = ProviderClient); kernel/__init__ re-exports full set; chat/server.py builds via build_provider keeping patchable seam + provider= injection. ChatRequest.tenant_id appended last (default "owner", mismatch → tenant_mismatch, never in upstream body). Stream typed AsyncIterator[ProviderEvent]. Contract doc+test updated together (missing_base_url/missing_api_key required; admin turn_id clarified optional).
- Evidence (verified): focused suites pass; `python -m pytest tests/test_service_contract.py tests/test_kernel_provider.py -q` → EXIT 0; full suite 1168 passed, 4 pre-existing skips.

- P1-2 config generalization DONE 2026-11-09: commit 79d77f0. Generic KELP_PROVIDER / BASE_URL (empty default, fails closed) / API_KEY + KELP_PRICING_JSON; legacy Nebius names aliases only; ChatService._run_turn uses service config; server passes explicit key/environ/tenant to build_provider. Verified: contract tests EXIT 0; full suite green per implementer (1172 collected, 4 pre-existing skips).
- P2 note: kernel/loop.py still prices via module-level estimate_cost (outside P1-2 ownership); a later task should switch it to the config's pricing.

- P2-1 tenant columns DONE 2026-11-09: commit 623f240. tenants table + tenant_id on threads/artifacts/shares/sessions with idempotent ALTER migration + backfill to owner; UsageRecord + audit carry tenant_id; 13 tenancy tests prove cross-tenant denial. Verified: tenancy + contract tests EXIT 0.
- P2-2 tenant context DONE 2026-11-09: commit 40dc585. Service methods carry TenantContext; API builds tenant from authenticated principal, binds per request, rejects body mismatch pre-run; share token carries its tenant (documented); session rows store tenant_id; audit rows explicitly labelled. Verified: tenancy tests EXIT 0; full suite 1190 passed, 4 pre-existing skips.
- P2-3 must handle: tenant-blind ArtifactRegistry internals, flat storage layout, global sweeps/recovery, docs-answer index location.

- P2-3 per-tenant filesystem roots DONE 2026-11-09: commit 1e809ee. Owner paths unchanged; non-owner roots under workspace/tenants/<tenant>; slug-validated; forged paths treated as missing; tenant-aware schedule roots + proactive scoping. skills/policy.py untouched (already accepts workspace root). Verified: tenancy tests EXIT 0; full suite 1194 passed, 4 pre-existing skips.
- Cleanup survey DONE 2026-11-09 (researcher, read-only): KEEP = chat/kernel/skills/openshell/evals core + service-control-layer work; REMOVE candidates grouped (hackathon docs, obsolete code, one-time artifacts, out-of-scope features). Awaiting owner answers on ambiguous items.

## Next single action
- P4-1 per-tenant secrets DONE: commit 9719621 (kernel/secrets.py resolvers, registry without implicit environ, server wiring, 19 secret tests). Verified: focused suites EXIT 0; full suite 1211 passed.
- Reviewer-skill pass DONE: commit 3f4d55d — fixed tenant-file bypass (ambient config key overrode resolver; resolver now authoritative, miss fails closed) + 3 regression tests + docstring corrections. Verified: focused suites EXIT 0; full suite 1214 passed; offline evals pass. Left for follow-up: KELP_TENANT_SECRETS_FILE not yet in DEPLOY.md §3/env examples; tenant-file skill tokens scrubbed but not supplied.
- 3D progress site RETIRED: server stopped, files removed.
- P6 admin visibility + support runbook DONE: commit cd82dca (chat/admin.py read-only owner-only routes, ADMIN_API + SUPPORT_RUNBOOK docs, 10 admin tests + 12 runbook-doc tests). Verified: focused suites EXIT 0; full suite 1246 passed, 4 pre-existing skips.
- PUSHED 2026-11-09: service-control-layer → origin (cd82dca confirmed on remote). 13 commits ahead of origin/main.
- PR #53 MERGED: service-control-layer → main (merge commit c8bdeb5 on origin/main).
- Branches cleaned up: service-control-layer + cleanup-hackathon-debris deleted locally and on origin; worktrees removed.
- Infra pilots KICKED OFF: pilot plan doc committed as 3e34ebc on branch pilot/steel-boat (/tmp/opencode/pilot, from origin/main@c8bdeb5). docs/PILOT_PLAN.md (227 lines): 5 scripted Steel browser tasks + 5 Boat desktop tasks (test sites/fake values only), CSV measurement protocol, draft go/no-go thresholds, cost formulas, safety/stop rules. Verified: clean tree, no credentials in doc. NOT pushed. Awaiting owner signups before any runs.
- Old-branch sweep: deleted all local+remote branches fully merged into origin/main (old kel-30-*, feat/*, integration/revert/restore, kelp-demo-*, netlify setup, opencode/*, work/*) plus their 8 linked worktrees (all clean). Remote now holds only main + unmerged/active lines. Kept: desktop-demo (checked out), desktop-app, docs-design-inspo, unmerged kel-30/kel-31/kel-8/phone/landing/video lanes — none touched.

## Cleanup 2026-11-09 (user: remove hackathon naming, obsolete, one-time, out-of-scope)
- Answers: KEEP payments (future SaaS billing), REMOVE demo/, KEEP desktop/, REMOVE tracker.
- Branch cleanup-hackathon-debris (/tmp/opencode/cleanup, from origin/main@c1bf712), NOT pushed:
  - cfa465b: removed demo/ (24 files), tests/test_demo.py, tests/test_tracker_sync.py, netlify/index.html, docs SDLC mindmap files; netlify.toml KEPT+edited (mobile PWA + api redirect live on); Makefile/pyproject/ci/gitleaks/dockerignore/README/landing/PRIVACY/DEPLOY refs cleared. Full suite EXIT 0 (verified twice).
  - 3917bbe: removed docs/REVIEW_KEL6_KEL7.md, docs/linear-p0-plan.md, docs/linear-setup.md. Full suite EXIT 0.
- SPARED for Ketock (native mobile, v1 facade agreement pending): docs/HACKATHON_DEMO_CONTRACT.md, docs/HACKATHON_DEMO_PROGRESS.md. Do not delete until he confirms.
- Deleted from main-tree disk (untracked): PERSON_* runbooks, PROJECT_STATUS, KELP_UNATTENDED_*, claude-video-theme-fix, media blobs (mp4/ttf/PDFs/mindmaps), .codex/.cursor/.grok/.impeccable, deploy/phone-pr-rehearsal/, var/logs/caches/node_modules/dist/target/.netlify.
- This ledger renamed HACKATHON_DEMO_LEDGER.md → SERVICE_DELIVERY_LEDGER.md.
- Open: Storm role/channel unknown (asked user); cleanup branch needs PR + merge.
- MERGED 2026-11-09: cleanup-hackathon-debris → service-control-layer as 3fa05f2 (clean auto-merge, no conflicts). Full suite EXIT 0 after merge; empty demo/ husk dirs removed. Service branch now includes all cleanup. Still not pushed.
- P3-1 task-run records DONE: commit 0da92f3. chat/runs.py (RunStatus, TaskRun, RunRecorder with idempotency dedupe + terminal-once + swallowing on_terminal hook), runs table with unique partial indexes (one-run-per-turn, replay dedupe), terminal-guarded UPDATE. Service hooks: send/retry/stop/recover_interrupted, proactive scheduled runs. 12 run tests; verified focused suites EXIT 0; full suite 1192 passed, 4 pre-existing skips. Note: runs outlive thread purge (P3-2/retention decision pending); pending approvals not counted.
- Next: P3-2 run API (TaskRunner + POST/GET /api/runs).
- PR #34 (tracker sync) CLOSED 2026-11-09: cleanup deleted every file it synced; close-note left. No open PRs remain. No new Storm/Ketock activity on GitHub (both last active Nov 3).
- PR #54 (phone-fix) MERGED 2026-11-11 (e667c27): demo sessions verify through managed Netlify Identity. Vaktresh's review lane is free — next: binding-branch rebase.
- Pivot debate DONE: 5 agents × 2 rounds, graded per rubric, record at docs/PIVOT_PATHS_DEBATE.md. Winner on points: E (23), then A/B/D (21). Convergent recommendation: capped paid pilot on the service backend with cost-per-run accounting.

## Decision 2026-11-09: Steel/Boat instead of VM (VM hands-off)
- Computer path is managed sessions (Steel for browser-only, Boat for full desktop), NOT the hand-rolled EC2+VNC box. VM left alone: no more SSH, no restarts, no password changes; the 3.107.248.82 box is decommissioned path, not to be relied on for any rehearsal or demo.
- One in-flight SSH cleanup (stale webkit temp-file delete) was already running when the decision landed; it changes no config and needs no follow-up. No further VM action.
- Storm's kel-31-computer-tab work (RFB stub, viewer URL rules, reconnect ladder) was built against the VM viewer and is superseded — keep only the reconnect/auth UX learnings; do not build a custom VNC viewer. New answers for Storm: no websockify endpoint, no VNC password (nothing to fetch — the box is out of scope); computer verification moves to provider sessions (Boat desktop-stream URL / Steel session) once provisioned.
- New-member delegation still open: needs member identity + lane; keep clear of P3–P6 service work.

## Blockers 2026-11-09 (owner-directed, do not work around)
- BOAT TRIAL BLOCKED: do not start until Storm + Ketock deliver the phone and desktop apps AND everything is tested, connected, and working. No trial signup, no sandbox runs, no Boat keys until the blocker clears.
- STEEL DELAYED: do not run Steel tasks yet either. Pilot plan (docs/PILOT_PLAN.md, branch pilot/steel-boat, unpushed) stays parked.
- These are owner gates, not technical blockers — report status, do not route around them.

## 2026-11-22 — Demo client retired; PR #56 merged; handoff #57 open (this branch)

- origin/main @ 2308ba2 (Merge PR #56 from Stormn13/kel-31-rework, 2026-11-16). Full CI green on the merge (secret scan, dependency audit, ruff, mypy, pytest, backend+mobile JS).
- PR #55 (Storm, kel-31-computer-tab → desktop-app) CLOSED unmerged 2026-11-11 after two CHANGES_REQUESTED review rounds (C1 service-control revert, C2 RFB-vs-Steel/Boat gate, C3 dead session.url, I1 CSP, I2 cleartext/token; re-review found S0 dev-URL default + S1 credential-store removal). Superseded by PR #56 (same title, kel-31-rework → main, 4 commits), reviewed (C1 managed-RFB bypass, I1 dead Stronghold wiring, I2 bridge allowlist, I3 token echo), fixed (48319fe), APPROVED, merged.
- PR #57 (handoff/ketock-native-app → main, docs-only Ketock handoff, opened 2026-11-16) OPEN, CI green, held unmerged. Ketock posted two COMMENTED reviews 2026-11-22: a detailed 5-point redline (PROGRESS-doc absence, force-push wording, CHANGES_REQUESTED mandate, facade-sign-off ambiguity with React Native/Expo shell decision announced, fail-closed + CI clarifications), then a full retraction — demo client out of his lane, v1 facade sign-off discussion struck as owner call, fresh review to follow. Owner posted scope confirmation + handoff rework (this branch's sibling commit on handoff/ketock-native-app): §2B struck, DoD five items, PROGRESS reference corrected.
- OWNER DECISION 2026-11-22: demo phone app (netlify/mobile/ + Plan B backend) retired entirely — out of Ketock's lane and out of scope. Ketock lane = native app (React Native/Expo spike branch to follow) against the real /api only.
- THIS BRANCH (cleanup/retire-demo-client, from origin/main@2308ba2): deletes the retired surface — netlify/ + netlify.toml, demo docs (HACKATHON_DEMO_CONTRACT, KELP_DEMO_BACKEND, KELP_DEMO_LIVE_CHECK, MOBILE_DEMO_FALLBACK, MOBILE_REUSE_NOTES, mobile-preview, mobile-qa), tests/demo-backend + mobile-demo.test.mjs + test_demo_worker.py, scripts/kelp_demo_worker.py, .github/workflows/kelp-demo.yml, root package.json + package-lock.json (demo-only deps/scripts), ci.yml javascript job, Makefile demo bits. phone/ PWA + comments untouched (Ketock's lane). Ledger file itself added here (was untracked since the Nov 9 rename; never on origin/main until now).
- Verified pre-delete: root package.json scripts/deps demo-only; ci.yml javascript job demo-only; DEMO_* env refs only in deleted files; PRIVACY/egress_ledger untouched (no demo refs); DEPLOY.md untouched (no netlify/demo refs); repair-bot.yml untouched. ADR-0002 mentions netlify.toml historically (left as record).
- Steel/Boat pilots: still BLOCKED (owner gates) — clear only when Storm's desktop + Ketock's native app are delivered and tested end to end. pilot/steel-boat still local-only, now ~11 behind main; rebase deferred until gate clears.
- Vaktresh: P6 follow-up docs PR + kel-30-credential-target-binding rebase + PR still pending (branch at old 88d0175). Storm: desktop merged; retest evidence pending.

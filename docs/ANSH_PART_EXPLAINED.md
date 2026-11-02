# Ansh's part explained

This is a plain-words explanation of the part Ansh (Person B) plays in the
kelp project, what he has already merged, what is still left, and how his work
fits with the other two people.

If you are new, read this file from the top. Every technical word is explained
the first time it appears, and all of them are collected again in the glossary
at the end.

---

## 1. The big picture

The project is a private assistant you talk to from a screen. Three people each
own one slice of it:

- Aakarsh (Person A) builds the engine, the part that thinks, reads your
  documents, uses tools, and keeps the spending safe. It lives mainly in the
  `kernel/` and `openshell/` folders.
- Akshat (Person C) builds the screens, the parts a person actually sees and
  clicks. It lives mainly in the `frontend/`, `desktop/`, and `netlify/`
  folders.
- Ansh (Person B) builds the front door of the server in between. The front
  door is the layer of the server that receives every click and question from
  a screen, decides who is allowed in, sorts each request to the right place,
  slows down abusers, enforces the cost limits, and reports whether the server
  is healthy.

So every action flows in one loop: a click on a screen goes to the front door,
Ansh's part routes it, checks login, applies speed limits and cost caps, and
checks health, then hands it to the engine. The engine does the work and sends
the answer back through the same front door to the screen. The front door is
the only door into the engine, and the only door back out.

---

## 2. What Ansh has already merged

All of the pieces below are merged into `main`, the main branch of the
codebase. Each one is described in its own section, with what it is, why it
matters, and what would break if it were not there.

### 2.1 The CI gate (task 1)

What it is. CI means "continuous integration", which is a robot that runs
checks automatically every time code is uploaded. Ansh added a robot to GitHub
Actions that runs four checks on every push and every proposed change: a
secret scan (gitleaks), a style check (ruff), a type check (mypy), and the
whole test suite (pytest). The repository is set so that if any of these
checks turns red, the change is blocked from being merged into `main`.

Why it matters. Before this, nothing ran automatically when code was pushed.
Bad code, committed secrets, or style mistakes could quietly land on `main`
with nobody noticing until something broke later.

What would break without it. There would be no safety net. A password or key
could be typed into code and pushed to the world. Broken code could be merged
and reach the live server. This gate protects everyone's work, not just
Ansh's.

### 2.2 The router split (task 2, merged as `f7ed9d1`)

What it is. The server's front door used to be one very large file,
`chat/api.py`, about 1,900 lines long. Ansh cut it into smaller files, one per
topic, inside a new `chat/routers/` folder: login, threads, turns, approvals,
artifacts, health, sharing, and shared helpers. This split changes only the
file layout, not any behavior. A helper script, `scripts/route_parity.py`,
double-checks that every route still answers exactly the same before and after.

Why it matters. A file that big is hard to read, hard to review, and easy to
break by accident. Splitting it by topic means each part has a clear home and
a clear owner.

What would break without it. The front door would still be one giant file. It
would be slower and riskier for anyone to change, and reviews would be much
harder because every change touched one crowded file.

### 2.3 The refusal counters (task 3, merged as `743cb4c`)

What it is. The server has speed limits, called rate limits, that stop any one
user or gadget from flooding it. When the server says "no, too fast", it
answers with a code of 429 and a message. Ansh added a new number the server
reports, called `kelp_rate_limit_refusals_total`, which counts how many times
each speed-limit bucket refused a request over time. It appears on the metrics
page, `/api/metrics`.

Why it matters. Before this, the metrics page only showed how full the speed
limits were right now (like a fuel gauge), not how often people were being
refused (like an odometer). An operator could not see whether refusals were
growing across the day.

What would break without it. You would only know how full the speed limits were
at this exact moment, not whether a client was hammering the server and getting
refused again and again. Catching that kind of abuse early would be much
harder.

### 2.4 The readiness check for unpriced models (task 4, merged as `7d10b39`)

What it is. The server has a readiness probe, `GET /api/readyz`, which is the
"are you ready to do real work" health check an operator watches. Ansh added a
new sub-check to it called `unpriced_models`. It reports "ok: false" with a
count when there are usage records whose cost is unknown in the current time
window, and "ok: true" when there are none. It never reveals model names or any
values, only a count. He also taught the deployment probe, `deploy/healthcheck.py`,
to recognize the new "unpriced" detail code.

Why it matters. Some models have no known price, so their cost is unknown.
Before this, that fact was only visible on the metrics page. An operator
checking readiness would not see it.

What would break without it. An operator watching the health page would not be
told that some spending was uncounted or unknown. That hidden state can matter
for cost control, so hiding it from the readiness page would be a real blind
spot.

### 2.5 The dependency audit (task 5, merged as `00db5ca`)

What it is. A server relies on many third-party libraries, the building blocks
other people wrote. Ansh added a script, `tools/audit_deps.py`, and a CI job
that runs a tool called `pip-audit` against the declared libraries on every
push and proposed change. It also added a `Makefile` hook and tests that keep
the project tracker in sync.

Why it matters. Libraries sometimes get known security holes. This check finds
those known holes before the change is merged, using the latest advisory data.

What would break without it. A third-party library with a known, published
security hole could be merged and deployed without anyone noticing. There would
be no automated supply-chain check, meaning the check that the code itself is
clean would not extend to the libraries it depends on.

### 2.6 The host-pass preparation (task 6, merged as `ee4d7ac`)

What it is. Deployment means putting the server on a real machine, called a
host. The deployment manual, `docs/DEPLOY.md`, is a set of instructions for
doing that. Ansh filled in the settings template, `deploy/kelp.env.example`,
so it lists every variable the manual needs, with the two required secrets
shown as names only and every optional setting as a commented line. He also
sharpened the manual so a person who did not write it can follow all of it
without asking the author.

Why it matters. Before this, the template was incomplete. A stranger running
the manual would hit missing settings and unclear steps and have to track down
the author.

What would break without it. The deployment manual could not be followed by
someone who did not write it. They would be stuck partway through, and the
promise that a non-author can deploy the server would be false.

---

## 3. What is still left

These are the open Ansh items, not yet merged.

### 3.1 The eval-live shortcut

An eval is a repeatable test of answer quality. The offline eval runs against
recorded, fake answers and decides whether quality is good enough; it is part
of the merge gate. A live eval calls the real model through OpenRouter and
needs `OPENROUTER_API_KEY`. The project already supports running a live eval with a
longer command. What is left is a short convenience command, `make eval-live`,
that is a wrapper for that longer live command. It is a measurement tool only;
it must not block merging, and it must fail with a clear message when the key
is missing.

### 3.2 The development-setup doc decision

The project still owes a written standard development setup, a plain set of
instructions for how a new person sets up their own copy of the project to work
on it. This is an open foundation item. The work here is partly a decision
about what that document should contain, and then writing it.

### 3.3 Device audit rows, shared with Aakarsh

This is a two-person task. A device is a browser you signed in on. The project
stores a list of devices and lets the owner see them and revoke, or kick out,
one. Today, when a device appears or gets kicked out, nothing is written in the
event log. An audit row is a short log line saying "a device was added" or "a
device was removed", recording only a scrambled short version of the device id,
never the real id and never any secret. The split of work is:

- Aakarsh (Person A) does the store half first: he adds an optional audit
  parameter to the `ChatStore` object and writes the audit rows when a device
  is minted, revoked, or mass-revoked, inside the store itself so the API
  routes do not have to change.
- Then Ansh wires the logbook in `chat/server.py`, the file that starts the
  server. He passes the process's audit log into `ChatStore` when the store is
  created, so the new audit rows actually get written.

The order matters: Aakarsh's store half comes first, then Ansh's wiring, so
there is always a working build.

---

## 4. How the three parts connect

The three people share files and contracts. A contract is an agreement about
the shape of something, so that the parts fit together without each person
needing to know how the other parts work inside.

### 4.1 The shared files and contracts

`chat/api.py` and its public names. This is Ansh's front-door file. It has a
fixed list of public names that the rest of the code may import: `ChatAPI`,
`HealthProbe`, `RateLimiter`, `SessionStore`, `AuthFailureTracker`,
`TokenBucket`, `LoopRunner`, `Request`, `Response`, `make_server`, and `serve`.
The router split kept every one of these names unchanged. `chat/server.py` and
many tests import these names, so changing them would break the wiring.

`chat/server.py` wiring. This is the file that starts the whole server. It
builds every piece at boot, the store, the service, the API, the metrics, and
the audit log, and then serves. Ansh owns it. It is also where the device audit
logbook wiring will land in the leftover work.

The metric names Akshat parses. The desktop app, which Akshat owns, reads
spending numbers from the metrics page. The exact names it reads are
`spend_used_usd`, `spend_cap_usd`, and `spend_cap_ratio`. If these names
changed, Akshat's screen would lose its spending figures. The refusal counter,
`kelp_rate_limit_refusals_total`, was added alongside them without changing
those existing names.

`kernel/usage.py` is read-only for Ansh. Aakarsh owns the engine and the usage
store. Ansh reads from it for his work, for example to count unpriced records
for the readiness check, but he must never change its behavior or the model
loop's semantics. If he changed it, Aakarsh's engine tests would break.

The tracker, five files that must always agree. The project's progress is told
in five places that must be kept in sync in every pull request:
`docs/SDLC_MINDMAP.md`, `docs/sdlc-mindmap.gv`, `docs/sdlc-mindmap.svg`,
`docs/sdlc-mindmap.png`, and `netlify/index.html`. Each person syncs the
tracker in their own pull requests. The `.gv` file is the source; the pictures
are rendered from it, and the website embeds a copy of the SVG picture.

### 4.2 The merge order

The people merge in a fixed order each cycle: Aakarsh (A) first, then Ansh
(B), then Akshat (C). Ansh must not merge his branch ahead of Aakarsh's in the
same cycle. This keeps the engine changes in first, so the front door always
sits on top of a working engine.

### 4.3 What breaks when each side changes

- If Aakarsh changes `kernel/usage.py` or the stream event schema, Ansh's
  readiness and metrics reading, and Akshat's screen rendering, could break.
- If Ansh changes a public name in `chat/api.py` or the shape of a route or a
  metric name, Akshat's screen, which consumes those addresses and names,
  breaks.
- If Akshat changes the tracker website without updating all five tracker files,
  the tracker drifts and stops telling one story.
- If Ansh changes the metric names that Akshat's desktop app parses, the
  desktop app loses its spending figures.

The contracts exist so these breaks are caught by tests and reviews before they
reach the live server.

---

## 5. Glossary

A short list of every technical word used in this document, in plain words.

- API (Application Programming Interface). A set of named addresses a program
  uses to ask another program to do something.
- Audit row. A short, safe log line recording that an event happened, with no
  secret values.
- Authentication (auth). Proving who you are before you are let in.
- Cap. A ceiling, a maximum allowed amount, such as a daily spending limit.
- CI (continuous integration). A robot that runs checks automatically on every
  code upload.
- Contract. An agreement about the shape of something so separate parts fit
  together.
- Counter. A metric that only goes up over time, like an odometer.
- Deploy / deployment. Putting the server onto a real running machine, a host.
- Dependency. A third-party library the project relies on.
- Env template. A fill-in-the-blank settings file listing variable names with
  no secret values.
- Eval. A repeatable test of answer quality.
- Front door. The layer of the server that receives every request from a
  screen, checks login, routes, limits, caps, and health.
- Gauge. A metric showing a current level, like a fuel gauge.
- Gitleaks. A tool that scans code for accidentally committed secrets.
- GitHub Actions. GitHub's built-in robot runner used for CI.
- Host. The real machine the server runs on.
- HTTP. The protocol, the set of rules, browsers and servers use to talk.
- JSON. A plain text format for structured data.
- Limiter. A speed limit that stops one user from flooding the server.
- `main`. The main branch of the codebase, the shared copy of the code.
- Merge. Combining a finished branch back into `main`.
- Metrics. Numbers the server reports about its own traffic and health.
- Mypy. A type-checking tool that catches mistakes before the program runs.
- PR (pull request). A proposed change, ready for review before merging.
- Probe. A health check the server exposes, e.g. readiness.
- Pytest. The test tool that runs the test suite.
- Rate limit. A speed limit on how fast one client can ask.
- Readiness / readyz. The "are you ready to do real work" health check.
- Request. A question or command a screen sends to the server.
- Route. A specific named address on the server.
- Router. The code that sorts each incoming request to the right handler.
- Ruff. A style-checking tool that keeps code consistent.
- Secret scan. Checking that no passwords or keys were committed.
- Session. A saved sign-in on one browser or device.
- Test suite. The full set of automated tests for the project.
- Tracker. The five files recording the project's status.
- Type check. A check that catches mistakes without running the program.
- 429. The code the server returns when it refuses a request as too fast.

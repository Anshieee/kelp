# Linear MCP setup

Linear MCP is installed for the "always-on private assistant" framework in this repo.
Server: `https://mcp.linear.app/mcp` (Streamable HTTP; the `/sse` path is deprecated).
Read-only alternative: `https://mcp.linear.app/mcp/readonly`.

## Where it is configured

| Client | File | Entry |
|---|---|---|
| OpenCode (global) | `~/.config/opencode/opencode.json` | `mcp.servers.linear` = `{type: remote, url: https://mcp.linear.app/mcp, enabled: true}` |
| Claude Code (user scope) | `~/.claude.json` | `mcpServers["linear-server"]` = HTTP `https://mcp.linear.app/mcp` |
| Codex (global) | `~/.codex/config.toml` | `[mcp_servers.linear] url = "https://mcp.linear.app/mcp"` |

Backups: `~/.config/opencode/opencode.json.backup-linear-*`.

## One-time authentication (interactive, required once)

- OpenCode: `opencode mcp auth linear` (or `/mcp` inside a session)
- Claude Code: `claude mcp auth linear-server` (or `/mcp` inside a session)
- Codex: `codex mcp login linear`

Auth tokens are stored per-client (OpenCode under `~/.mcp-auth` for remote MCP OAuth),
not in this repo. No Linear key is committed anywhere.

Headless / CI alternative: create a Linear API key (Settings > Account > Security & Access)
and pass it as `Authorization: Bearer <key>` in the server headers, or export it and
reference `{env:LINEAR_API_KEY}`. Use a `Read`-only key where writes are not needed.

## Proposed Linear structure

Team: `NEB` — Nebius Assistant
Project: **Always-On Private Assistant** (one project, milestone-per-phase)

Milestones (roadmap: Prototype polished MVP -> Full vision):
1. **M0 — Foundation**: repo scaffold, CI, dev env, model/provider selection, threat model
2. **M1 — Prototype**: end-to-end query -> tool -> answer loop, local persistence, single-user auth
3. **M2 — Polished MVP**: reliability (restart/resume, health checks), memory/retrieval quality, evals, observability, deploy on Nebius GPU target
4. **M3 — Hardening**: privacy controls, secret isolation, rate limits, data retention, cost guardrails
5. **M4 — Full vision**: multi-device presence, proactive/background tasks, plugin surface, team sharing

Labels:
- `area:` `core-agent`, `inference`, `memory`, `tools`, `infra`, `security`, `evals`, `ux`
- `type:` `feature`, `bug`, `spike`, `chore`, `docs`
- `risk:` `privacy`, `cost`, `reliability`
- `phase:` `prototype`, `mvp`, `vision`

Conventions:
- Branch names carry the issue ID (`neb-123-short-slug`) so PRs auto-link.
- Cycles of 1-2 weeks; specs live in the issue description, decisions in `docs/adr/`.

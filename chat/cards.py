"""Server-side card derivation from typed tool envelopes (KEL-8).

Everything a person sees is derived here from a validated envelope, an
approval object, or artifact metadata. No model-authored UI, no HTML: only
typed payloads that a renderer can draw. See ``docs/CHAT_SPEC.md`` §6-§9.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote

from .store import scrub_artifact_handles

__all__ = [
    "APPROVAL_RATIONALE_SUFFIX",
    "ERROR_COPY",
    "EXEC_AVAILABLE",
    "EXEC_UNAVAILABLE_CLAUSE",
    "MAX_ARGUMENT_BYTES",
    "MAX_SOURCES",
    "MAX_SUMMARY_CHARS",
    "SHARE_DROPPED_FIELDS",
    "apply_sweeps",
    "approval_card",
    "approval_rationale",
    "artifact_card",
    "error_body",
    "error_copy",
    "merge_sources",
    "redact_arguments",
    "share_snapshot",
    "source_key",
    "sources_from_envelope",
    "summarize_arguments",
    "sweep_items",
    "tool_label",
]

MAX_SUMMARY_CHARS = 120
MAX_ARGUMENT_BYTES = 4096
MAX_APPROVAL_FILES = 20
MAX_SOURCES = 8
SWEEP_ITEM_LIMIT = 5
_SNIPPET_CHARS = 240

APPROVAL_RATIONALE_SUFFIX = (
    "Nothing has run yet. This approval works once."
)

#: Whether this build can execute an approved command (KEL-22 decision).
#:
#: It cannot. ``chat.server.build_app`` never constructs ``openshell.ExecGate``,
#: no skill manifest may declare the ``execute`` effect, and ``shell.exec`` is
#: not in the model-facing tool list. The flag exists so the card copy and the
#: wiring cannot drift apart: the promise below follows this value, and
#: ``tests/test_privacy.py`` fails if the flag is flipped without the gate.
#: See ``docs/PRIVACY.md`` §5 for the decision and how to reverse it.
EXEC_AVAILABLE = False

#: Card copy for an ``execute`` approval in a build that cannot run commands.
#: It still names the request and the single-use window, but it does not
#: promise a run that will not happen.
EXEC_UNAVAILABLE_CLAUSE = (
    "The assistant asked to run a command on this machine. This build does not "
    "run commands: approving records your decision and nothing is executed."
)

_SECRET_KEY_RE = re.compile(
    r"(token|key|secret|password|credential|authorization|cookie)", re.IGNORECASE
)
_SECRET_VALUE_FALLBACK = "[redacted]"

TOOL_LABELS: dict[str, str] = {
    "web.search": "Web search",
    "docs.answer": "Document answer",
    "github.read": "GitHub",
    "github.write": "Open GitHub pull request",
    "schedule.create": "Schedule task",
    "browser.act": "Showcase browser action",
    "shell.exec": "Run command",
}

# Deterministic display rules from the spec (§6). Keys are manifest properties.
_DISPLAY_FIELDS: dict[str, tuple[str, ...]] = {
    "web.search": ("query",),
    "docs.answer": ("question",),
    "github.read": ("repo", "kind"),
    "schedule.create": ("title", "schedule.expr"),
    "browser.act": ("action", "session", "page"),
    "shell.exec": ("command",),
}

ERROR_COPY: dict[str, str] = {
    "missing_api_key": "The assistant isn't connected to its model yet.",
    "provider_auth": "The model service rejected the assistant's key.",
    "provider_model_not_found": "The configured model isn't available.",
    "provider_timeout": "The model took too long to answer.",
    "provider_rate_limited": "The model service is busy.",
    "provider_unavailable": "The model service is unavailable right now.",
    "provider_rejected": "The model service refused that request.",
    "provider_unreachable": "Can't reach the model service.",
    "provider_error": "The model service returned an error it didn't explain.",
    "turn_budget_exceeded": "That answer got too long.",
    "invalid_arguments": "The assistant asked for something the tool can't do.",
    "invalid_tool_arguments": "The assistant's tool request couldn't be read.",
    "invalid_output": "The tool sent back something unexpected.",
    "invalid_tool_output": "The tool sent back something unexpected.",
    "timeout": "That step took too long.",
    "handler_error": "That step failed.",
    "tool_error": "A tool crashed while running.",
    "no_tool_executor": "This tool isn't available in this build.",
    "missing_secret": "A tool isn't configured yet.",
    "not_implemented": "This tool isn't finished yet.",
    "approval_required": "This step is waiting for your approval.",
    "approval_denied": "You said no. Nothing ran.",
    "expired": "That approval expired. Nothing ran.",
    "already_used": "That was already used. Nothing ran twice.",
    "artifact_not_found": "That file isn't available any more.",
    "unknown_skill": "The assistant tried a tool that doesn't exist.",
    "turn_in_progress": "Still answering. Wait or press Stop.",
    "rate_limited": "Too many requests. Wait a moment.",
}

_RETRYABLE = frozenset(
    {
        "provider_timeout",
        "provider_rate_limited",
        "provider_unavailable",
        "provider_unreachable",
        "provider_error",
        "invalid_tool_arguments",
        "timeout",
        "handler_error",
        "tool_error",
        "rate_limited",
    }
)


def error_copy(code: str) -> str:
    return ERROR_COPY.get(code, "Something went wrong.")


def error_body(
    code: str,
    *,
    message: str | None = None,
    retryable: bool | None = None,
    user_fixable: bool = False,
    field: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "code": code,
        "message": message if message is not None else error_copy(code),
        "retryable": (code in _RETRYABLE) if retryable is None else retryable,
        "user_fixable": user_fixable,
    }
    if field is not None:
        body["field"] = field
    if details:
        body["details"] = dict(details)
    return body


def tool_label(tool: str) -> str:
    return TOOL_LABELS.get(tool, tool)


# -- arguments -------------------------------------------------------------


def _is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY_RE.search(key))


def _scrub(value: Any, secrets: Sequence[str]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            if secret and len(secret) >= 4:
                value = value.replace(secret, _SECRET_VALUE_FALLBACK)
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _SECRET_VALUE_FALLBACK if _is_secret_key(str(key)) else _scrub(v, secrets)
            for key, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_scrub(item, secrets) for item in value]
    return value


def redact_arguments(
    arguments: Mapping[str, Any] | None,
    *,
    secrets: Sequence[str] = (),
    limit: int = MAX_ARGUMENT_BYTES,
) -> dict[str, Any]:
    """Human-facing arguments: secret keys/values removed, 4 KB cap.

    Beyond the cap only ``{"truncated": true}`` is returned; a partial payload
    might cut through a secret value.
    """

    if not arguments:
        return {}
    cleaned = _scrub(dict(arguments), list(secrets))
    encoded = json.dumps(cleaned, default=str, sort_keys=True)
    if len(encoded) > limit:
        return {"truncated": True}
    return cleaned


def _scalar(value: Any) -> str | None:
    if value is None or isinstance(value, (Mapping, list, tuple, set)):
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _nested(arguments: Mapping[str, Any], dotted: str) -> Any:
    current: Any = arguments
    for part in dotted.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def summarize_arguments(tool: str, arguments: Mapping[str, Any] | None) -> str:
    """Deterministic one-line summary, ≤120 chars, secrets never included."""

    args = dict(arguments or {})
    parts: list[str] = []
    for field in _DISPLAY_FIELDS.get(tool, ()):
        value = _nested(args, field)
        if isinstance(value, (list, tuple)):
            text = " ".join(str(item) for item in value)
        else:
            text = _scalar(value)
        if text and text.strip():
            parts.append(text)
    if not parts:
        for key, value in args.items():
            if _is_secret_key(str(key)):
                continue
            text = _scalar(value)
            if text and text.strip():
                parts.append(f"{key}={text}")
            if len(parts) == 3:
                break
    summary = " ".join(parts) if tool == "shell.exec" else ", ".join(parts)
    summary = " ".join(summary.split())
    if len(summary) > MAX_SUMMARY_CHARS:
        summary = summary[: MAX_SUMMARY_CHARS - 1] + "…"
    return summary


# -- sources ---------------------------------------------------------------


def _safe_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        return None
    return value.strip()


def _snippet(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = " ".join(value.split())
    return text[:_SNIPPET_CHARS]


def _source(
    *,
    kind: str,
    tool: str,
    title: str,
    url: str | None = None,
    snippet: str | None = None,
    handle: str | None = None,
    locator: str | None = None,
    published_at: str | None = None,
    status: str = "ok",
) -> dict[str, Any]:
    source: dict[str, Any] = {
        "id": "src_0",
        "kind": kind,
        "title": title,
        "status": status,
        "tool": tool,
    }
    if url is not None:
        source["url"] = url
    if snippet is not None:
        source["snippet"] = snippet
    if handle is not None:
        source["handle"] = handle
    if locator is not None:
        source["locator"] = locator
    if published_at is not None:
        source["published_at"] = published_at
    return source


def sources_from_envelope(
    tool: str,
    envelope: Mapping[str, Any] | None,
    *,
    artifact: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Derive source chips from one tool envelope. Failures yield one chip."""

    result = dict(envelope or {})
    ok = bool(result.get("ok"))
    output = result.get("output") if isinstance(result.get("output"), Mapping) else {}

    if tool == "web.search":
        if not ok:
            return [_source(kind="web", tool=tool, title="Web search failed", status="failed")]
        sources: list[dict[str, Any]] = []
        for item in output.get("results", []) or []:
            if not isinstance(item, Mapping):
                continue
            url = _safe_url(item.get("url"))
            title = str(item.get("title") or url or "Result")
            if url is None:
                continue
            sources.append(
                _source(
                    kind="web",
                    tool=tool,
                    title=title,
                    url=url,
                    snippet=_snippet(item.get("snippet")),
                    published_at=_scalar(item.get("published_at")),
                )
            )
        return sources

    if tool == "docs.answer":
        if not ok:
            name = (artifact or {}).get("name") or "Document"
            return [
                _source(kind="doc", tool=tool, title=str(name), status="failed")
            ]
        handle = None
        name = "Document"
        if isinstance(output.get("artifact"), Mapping):
            handle = _scalar(output["artifact"].get("handle"))
        if artifact:
            handle = artifact.get("handle") or handle
            name = str(artifact.get("name") or name)
        sources = []
        for citation in output.get("citations", []) or []:
            if not isinstance(citation, Mapping):
                continue
            locator = _scalar(citation.get("locator"))
            sources.append(
                _source(
                    kind="doc",
                    tool=tool,
                    title=name,
                    handle=handle,
                    locator=locator,
                    snippet=_snippet(citation.get("text")),
                )
            )
        return sources

    if tool == "github.read":
        if not ok:
            return []
        kind = output.get("kind")
        if kind == "repo" and isinstance(output.get("repo"), Mapping):
            repo = output["repo"]
            url = _safe_url(repo.get("html_url"))
            if url is None:
                return []
            return [
                _source(
                    kind="web",
                    tool=tool,
                    title=str(repo.get("full_name") or "Repository"),
                    url=url,
                    snippet=_snippet(repo.get("description")) or "Repository",
                )
            ]
        item = None
        for key in ("issue", "pull_request"):
            if isinstance(output.get(key), Mapping):
                item = output[key]
                break
        if item is None:
            return []
        url = _safe_url(item.get("html_url"))
        if url is None:
            return []
        return [
            _source(
                kind="web",
                tool=tool,
                title=str(item.get("title") or "GitHub item"),
                url=url,
                snippet=_snippet(item.get("body")) or str(item.get("state") or ""),
            )
        ]

    return []


def source_key(source: Mapping[str, Any]) -> tuple[str, ...]:
    url = source.get("url")
    if url:
        return (str(source.get("kind") or ""), str(url))
    if source.get("handle") or source.get("locator"):
        return (
            str(source.get("kind") or ""),
            str(source.get("handle") or ""),
            str(source.get("locator") or ""),
        )
    return (
        str(source.get("kind") or ""),
        str(source.get("title") or ""),
        str(source.get("status") or ""),
    )


def merge_sources(
    existing: Sequence[Mapping[str, Any]],
    new: Iterable[Mapping[str, Any]],
    *,
    limit: int = MAX_SOURCES,
) -> tuple[list[dict[str, Any]], int]:
    """Dedupe by first appearance, cap the visible chips, return the total."""

    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for source in list(existing) + list(new):
        key = source_key(source)
        if key in seen:
            continue
        seen.add(key)
        merged.append(dict(source))
    visible = merged[:limit]
    for index, source in enumerate(visible):
        source["id"] = f"src_{index}"
    return visible, len(merged)


# -- artifact card ---------------------------------------------------------


def artifact_card(meta: Mapping[str, Any], *, base: str = "/api/artifacts") -> dict[str, Any]:
    handle = str(meta["handle"])
    kind = str(meta.get("kind") or "file")
    # A bound artifact is only served to its thread (§8), so every URL the
    # card exposes must carry that scope. Unbound uploads need no scope.
    owner = meta.get("thread_id")
    scope = f"thread_id={quote(str(owner), safe='')}" if owner else ""
    suffix = f"?{scope}" if scope else ""
    card: dict[str, Any] = {
        "handle": handle,
        "name": meta.get("name") or handle,
        "kind": kind,
        "mime": meta.get("mime") or "application/octet-stream",
        "size_bytes": meta.get("size_bytes"),
        "pages": meta.get("pages"),
        "rows": meta.get("rows"),
        "columns": meta.get("columns"),
        "created_at": meta.get("created_at"),
        "open_url": f"{base}/{handle}/content{suffix}",
        "preview_url": f"{base}/{handle}/content{suffix}#page=1&toolbar=0",
        "download_url": f"{base}/{handle}/content?download=1"
        + (f"&{scope}" if scope else ""),
        "table": meta.get("table"),
    }
    if kind != "pdf":
        card["preview_url"] = f"{base}/{handle}/preview{suffix}"
    return card


# -- approvals -------------------------------------------------------------


def _approval_amounts(
    arguments: Mapping[str, Any] | None, secrets: Sequence[str]
) -> list[str]:
    """Render declared monetary bounds without copying arbitrary arguments."""
    result: list[str] = []
    for key in ("amount", "amount_usd", "max_amount", "max_amount_usd", "limit_usd"):
        value = (arguments or {}).get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result.append(f"{key}={_scrub(str(value), secrets)}")
    return result


def approval_rationale(
    effects: Sequence[str],
    *,
    scopes: Mapping[str, Any] | None = None,
    command: Sequence[str] | None = None,
    tool: str = "",
    expires_at: str | None = None,
    amounts: Sequence[str] = (),
    truncated: bool = False,
) -> str:
    # Adapted from OpenMausBot's PendingApproval/ApprovalCard plain-language
    # pending-card pattern (milind-soni/OpenMausBot, Apache-2.0); modified for
    # Kelp's server-derived, single-use approval payloads.
    effect_set = {str(effect) for effect in effects}
    clauses: list[str] = []
    clauses.append(f"Requested tool: {tool or 'unspecified'}.")
    effect_words = {
        "read": "read access",
        "write": "write changes",
        "network": "network access",
        "execute": "command execution",
    }
    plain_effects = [effect_words.get(effect, effect) for effect in sorted(effect_set)]
    clauses.append(
        "Requested effects: "
        + (", ".join(plain_effects) if plain_effects else "none declared")
        + "."
    )
    scope_values = scopes or {}
    for key, label in (("hosts", "Hosts"), ("paths", "Paths"), ("repos", "Repositories")):
        values = [str(value) for value in scope_values.get(key, ()) or ()]
        clauses.append(f"{label}: {', '.join(values) if values else 'none declared'}.")
    clauses.append(
        f"Monetary amount: {', '.join(amounts) if amounts else 'none declared'}."
    )
    clauses.append(
        f"Expiry: {expires_at or 'not specified'}; this approval works once."
    )
    if "execute" in effect_set and not EXEC_AVAILABLE:
        clauses.append(EXEC_UNAVAILABLE_CLAUSE)
    if tool == "github.write":
        clauses.append(
            "After you approve, the repository action is to create a branch, commit the files "
            "summarized below, and open a pull request."
        )
    elif tool == "browser.act":
        clauses.append(
            "After you approve, this one scoped action can open the public Kelp showcase, "
            "move its visible demo pointer, click only its README link, or read its README. "
            "It uses the separate demo browser profile, never credential sessions. "
            "It cannot type, submit forms, capture screenshots, or run shell commands."
        )
    elif "write" in effect_set:
        clauses.append("The assistant wants to change something in your workspace.")
    if "network" in effect_set and (scope_values.get("hosts") or ()):
        clauses.append(
            "The assistant wants to send data to "
            + ", ".join(str(host) for host in scope_values.get("hosts", ()))
            + "."
        )
    elif "execute" in effect_set and EXEC_AVAILABLE:
        clauses.append("The assistant wants to run the requested command on this machine.")
    if truncated:
        clauses.append("Some request details are truncated; see the card's truncation markers.")
    if not clauses:
        clauses.append("The assistant needs your permission for this step.")
    return " ".join(clauses + [APPROVAL_RATIONALE_SUFFIX])


def _ttl_seconds(requested_at: str, expires_at: str) -> int:
    try:
        start = datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:  # pragma: no cover - internal invariant
        return 600
    return max(0, int((end - start).total_seconds()))


def approval_card(
    *,
    approval_id: str,
    source: str,
    thread_id: str,
    turn_id: str | None,
    call_id: str | None,
    tool: str,
    effects: Sequence[str] = (),
    arguments: Mapping[str, Any] | None = None,
    scopes: Mapping[str, Any] | None = None,
    command: Sequence[str] | None = None,
    blast_radius: Sequence[str] = (),
    arguments_digest: str | None = None,
    requested_at: str | None = None,
    expires_at: str | None = None,
    status: str = "pending",
    title: str | None = None,
    skill: str | None = None,
    secrets: Sequence[str] = (),
) -> dict[str, Any]:
    secret_values = [
        str(value)
        for value in secrets
        if isinstance(value, str) and len(value) >= 4
    ]
    # The argv list is shown in full — it is what will run — except for secret
    # values, which are scrubbed exactly like any other field (§7.2).
    command_list = (
        [_scrub(str(part), secret_values) for part in command]
        if command is not None
        else None
    )
    summary_args: Mapping[str, Any] = _scrub(
        dict(arguments or {}), secret_values
    )
    if command_list:
        summary_args = {"command": command_list}
    summary = summarize_arguments(tool, summary_args)
    cleaned_arguments = redact_arguments(arguments, secrets=secret_values)
    file_summary: dict[str, Any] | None = None
    if tool == "github.write":
        raw_files = (arguments or {}).get("files")
        files = raw_files if isinstance(raw_files, (list, tuple)) else []
        visible_files: list[dict[str, Any]] = []
        for item in files[:MAX_APPROVAL_FILES]:
            if not isinstance(item, Mapping):
                continue
            path = item.get("path")
            content = item.get("content")
            if not isinstance(path, str):
                continue
            size_bytes = len(content.encode("utf-8")) if isinstance(content, str) else None
            visible_files.append(
                {
                    "path": _scrub(path, secret_values),
                    "size_bytes": size_bytes,
                }
            )
        file_summary = {
            "files": visible_files,
            # The arguments pane is intentionally all-or-nothing at 4 KB. Be
            # explicit that this bounded metadata summary is not the full
            # request whenever that payload (or the file list) was truncated.
            "truncated": cleaned_arguments == {"truncated": True}
            or len(files) > MAX_APPROVAL_FILES,
        }
    safe_scopes = {
        "paths": _scrub(list((scopes or {}).get("paths") or []), secret_values),
        "hosts": _scrub(list((scopes or {}).get("hosts") or []), secret_values),
        "repos": _scrub(list((scopes or {}).get("repos") or []), secret_values),
    }
    requested = requested_at or ""
    expires = expires_at or ""
    return {
        "id": approval_id,
        "source": source,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "call_id": call_id,
        "tool": tool,
        "skill": skill or tool,
        "title": title or f"Use {tool_label(tool)}",
        "rationale": approval_rationale(
            effects,
            scopes=safe_scopes,
            command=command_list,
            tool=tool,
            expires_at=expires,
            amounts=_approval_amounts(arguments, secret_values),
            truncated=cleaned_arguments == {"truncated": True}
            or (file_summary is not None and file_summary["truncated"]),
        ),
        "summary": summary,
        "effects": [str(effect) for effect in effects],
        "blast_radius": _scrub([str(item) for item in blast_radius], secret_values),
        "scopes": safe_scopes,
        "command": command_list,
        "arguments": cleaned_arguments,
        **({"file_summary": file_summary} if file_summary is not None else {}),
        "arguments_digest": arguments_digest,
        "requested_at": requested,
        "expires_at": expires,
        "ttl_seconds": _ttl_seconds(requested, expires),
        "single_use": True,
        "status": status,
        "decided_at": None,
        "consumed_at": None,
        "cost": {"amount_usd": None, "known": False},
    }


# -- sweep cards -----------------------------------------------------------


def sweep_items(kind: str, envelope: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Items contributed by one ``github.read`` result, in sitemap order."""

    result = dict(envelope or {})
    if not result.get("ok"):
        return []
    output = result.get("output") if isinstance(result.get("output"), Mapping) else {}
    output_kind = output.get("kind") or kind
    if output_kind == "repo":
        repo = output.get("repo")
        if not isinstance(repo, Mapping):
            return []
        url = _safe_url(repo.get("html_url"))
        if url is None:
            return []
        return [
            {
                "number": None,
                "title": str(repo.get("full_name") or "Repository"),
                "state": "open",
                "url": url,
            }
        ]
    for key in ("issue", "pull_request"):
        item = output.get(key)
        if not isinstance(item, Mapping):
            continue
        url = _safe_url(item.get("html_url"))
        if url is None:
            return []
        return [
            {
                "number": item.get("number"),
                "title": str(item.get("title") or ""),
                "state": str(item.get("state") or ""),
                "url": url,
            }
        ]
    return []


def _sweep_count(kind: str, envelope: Mapping[str, Any] | None) -> int:
    result = dict(envelope or {})
    output = result.get("output") if isinstance(result.get("output"), Mapping) else {}
    if kind == "repo" and isinstance(output.get("repo"), Mapping):
        count = output["repo"].get("open_issues_count")
        if isinstance(count, int):
            return count
    return len(sweep_items(kind, envelope))


def apply_sweeps(blocks: Sequence[Mapping[str, Any]]) -> None:
    """Attach merged ``detail.sweep`` to the anchor of each contiguous run.

    A run is consecutive ``github.read`` blocks in one message sharing ``repo``
    and ``kind``. Followers keep their own ``call_id`` and point at the anchor;
    nothing is reordered (§6.1).
    """

    index = 0
    total = len(blocks)
    while index < total:
        block = blocks[index]
        detail = block.get("detail") if isinstance(block.get("detail"), Mapping) else {}
        args = detail.get("arguments") if isinstance(detail.get("arguments"), Mapping) else {}
        if block.get("tool") != "github.read":
            index += 1
            continue
        repo = args.get("repo")
        kind = args.get("kind")
        run = [(index, block)]
        cursor = index + 1
        while cursor < total:
            nxt = blocks[cursor]
            nxt_detail = nxt.get("detail") if isinstance(nxt.get("detail"), Mapping) else {}
            nxt_args = (
                nxt_detail.get("arguments")
                if isinstance(nxt_detail.get("arguments"), Mapping)
                else {}
            )
            if (
                nxt.get("tool") != "github.read"
                or nxt_args.get("repo") != repo
                or nxt_args.get("kind") != kind
            ):
                break
            run.append((cursor, nxt))
            cursor += 1

        items: list[dict[str, Any]] = []
        count = 0
        for _, member in run:
            member_detail = (
                member.get("detail") if isinstance(member.get("detail"), Mapping) else {}
            )
            result = (
                member_detail.get("result")
                if isinstance(member_detail.get("result"), Mapping)
                else {}
            )
            items.extend(sweep_items(str(kind or ""), result))
            count += _sweep_count(str(kind or ""), result)

        anchor_detail = run[0][1].setdefault("detail", {})
        if isinstance(anchor_detail, dict):
            anchor_detail["sweep"] = {
                "repo": repo,
                "kind": kind,
                "count": max(count, len(items)),
                "items": items[:SWEEP_ITEM_LIMIT],
                "truncated": max(count, len(items)) > SWEEP_ITEM_LIMIT,
            }
            anchor_detail.pop("sweep_anchor", None)
        anchor_id = run[0][1].get("call_id")
        for _, member in run[1:]:
            member_detail = member.setdefault("detail", {})
            if isinstance(member_detail, dict):
                member_detail.pop("sweep", None)
                member_detail["sweep_anchor"] = anchor_id
        index = cursor


# -- share projection (KEL-29) ---------------------------------------------


#: Every field name a read-only share snapshot must never carry, wherever it
#: is found. The allow-lists in ``share_snapshot`` are the structural
#: guarantee — anything unlisted is dropped — and this set is the belt to
#: their braces: a key on this list is refused even if a future allow-list
#: edit mistakenly admits it. Derived from what those allow-lists drop plus
#: the approval card (``approval_card``), whose payload pending approvals
#: count but never copy.
SHARE_DROPPED_FIELDS: frozenset[str] = frozenset(
    {
        # artifact identity and storage (ADR 0002 §8)
        "handle",
        "storage_path",
        "sha256",
        # tool-call plumbing, argv and results (ADR 0002 §8)
        "call_id",
        "approval_id",
        "detail",
        "arguments",
        "result",
        "result_summary",
        "error",
        "command",
        "argument_hash",
        # message, turn and thread identity
        "id",
        "thread_id",
        "turn_id",
        "client_turn_id",
        "superseded_by",
        "sources_total",
        "updated_at",
        # approval lifecycle: approval cards are counted, never copied
        "decided_at",
        "consumed_at",
        # attachment URLs and derived metadata the allow-list does not keep
        "open_url",
        "preview_url",
        "download_url",
        "pages",
        "rows",
        "columns",
        "table",
        # sweep bookkeeping attached to tool_call detail
        "sweep",
        "sweep_anchor",
    }
)

#: A snapshot source keeps only these display fields. Anything else — an
#: artifact handle, a storage path, a content hash — is dropped structurally
#: rather than filtered by a query someone must remember.
_SHARE_SOURCE_KEYS: tuple[str, ...] = (
    "kind",
    "title",
    "url",
    "snippet",
    "locator",
    "published_at",
    "status",
    "tool",
)

#: A snapshot attachment block keeps only these display fields; the handle
#: and every derived URL stay behind.
_SHARE_ATTACHMENT_KEYS: tuple[str, ...] = (
    "name",
    "kind",
    "mime",
    "size_bytes",
    "status",
)

#: A snapshot tool_call block keeps only these display fields; the call and
#: approval ids and the whole ``detail`` payload (argv, result, error) stay
#: behind.
_SHARE_TOOL_CALL_KEYS: tuple[str, ...] = (
    "tool",
    "label",
    "summary",
    "status",
    "duration_ms",
    "effects",
)


def _allowlisted(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """Copy only allow-listed keys with a value, and never a forbidden name."""

    return {
        key: mapping[key]
        for key in keys
        if key not in SHARE_DROPPED_FIELDS and mapping.get(key) is not None
    }


def _project_source(source: Any) -> dict[str, Any]:
    if not isinstance(source, Mapping):
        return {}
    return _allowlisted(source, _SHARE_SOURCE_KEYS)


def _project_block(block: Any) -> dict[str, Any]:
    """Allow-list one block; every unlisted shape becomes ``unknown``."""

    if not isinstance(block, Mapping):
        return {"type": "unknown"}
    kind = block.get("type")
    if kind == "text":
        return {"type": "text", "text": str(block.get("text") or "")}
    if kind == "attachment":
        attachment = block.get("attachment")
        attachment = attachment if isinstance(attachment, Mapping) else {}
        return {
            "type": "attachment",
            "attachment": _allowlisted(attachment, _SHARE_ATTACHMENT_KEYS),
        }
    if kind == "tool_call":
        projected: dict[str, Any] = {"type": "tool_call"}
        for key in _SHARE_TOOL_CALL_KEYS:
            if key in SHARE_DROPPED_FIELDS:  # pragma: no cover - deny-list belt
                continue
            if key == "effects":
                projected[key] = list(block.get(key) or [])
            else:
                projected[key] = block.get(key)
        return projected
    return {"type": "unknown"}


def _project_message(message: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "role": message.get("role"),
        "status": message.get("status"),
        "created_at": message.get("created_at"),
        "blocks": [_project_block(block) for block in message.get("blocks") or []],
        "sources": [
            _project_source(source) for source in message.get("sources") or []
        ],
    }


def _scrub_handles(value: Any) -> Any:
    """Replace every artifact handle anywhere in the share projection.

    Runs over the finished dict rather than at each field, so a block shape
    added later cannot forget it. Keys are never rewritten: they are fixed
    names, not user content.
    """

    if isinstance(value, str):
        return scrub_artifact_handles(value)
    if isinstance(value, Mapping):
        return {key: _scrub_handles(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_handles(item) for item in value]
    return value


def share_snapshot(
    thread: Mapping[str, Any],
    messages: Sequence[Mapping[str, Any]],
    pending_approval_count: int,
) -> dict[str, Any]:
    """Project the settled thread once, dropping every owner-only field.

    The snapshot behind a read-only share link is a frozen, scrubbed copy:
    the allow-lists above keep only display fields, ``SHARE_DROPPED_FIELDS``
    names what must never reappear, and every artifact handle embedded in
    free text is replaced by ``[attachment]``. Pending approval cards are
    excluded and counted, so a half-finished turn cannot leak an argv
    payload. See ``docs/adr/0002-sharing-and-identity.md`` §8 and §14.1.
    """

    snapshot: dict[str, Any] = {
        "version": 1,
        "title": str(thread.get("title") or "Shared chat"),
        "message_count": len(messages),
        "messages": [_project_message(message) for message in messages],
        "omitted": {"approvals": int(pending_approval_count)},
        "viewer": "anonymous-share",
        "read_only": True,
    }
    return _scrub_handles(snapshot)

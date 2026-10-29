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

__all__ = [
    "APPROVAL_RATIONALE_SUFFIX",
    "ERROR_COPY",
    "EXEC_AVAILABLE",
    "EXEC_UNAVAILABLE_CLAUSE",
    "MAX_ARGUMENT_BYTES",
    "MAX_SOURCES",
    "MAX_SUMMARY_CHARS",
    "apply_sweeps",
    "approval_card",
    "approval_rationale",
    "artifact_card",
    "error_body",
    "error_copy",
    "merge_sources",
    "redact_arguments",
    "source_key",
    "sources_from_envelope",
    "summarize_arguments",
    "sweep_items",
    "tool_label",
]

MAX_SUMMARY_CHARS = 120
MAX_ARGUMENT_BYTES = 4096
MAX_SOURCES = 8
SWEEP_ITEM_LIMIT = 5
_SNIPPET_CHARS = 240

APPROVAL_RATIONALE_SUFFIX = (
    "Nothing has run yet. This approval expires in 10 minutes and works once."
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
    "schedule.create": "Schedule task",
    "shell.exec": "Run command",
}

# Deterministic display rules from the spec (§6). Keys are manifest properties.
_DISPLAY_FIELDS: dict[str, tuple[str, ...]] = {
    "web.search": ("query",),
    "docs.answer": ("question",),
    "github.read": ("repo", "kind"),
    "schedule.create": ("title", "schedule.expr"),
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


def approval_rationale(
    effects: Sequence[str],
    *,
    scopes: Mapping[str, Any] | None = None,
    command: Sequence[str] | None = None,
) -> str:
    effect_set = {str(effect) for effect in effects}
    clauses: list[str] = []
    if "write" in effect_set:
        clauses.append("The assistant wants to change something in your workspace.")
    if "execute" in effect_set:
        if EXEC_AVAILABLE:
            clauses.append(
                "The assistant wants to run a command on this machine. It runs once."
            )
        else:
            clauses.append(EXEC_UNAVAILABLE_CLAUSE)
    hosts = list((scopes or {}).get("hosts") or [])
    if not hosts and command and effect_set & {"execute", "network"}:
        hosts = []
    if "network" in effect_set and hosts:
        clauses.append(f"The assistant wants to send data to {hosts[0]}.")
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
            effects, scopes=safe_scopes, command=command_list
        ),
        "summary": summary,
        "effects": [str(effect) for effect in effects],
        "blast_radius": _scrub([str(item) for item in blast_radius], secret_values),
        "scopes": safe_scopes,
        "command": command_list,
        "arguments": redact_arguments(arguments, secrets=secret_values),
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

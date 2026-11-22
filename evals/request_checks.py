"""Request-shape checks over the provider requests an eval task produced.

Offline tool and multi-turn tasks replay recorded provider output, so a final
answer proves nothing about what the service *sent*. These helpers inspect the
recorded :class:`~kernel.providers.base.ChatRequest` objects instead. Each one
returns ``(ok, detail)`` ready for ``Observation.add``; details are short
counts or names, never transcripts.

``ChatRequest.messages`` is the agent loop's live transcript list, which the
loop keeps appending to. ``CountingProvider`` therefore snapshots each request
on arrival; these helpers assume that and read ``request.messages`` as it was
when that request was sent.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from kernel.providers.base import ChatRequest

__all__ = [
    "check_followup_last",
    "check_history",
    "check_retry_prompt",
    "check_tool_results",
    "check_tool_specs",
    "snapshot_request",
    "tool_spec_name",
]

_MAX_NAMES = 4


def tool_spec_name(spec: Any) -> str | None:
    """Return the tool name from a model-facing spec (wrapped or flat)."""

    if not isinstance(spec, Mapping):
        return None
    name = spec.get("name")
    if not isinstance(name, str):
        function = spec.get("function")
        name = function.get("name") if isinstance(function, Mapping) else None
    if not isinstance(name, str):
        return None
    return name.strip() or None


def snapshot_request(request: ChatRequest) -> ChatRequest:
    """Freeze a request so later transcript mutation cannot change it."""

    return ChatRequest(
        model=request.model,
        messages=[_freeze(message) for message in request.messages],
        tools=[_freeze(spec) for spec in request.tools],
        temperature=request.temperature,
        max_tokens=request.max_tokens,
        tenant_id=request.tenant_id,
    )


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _freeze(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_freeze(item) for item in value]
    return value


def _names(items: Sequence[str]) -> str:
    shown = ", ".join(items[:_MAX_NAMES])
    extra = len(items) - _MAX_NAMES
    return f"{shown} (+{extra} more)" if extra > 0 else shown


def _content_text(content: Any) -> str:
    """Flatten message content (string or block list) to plain text."""

    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (bytes, bytearray)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, Mapping) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def _roles(messages: Sequence[Mapping[str, Any]], role: str) -> list[str]:
    return [
        _content_text(message.get("content"))
        for message in messages
        if isinstance(message, Mapping) and message.get("role") == role
    ]


def check_tool_specs(
    requests: Sequence[ChatRequest], expected: Sequence[str]
) -> tuple[bool, str]:
    """Every request must carry a spec for each expected tool name."""

    if not requests:
        return False, "no provider request recorded"
    if not expected:
        return False, "no expected tool names to check"
    problems: list[str] = []
    for index, request in enumerate(requests, start=1):
        offered = {tool_spec_name(spec) for spec in request.tools}
        missing = [name for name in expected if name not in offered]
        if missing:
            problems.append(f"request {index} missing {_names(missing)}")
    if problems:
        return False, "; ".join(problems[:_MAX_NAMES])
    return True, f"{len(expected)} tool spec(s) in {len(requests)} request(s)"


def check_tool_results(
    requests: Sequence[ChatRequest], call_ids: Sequence[str]
) -> tuple[bool, str]:
    """The request after the tool calls must carry a ``tool`` message per call."""

    if not call_ids:
        return False, "no tool call was observed"
    if len(requests) < 2:
        return False, f"{len(requests)} request(s); no request followed the tool call"
    fed_back = {
        str(message.get("tool_call_id"))
        for message in requests[-1].messages
        if isinstance(message, Mapping) and message.get("role") == "tool"
    }
    missing = [call_id for call_id in call_ids if call_id not in fed_back]
    if missing:
        return False, f"last request has no tool result for {_names(missing)}"
    return True, f"{len(call_ids)} tool result(s) fed back"


def check_history(
    requests: Sequence[ChatRequest], first_prompt: str, first_answer: str
) -> tuple[bool, str]:
    """The second request must carry the first prompt and the first answer."""

    if len(requests) < 2:
        return False, f"{len(requests)} request(s); no second turn"
    messages = requests[1].messages
    users = " ".join(_roles(messages, "user"))
    assistants = " ".join(_roles(messages, "assistant"))
    missing: list[str] = []
    if first_prompt not in users:
        missing.append("first prompt")
    if not first_answer or first_answer not in assistants:
        missing.append("first answer")
    if missing:
        return False, f"second request lacks {' and '.join(missing)} ({len(messages)} message(s))"
    return True, f"history in second request ({len(messages)} message(s))"


def check_followup_last(
    requests: Sequence[ChatRequest], followup: str
) -> tuple[bool, str]:
    """The second request must end with the follow-up as a user message."""

    if len(requests) < 2:
        return False, f"{len(requests)} request(s); no second turn"
    messages = requests[1].messages
    if not messages:
        return False, "second request has no messages"
    last = messages[-1]
    ok = (
        isinstance(last, Mapping)
        and last.get("role") == "user"
        and followup in _content_text(last.get("content"))
    )
    if not ok:
        role = last.get("role") if isinstance(last, Mapping) else "?"
        return False, f"last message is {role!r}, not the follow-up user prompt"
    return True, "follow-up is the last user message"


def check_retry_prompt(requests: Sequence[ChatRequest], prompt: str) -> tuple[bool, str]:
    """The retried (last) request must carry the original user prompt."""

    if len(requests) < 2:
        return False, f"{len(requests)} request(s); no retried request"
    users = " ".join(_roles(requests[-1].messages, "user"))
    if prompt not in users:
        return False, "retried request lacks the original user prompt"
    return True, "retried request carries the original prompt"

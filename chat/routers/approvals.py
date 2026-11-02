"""Approval listing, lookup, and decisions."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from ..errors import ChatError
from ._util import _DECISION_LIMIT, _int_param, _json_response, _segment
from .body import _json_body

if TYPE_CHECKING:
    from ..api import ChatAPI, Request, Response


def _approvals(
    api: "ChatAPI", request: "Request", method: str, rest: list[str]
) -> "Response":
    if not rest:
        if method != "GET":
            raise ChatError("not_found", status=404, message="Unknown endpoint.")
        limit = _int_param(request, "limit", 50, 1, 200)
        approvals = api.service.list_approvals(
            status=request.query_one("status"),
            thread_id=request.query_one("thread_id"),
            limit=limit,
        )
        return _json_response(200, {"ok": True, "approvals": approvals})
    approval_id = _segment(rest[0])
    if len(rest) == 1:
        if method != "GET":
            raise ChatError("not_found", status=404, message="Unknown endpoint.")
        return _json_response(
            200, {"ok": True, "approval": api.service.get_approval(approval_id)}
        )
    if rest[1] != "decision" or len(rest) != 2 or method != "POST":
        raise ChatError("not_found", status=404, message="Unknown endpoint.")
    body = _json_body(api, request, limit=_DECISION_LIMIT)
    decision = body.get("decision")
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        raise ChatError(
            "invalid_request",
            status=400,
            message="note must be a string.",
            user_fixable=True,
            field="/note",
        )
    result = api.service.decide(approval_id, cast(str, decision), note)
    return _json_response(200, result)


__all__ = ["_approvals"]

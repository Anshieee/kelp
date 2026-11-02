"""Artifact upload, card, preview, and content routes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..errors import ChatError
from ._util import _int_param, _json_response, _multipart_file, _segment

if TYPE_CHECKING:
    from ..api import ChatAPI, Request, Response


def _upload(api: "ChatAPI", request: "Request") -> "Response":
    if len(request.body) > api.max_upload + 64 * 1024:
        raise ChatError("upload_too_large", status=413)
    filename, data = _multipart_file(request)
    meta = api.service.artifacts.save_upload(filename, data)
    card = api.service.artifacts.card(meta["handle"])
    return _json_response(201, {"ok": True, "artifact": card})


def _artifacts(
    api: "ChatAPI", request: "Request", method: str, rest: list[str]
) -> "Response":
    from ..api import Response

    if method != "GET" or not rest:
        raise ChatError("not_found", status=404, message="Unknown endpoint.")
    handle = _segment(rest[0])
    thread_id = request.query_one("thread_id")
    if len(rest) == 1:
        card = api.service.artifacts.card(handle, thread_id=thread_id)
        return _json_response(200, {"ok": True, "artifact": card})
    action = rest[1]
    if len(rest) != 2:
        raise ChatError("not_found", status=404, message="Unknown endpoint.")
    if action == "preview":
        rows = _int_param(request, "rows", 20, 1, 20)
        preview = api.service.artifacts.preview(
            handle, rows=rows, thread_id=thread_id
        )
        return _json_response(200, preview)
    if action == "content":
        result = api.service.artifacts.content(
            handle,
            thread_id=thread_id,
            download=request.query_one("download") == "1",
            range_header=request.header("range"),
        )
        return Response(status=result.status, headers=dict(result.headers), body=result.body)
    raise ChatError("not_found", status=404, message="Unknown endpoint.")


__all__ = ["_artifacts", "_upload"]

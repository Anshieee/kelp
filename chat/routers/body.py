"""Request body parsing and body-size limits.

The transport adapter asks ``ChatAPI.body_limit_for`` for a cap before reading
any bytes; the JSON route handlers call ``_json_body`` after the body is read.
Both bodies are unchanged from the pre-split implementation.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

from ..errors import ChatError
from ._util import (
    _JSON_LIMIT,
    _UPLOAD_OVERHEAD,
    _json_response,
)

if TYPE_CHECKING:
    from ..api import ChatAPI, Request, Response


class _BodyRejected(Exception):
    """A body that must be refused before it is read into memory."""

    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


def _body_rejection_response(rejected: "_BodyRejected") -> "Response":
    error = ChatError(
        rejected.code,
        status=rejected.status,
        message=rejected.message,
        retryable=False,
        user_fixable=True,
    )
    return _json_response(rejected.status, error.to_body())


def body_limit_for(api: "ChatAPI", path: str) -> tuple[int, int, str]:
    """Return ``(cap, status, code)`` for a request path.

    The transport adapter uses this to reject a declared body before any
    bytes are read. Uploads keep their richer ``413 upload_too_large``;
    every other route reports ``400 invalid_request``, matching
    :func:`_json_body`.
    """

    try:
        clean = unquote(urlsplit(path).path)
        segments = [segment for segment in clean.split("/") if segment]
    except ValueError:  # pragma: no cover - urlsplit rarely raises here
        segments = []
    if len(segments) >= 2 and segments[0] == "api" and segments[1] == "attachments":
        return api.max_upload + _UPLOAD_OVERHEAD, 413, "upload_too_large"
    if len(segments) >= 2 and segments[1] in {"credential-requests", "credential-worker"}:
        return 16_384, 413, "credential_body_too_large"
    return _JSON_LIMIT, 400, "invalid_request"


def _json_body(
    api: "ChatAPI",
    request: "Request",
    *,
    optional: bool = False,
    limit: int = _JSON_LIMIT,
) -> dict[str, Any]:
    if not request.body:
        if optional:
            return {}
        raise ChatError(
            "invalid_request",
            status=400,
            message="A JSON object is required.",
            user_fixable=True,
        )
    if len(request.body) > limit:
        raise ChatError(
            "invalid_request",
            status=400,
            message="The request body is too large.",
            user_fixable=True,
        )
    try:
        body = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ChatError(
            "invalid_request",
            status=400,
            message="The request body is not valid JSON.",
            user_fixable=True,
        ) from None
    if not isinstance(body, dict):
        raise ChatError(
            "invalid_request",
            status=400,
            message="The request body must be a JSON object.",
            user_fixable=True,
        )
    return body


__all__ = [
    "_BodyRejected",
    "_body_rejection_response",
    "_json_body",
    "body_limit_for",
]

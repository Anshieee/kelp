"""Typed chat-layer failures that map to HTTP status codes.

Codes are internal (``docs/CHAT_SPEC.md`` §11): the client renders one plain
sentence from ``chat.cards.error_copy`` and never shows the code or a payload.
"""

from __future__ import annotations

from typing import Any, Mapping

from .cards import error_body

__all__ = ["ChatError"]

_DEFAULT_STATUS = {
    "unknown_thread": 404,
    "unknown_turn": 404,
    "unknown_approval": 404,
    "artifact_not_found": 404,
    "turn_in_progress": 409,
    "turn_not_running": 409,
    "turn_not_retryable": 409,
    "not_latest_turn": 409,
    "not_approved": 409,
    "expired": 409,
    "already_used": 409,
    "rate_limited": 429,
    "unauthorized": 401,
    "csrf_failed": 403,
    "upload_too_large": 413,
    "unsupported_file": 415,
    "preview_unavailable": 415,
}


class ChatError(Exception):
    def __init__(
        self,
        code: str,
        *,
        status: int | None = None,
        message: str | None = None,
        retryable: bool | None = None,
        user_fixable: bool = False,
        field: str | None = None,
        details: Mapping[str, Any] | None = None,
    ):
        self.code = code
        self.status = status if status is not None else _DEFAULT_STATUS.get(code, 400)
        self.error = error_body(
            code,
            message=message,
            retryable=retryable,
            user_fixable=user_fixable,
            field=field,
            details=details,
        )
        super().__init__(self.error["message"])

    def to_body(self) -> dict[str, Any]:
        return {"ok": False, "error": dict(self.error)}

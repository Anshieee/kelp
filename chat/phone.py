"""Static mount for the phone web app (KEL-29 follow-up).

The phone app (``phone/`` in the repository root) is a small installable PWA
that talks to this server's own ``/api`` surface. Cookies, CSRF, and the SSE
streams are all same-origin by design, so the app must be served from this
server itself: ``GET /phone/`` returns the built app shell and every other
``/phone/*`` path maps into the build output.

The mount is deliberately boring:

- **Read-only.** Only ``GET`` and ``HEAD`` are answered; anything else is a
  typed 405. The app's mutations all go through ``/api``, which keeps its own
  auth, CSRF, and rate limiting.
- **Unauthenticated shell.** The app shell (HTML, JS, CSS, icons) is public,
  exactly like the share surface's static shape: it contains no owner data.
  Every ``/api`` call the shell makes enforces the owner session, so an
  unauthenticated visitor gets the shell and an empty pairing screen.
- **Traversal-safe.** A request path is resolved against the build root and
  refused unless the resolved file still lives inside it; ``..`` segments,
  symlinks that escape, and absolute escapes all answer the same
  indistinguishable 404.
- **Explicit cache policy.** ``index.html`` and ``sw.js`` are served
  ``no-cache`` so app updates propagate; the content-hashed files under
  ``assets/`` are ``immutable``; icons are day-cached.
- **Off by default when unbuilt.** With no build output on disk the mount
  answers one JSON 404 (``phone_not_built``) rather than a confusing shell.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for type checkers
    from .api import ChatAPI, Request, Response

from .errors import ChatError

__all__ = ["PHONE_PATH_PREFIX", "default_phone_root", "phone_response"]

PHONE_PATH_PREFIX = "phone"

#: Files whose content changes on every deploy; the browser must revalidate.
_NO_CACHE_FILES = frozenset({"index.html", "sw.js", "manifest.webmanifest"})

#: Content-hashed build output; safe to cache forever.
_IMMUTABLE_PREFIX = "assets/"

#: Extra MIME types mimetypes may not know on every platform.
_MIME_OVERRIDES = {
    ".webmanifest": "application/manifest+json",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".svg": "image/svg+xml",
}


def default_phone_root() -> Path:
    """The build output directory, overridable with ``KELP_PHONE_ROOT``.

    Defaults to ``phone/dist`` next to the repository root (the ``chat``
    package's parent), so a source checkout serves its own build.
    """

    import os

    override = os.environ.get("KELP_PHONE_ROOT", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "phone" / "dist"


def _content_type(suffix: str) -> str:
    mime = _MIME_OVERRIDES.get(suffix)
    if mime is None:
        mime = mimetypes.types_map.get(suffix, "application/octet-stream")
    return mime


def _cache_control(relative: str) -> str:
    if relative in _NO_CACHE_FILES:
        return "no-cache"
    if relative.startswith(_IMMUTABLE_PREFIX):
        return "public, max-age=31536000, immutable"
    return "public, max-age=86400"


def _file_response(path: Path, relative: str) -> "Response":
    from .api import Response

    body = path.read_bytes()
    return Response(
        status=200,
        headers={
            "Content-Type": _content_type(path.suffix.lower()),
            "Cache-Control": _cache_control(relative),
            "Content-Length": str(len(body)),
            "X-Content-Type-Options": "nosniff",
        },
        body=body,
    )


def _not_built() -> "Response":
    from .api import _json_response

    return _json_response(
        404,
        {
            "ok": False,
            "error": {
                "code": "phone_not_built",
                "message": "The phone app is not built. Run `make phone` in the repository root.",
                "retryable": False,
                "user_fixable": True,
            },
        },
    )


def phone_response(
    api: "ChatAPI", request: "Request", segments: list[str]
) -> "Response | None":
    """Serve one ``/phone/*`` request, or ``None`` to fall through to 404.

    ``segments`` is the path split below the leading ``phone`` segment. The
    caller has already established the first segment; this function owns
    everything after it.
    """

    if request.method.upper() not in {"GET", "HEAD"}:
        raise ChatError(
            "method_not_allowed",
            status=405,
            message="The phone app mount is read-only.",
        )

    root = getattr(api, "phone_root", None) or default_phone_root()
    root = Path(root).resolve()
    if not root.is_dir():
        return _not_built()

    relative = "/".join(segments)
    if not relative or relative == ".":
        relative = "index.html"
    if any(segment == ".." for segment in segments):
        raise ChatError("not_found", status=404, message="Unknown endpoint.")

    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        # Traversal attempt: indistinguishable from any other miss.
        raise ChatError("not_found", status=404, message="Unknown endpoint.")

    if candidate.is_dir():
        candidate = candidate / "index.html"
    if not candidate.is_file():
        # SPA fallback: an unknown extensionless path re-enters the shell.
        if "." not in Path(relative).name:
            candidate = root / "index.html"
        if not candidate.is_file():
            return _not_built()

    relative = candidate.relative_to(root).as_posix()
    response = _file_response(candidate, relative)
    if request.method.upper() == "HEAD":
        response.body = b""
    return response

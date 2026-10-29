#!/usr/bin/env python3
"""KELP health probe for the container, systemd, and the deploy runbook.

Two read-only checks, chosen by flag:

* ``--live`` (default) calls the unauthenticated liveness route
  ``/api/healthz``. A ``200`` proves the process is serving HTTP; it never
  touches SQLite, the filesystem, or the model provider.
* ``--ready`` calls the owner-authenticated readiness route ``/api/readyz``.
  It needs ``KELP_AUTH_TOKEN`` in the environment, sends it as a bearer header,
  and never prints it. A ``503`` is reduced to the failing check names and
  their stable detail codes, so probe output cannot carry a filesystem path, a
  configuration value, an exception message, or an upstream payload.

Exit codes: ``0`` healthy, ``1`` unhealthy, ``2`` the probe is misconfigured.
The script uses only the standard library: the runtime image needs no curl.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any, Mapping, Sequence

__all__ = ["base_url", "main", "probe_readiness", "summarise"]

DEFAULT_PORT = 8765
DEFAULT_TIMEOUT = 4.0
#: A readiness body is small; refuse to buffer anything larger.
MAX_BODY = 64 * 1024
#: Everything the probe treats as "the socket answered badly", not as a crash.
#: ``HTTPError`` is handled below as a real answer; ``HTTPException`` covers a
#: malformed status line or a truncated chunked body, which urllib does not wrap
#: in ``URLError`` and which would otherwise surface as a traceback.
FETCH_ERRORS = (urllib.error.URLError, http.client.HTTPException, OSError, ValueError)
#: Interface wildcards are not dialable; the probe talks to loopback instead.
_WILDCARD_HOSTS = frozenset({"", "0.0.0.0", "::", "[::]", "*"})
#: Detail codes HealthProbe emits today. Anything else is reported as unknown.
_DETAIL_CODES = frozenset({"not_configured", "timeout", "unavailable"})
_CHECK_NAME = re.compile(r"^[a-z][a-z0-9_]{0,24}$")
_COMMIT = re.compile(r"^[0-9a-zA-Z._-]{1,40}$")


def base_url(host: str, port: int) -> str:
    """``http://dialable-host:port``; wildcard binds become loopback."""

    target = (host or "").strip()
    if target in _WILDCARD_HOSTS:
        target = "127.0.0.1"
    if ":" in target and not target.startswith("["):
        target = f"[{target}]"  # IPv6 literal
    return f"http://{target}:{int(port)}"


def _fetch(url: str, *, token: str | None, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"Accept": "application/json", "User-Agent": "kelp-healthcheck"},
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read(MAX_BODY)
    except urllib.error.HTTPError as error:  # a 503 is a real answer
        try:
            body = error.read(MAX_BODY)
        except Exception:  # noqa: BLE001 - the status is enough to fail
            body = b""
        return int(error.code), body


def _decode(body: bytes) -> Mapping[str, Any] | None:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _safe_text(value: Any, pattern: re.Pattern[str]) -> str:
    if isinstance(value, str) and pattern.fullmatch(value):
        return value
    return "unknown"


def _safe_detail(value: Any) -> str:
    return value if isinstance(value, str) and value in _DETAIL_CODES else "unknown"


def summarise(payload: Mapping[str, Any] | None) -> tuple[bool, list[str], str, str]:
    """Return ``(ready, failing, version, commit)`` from a readiness body.

    Only allow-listed fields survive: boolean ``ok``, check names matching
    ``[a-z0-9_]``, and detail codes the server is known to emit. Everything
    else is dropped rather than echoed.
    """

    if payload is None:
        return False, ["body"], "unknown", "unknown"

    checks = payload.get("checks")
    failing: list[str] = []
    if isinstance(checks, Mapping):
        for name, result in checks.items():
            if not isinstance(name, str) or not _CHECK_NAME.match(name):
                continue
            if isinstance(result, Mapping) and result.get("ok") is True:
                continue
            detail = "unknown"
            if isinstance(result, Mapping):
                detail = _safe_detail(result.get("detail"))
            failing.append(f"{name}={detail}")
    elif checks is not None:
        failing.append("checks=unknown")

    ok = payload.get("ok") is True and not failing
    return (
        ok,
        sorted(failing),
        _safe_text(payload.get("version"), _COMMIT),
        _safe_text(payload.get("commit"), _COMMIT),
    )


def probe_readiness(
    url: str, *, token: str, timeout: float = DEFAULT_TIMEOUT
) -> tuple[int, str]:
    """Probe ``/api/readyz`` and return ``(exit_code, one-line summary)``."""

    status, body = _fetch(f"{url}/api/readyz", token=token, timeout=timeout)
    ready, failing, version, commit = summarise(_decode(body))
    if status == 200 and ready:
        return 0, f"readyz ok (version={version} commit={commit})"
    detail = ", ".join(failing) or f"http_status={status}"
    return 1, f"readyz degraded: {detail}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="healthcheck.py",
        description="Probe the KELP chat server without printing any secret.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--live",
        action="store_true",
        help="liveness only (/api/healthz, no auth); the default",
    )
    mode.add_argument(
        "--ready",
        action="store_true",
        help="readiness (/api/readyz) using KELP_AUTH_TOKEN",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="base URL, e.g. https://kelp.example.com (default: from KELP_HOST/KELP_PORT)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"socket timeout in seconds (default: {DEFAULT_TIMEOUT})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print one JSON object instead of a sentence",
    )
    return parser


def _emit(as_json: bool, ok: bool, mode: str, message: str, detail: str) -> None:
    if as_json:
        print(
            json.dumps(
                {"ok": ok, "mode": mode, "message": message, "detail": detail},
                sort_keys=True,
            ),
            flush=True,
        )
    else:
        print(f"kelp {message}", file=sys.stdout if ok else sys.stderr, flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    mode = "ready" if args.ready else "live"

    if args.url:
        url = args.url.rstrip("/")
        if not url.startswith(("http://", "https://")):
            print("kelp healthcheck: --url must start with http:// or https://", file=sys.stderr)
            return 2
    else:
        raw_port = (os.environ.get("KELP_PORT") or "").strip()
        try:
            port = int(raw_port) if raw_port else DEFAULT_PORT
        except ValueError:
            print("kelp healthcheck: KELP_PORT must be an integer", file=sys.stderr)
            return 2
        url = base_url(os.environ.get("KELP_HOST", ""), port)

    timeout = args.timeout if args.timeout and args.timeout > 0 else DEFAULT_TIMEOUT

    if mode == "ready":
        token = (os.environ.get("KELP_AUTH_TOKEN") or "").strip()
        if not token:
            print(
                "kelp healthcheck: KELP_AUTH_TOKEN is required for --ready",
                file=sys.stderr,
            )
            return 2
        try:
            code, message = probe_readiness(url, token=token, timeout=timeout)
        except FETCH_ERRORS as error:
            # Type only: a URLError message can embed the URL and a hostname.
            _emit(
                args.json,
                False,
                mode,
                f"readyz unreachable: {type(error).__name__}",
                "unreachable",
            )
            return 1
        _emit(args.json, code == 0, mode, message, "" if code == 0 else "degraded")
        return code

    try:
        status, body = _fetch(f"{url}/api/healthz", token=None, timeout=timeout)
    except FETCH_ERRORS as error:
        _emit(
            args.json,
            False,
            mode,
            f"healthz unreachable: {type(error).__name__}",
            "unreachable",
        )
        return 1

    payload = _decode(body)
    alive = status == 200 and isinstance(payload, Mapping) and payload.get("ok") is True
    if not alive:
        _emit(args.json, False, mode, f"healthz failed: http_status={status}", "unhealthy")
        return 1

    version = _safe_text(payload.get("version"), _COMMIT)
    commit = _safe_text(payload.get("commit"), _COMMIT)
    _emit(args.json, True, mode, f"healthz ok (version={version} commit={commit})", "")
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())

"""Health, readiness, and metrics route bodies.

The probe objects themselves stay in ``chat.api`` (``HealthProbe`` is public);
this module only renders their answers and the Prometheus exposition.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..errors import ChatError
from ..metrics import PROMETHEUS_CONTENT_TYPE
from ._util import _gauge, _json_response

if TYPE_CHECKING:
    from ..api import ChatAPI, Request, Response


def _health(
    api: "ChatAPI", request: "Request", method: str, rest: list[str]
) -> "Response":
    if rest or method != "GET":
        raise ChatError("not_found", status=404, message="Unknown endpoint.")
    # Legacy alias kept byte-compatible for existing supervisors.
    return _json_response(200, {"ok": True})


def _healthz(
    api: "ChatAPI", request: "Request", method: str, rest: list[str]
) -> "Response":
    if rest or method != "GET":
        raise ChatError("not_found", status=404, message="Unknown endpoint.")
    return _json_response(200, api.health.liveness())


def _readyz(
    api: "ChatAPI", request: "Request", method: str, rest: list[str]
) -> "Response":
    if rest or method != "GET":
        raise ChatError("not_found", status=404, message="Unknown endpoint.")
    payload, ready = api.health.readiness(runner=api.runner)
    return _json_response(200 if ready else 503, payload)


def _metrics(
    api: "ChatAPI", request: "Request", method: str, rest: list[str]
) -> "Response":
    from ..api import Response

    if rest or method != "GET":
        raise ChatError("not_found", status=404, message="Unknown endpoint.")
    # Refresh the derived spend gauges from the persisted records before
    # rendering, so a scrape is current even when no turn settled in this
    # process (a restart, or a turn that ended in another worker). The refresh
    # is best effort: a scrape must not fail because accounting is momentarily
    # unreadable (KEL-18 review M1).
    api.service.publish_spend_metrics()
    # Reached only through ``handle``, which has already authorized it.
    text = api.metrics.render() + _limiter_exposition(api)
    body = text.encode("utf-8")
    return Response(
        status=200,
        headers={
            "Content-Type": PROMETHEUS_CONTENT_TYPE,
            "Content-Length": str(len(body)),
            "Cache-Control": "no-store",
        },
        body=body,
    )


def _limiter_exposition(api: "ChatAPI") -> str:
    """Prometheus gauges for the two concurrency caps, appended to /metrics."""

    try:
        return _limiter_gauges(api)
    except Exception:  # noqa: BLE001 - a scrape must never fail on a gauge
        return ""


def _limiter_gauges(api: "ChatAPI") -> str:
    turn_used, turn_limit = api._turn_slots.stats()
    stream_used, stream_limit = api._stream_slots.stats()
    share_used, share_limit = api._share_stream_slots.stats()
    turn_ratio = turn_used / turn_limit if turn_limit > 0 else 0.0
    stream_ratio = stream_used / stream_limit if stream_limit > 0 else 0.0
    share_ratio = share_used / share_limit if share_limit > 0 else 0.0
    lines = [
        "# HELP kelp_limiter_in_use Slots currently held by a limiter.",
        "# TYPE kelp_limiter_in_use gauge",
        f'kelp_limiter_in_use{{class="turns"}} {_gauge(turn_used)}',
        f'kelp_limiter_in_use{{class="streams"}} {_gauge(stream_used)}',
        f'kelp_limiter_in_use{{class="share_streams"}} {_gauge(share_used)}',
        "# HELP kelp_limiter_capacity Slot limit per class; streams are per owner.",
        "# TYPE kelp_limiter_capacity gauge",
        f'kelp_limiter_capacity{{class="turns"}} {_gauge(turn_limit)}',
        f'kelp_limiter_capacity{{class="streams"}} {_gauge(stream_limit)}',
        f'kelp_limiter_capacity{{class="share_streams"}} {_gauge(share_limit)}',
        "# HELP kelp_limiter_saturation Fraction of a limiter's capacity in use.",
        "# TYPE kelp_limiter_saturation gauge",
        f'kelp_limiter_saturation{{class="turns"}} {_gauge(turn_ratio)}',
        f'kelp_limiter_saturation{{class="streams"}} {_gauge(stream_ratio)}',
        f'kelp_limiter_saturation{{class="share_streams"}} {_gauge(share_ratio)}',
    ]
    return "\n".join(lines) + "\n"


__all__ = [
    "_health",
    "_healthz",
    "_limiter_exposition",
    "_limiter_gauges",
    "_metrics",
    "_readyz",
]

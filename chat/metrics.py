"""Prometheus metrics for the KELP chat service (KEL-14, M2-D).

A small, dependency-free registry renders the Prometheus text exposition
format (``text/plain; version=0.0.4``) from ``GET /api/metrics``. The endpoint
sits behind the same owner auth as every other ``/api/*`` route.

The named series are the M2-D contract:

* ``http_requests_total`` — ``method``, ``route``, ``status``
* ``http_request_duration_seconds`` — histogram, same labels
* ``turns_total`` — ``outcome``
* ``turn_duration_seconds`` — histogram
* ``tokens_total`` — ``direction``, ``model``
* ``cost_usd_total`` — unlabeled counter
* ``tool_calls_total`` — ``tool``, ``status``
* ``approval_wait_seconds`` — histogram
* ``sse_reconnects_total`` — unlabeled counter
* ``recoveries_total`` — unlabeled counter

Label values are scrubbed with the OpenShell :class:`SecretScrubber`, so a
secret that somehow reaches a label (a model id built from a key, a tool name)
is rendered as ``[redacted]`` rather than exposed. Cards stay bounded because
routes are normalised by the API before they reach the registry.

:class:`LoggedUsageStore` wraps the append-only usage store: it stamps the
ambient request id onto each ``UsageRecord`` and writes a structured ``usage``
log line, so access log, turn, audit, and usage all agree on one id.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Any, Iterable, Mapping

from openshell.audit import SecretScrubber

from .logging import current_request_id, log

__all__ = [
    "Counter",
    "Histogram",
    "LoggedUsageStore",
    "Metrics",
    "PROMETHEUS_CONTENT_TYPE",
    "get_metrics",
]

PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

_HTTP_DURATION_BUCKETS = (
    0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)
_TURN_DURATION_BUCKETS = (
    0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0,
)
_APPROVAL_WAIT_BUCKETS = (
    0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0, 600.0, 1800.0,
)


def _escape_label(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )


def _format_value(value: float) -> str:
    if value == float("inf"):
        return "+Inf"
    if float(value).is_integer():
        return str(int(value))
    return repr(round(float(value), 9))


def _labels_text(labels: Mapping[str, str]) -> str:
    if not labels:
        return ""
    parts = [f'{key}="{_escape_label(value)}"' for key, value in labels.items()]
    return "{" + ",".join(parts) + "}"


class Counter:
    """A monotonically increasing sample set keyed by sorted label values."""

    def __init__(self, name: str, help_text: str, labelnames: Iterable[str] = ()):
        self.name = name
        self.help = help_text
        self.labelnames = tuple(labelnames)
        self._values: dict[tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + float(amount)

    def ensure(self, **labels: str) -> None:
        """Create a zero sample so an unlabeled series is present from boot."""

        key = self._key(labels)
        with self._lock:
            self._values.setdefault(key, 0.0)

    def value(self, **labels: str) -> float:
        return self._values.get(self._key(labels), 0.0)

    def _key(self, labels: Mapping[str, str]) -> tuple[str, ...]:
        return tuple(str(labels.get(name, "")) for name in self.labelnames)

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        with self._lock:
            items = sorted(self._values.items())
        for key, value in items:
            labels = dict(zip(self.labelnames, key))
            lines.append(f"{self.name}{_labels_text(labels)} {_format_value(value)}")
        return lines


class Histogram:
    """Cumulative buckets plus ``_sum`` and ``_count`` for durations."""

    def __init__(
        self,
        name: str,
        help_text: str,
        buckets: Iterable[float],
        labelnames: Iterable[str] = (),
    ):
        self.name = name
        self.help = help_text
        self.buckets = tuple(sorted(float(bucket) for bucket in buckets))
        self.labelnames = tuple(labelnames)
        self._counts: dict[tuple[str, ...], list[int]] = {}
        self._sums: dict[tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    def _key(self, labels: Mapping[str, str]) -> tuple[str, ...]:
        return tuple(str(labels.get(name, "")) for name in self.labelnames)

    def observe(self, value: float, **labels: str) -> None:
        value = max(0.0, float(value))
        key = self._key(labels)
        with self._lock:
            counts = self._counts.setdefault(key, [0] * len(self.buckets))
            for index, bucket in enumerate(self.buckets):
                if value <= bucket:
                    counts[index] += 1
            self._sums[key] = self._sums.get(key, 0.0) + value

    def count(self, **labels: str) -> int:
        counts = self._counts.get(self._key(labels))
        return counts[-1] if counts else 0

    def ensure(self, **labels: str) -> None:
        """Create a zero sample so an unlabeled histogram is present at boot."""

        key = self._key(labels)
        with self._lock:
            self._counts.setdefault(key, [0] * len(self.buckets))
            self._sums.setdefault(key, 0.0)

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} histogram"]
        with self._lock:
            items = sorted(self._counts.items())
            sums = dict(self._sums)
        for key, counts in items:
            labels = dict(zip(self.labelnames, key))
            for index, bucket in enumerate(self.buckets):
                bucket_labels = {**labels, "le": _format_value(bucket)}
                lines.append(
                    f"{self.name}_bucket{_labels_text(bucket_labels)} "
                    f"{_format_value(float(counts[index]))}"
                )
            total = counts[-1] if counts else 0
            inf_labels = {**labels, "le": "+Inf"}
            lines.append(
                f"{self.name}_bucket{_labels_text(inf_labels)} "
                f"{_format_value(float(total))}"
            )
            lines.append(
                f"{self.name}_sum{_labels_text(labels)} "
                f"{_format_value(sums.get(key, 0.0))}"
            )
            lines.append(
                f"{self.name}_count{_labels_text(labels)} {_format_value(float(total))}"
            )
        return lines


class Metrics:
    """The M2-D series set. One instance is shared by API and service."""

    def __init__(self, *, scrubber: SecretScrubber | None = None):
        self._scrubber = scrubber or SecretScrubber()
        self.http_requests = Counter(
            "http_requests_total", "HTTP requests by method, route, and status.",
            ("method", "route", "status"),
        )
        self.http_duration = Histogram(
            "http_request_duration_seconds", "HTTP request duration in seconds.",
            _HTTP_DURATION_BUCKETS, ("method", "route", "status"),
        )
        self.turns = Counter(
            "turns_total", "Turns by terminal outcome.", ("outcome",)
        )
        self.turn_duration = Histogram(
            "turn_duration_seconds", "Turn duration in seconds.",
            _TURN_DURATION_BUCKETS,
        )
        self.tokens = Counter(
            "tokens_total", "Model tokens by direction and model.",
            ("direction", "model"),
        )
        self.cost = Counter(
            "cost_usd_total", "Estimated model cost in US dollars."
        )
        self.tool_calls = Counter(
            "tool_calls_total", "Tool calls by tool and terminal status.",
            ("tool", "status"),
        )
        self.approval_wait = Histogram(
            "approval_wait_seconds", "Time spent waiting for a human approval.",
            _APPROVAL_WAIT_BUCKETS,
        )
        self.sse_reconnects = Counter(
            "sse_reconnects_total", "SSE stream resumes that asked for a position."
        )
        self.recoveries = Counter(
            "recoveries_total", "Turns and approvals settled by boot recovery."
        )
        # Unlabeled series exist at zero from the first scrape, so a dashboard
        # sees the contract even before the first turn or reconnect.
        self.turn_duration.ensure()
        self.approval_wait.ensure()
        self.cost.ensure()
        self.sse_reconnects.ensure()
        self.recoveries.ensure()
        self._metrics = (
            self.http_requests,
            self.http_duration,
            self.turns,
            self.turn_duration,
            self.tokens,
            self.cost,
            self.tool_calls,
            self.approval_wait,
            self.sse_reconnects,
            self.recoveries,
        )

    # -- labels ------------------------------------------------------------

    def _value(self, value: Any) -> str:
        return self._scrubber.scrub_text(str(value))

    def _labels(self, **labels: str) -> dict[str, str]:
        return {name: self._value(value) for name, value in labels.items()}

    # -- recording ---------------------------------------------------------

    def observe_http(
        self, method: str, route: str, status: int | str, duration_seconds: float
    ) -> None:
        labels = self._labels(method=method.upper(), route=route, status=str(status))
        self.http_requests.inc(**labels)
        self.http_duration.observe(duration_seconds, **labels)

    def observe_turn(self, outcome: str, duration_seconds: float) -> None:
        self.turns.inc(**self._labels(outcome=outcome))
        self.turn_duration.observe(duration_seconds)

    def add_tokens(self, direction: str, model: str, count: int) -> None:
        self.tokens.inc(
            float(count), **self._labels(direction=direction, model=model)
        )

    def add_cost(self, usd: float) -> None:
        self.cost.inc(float(usd))

    def observe_tool(self, tool: str, status: str) -> None:
        self.tool_calls.inc(**self._labels(tool=tool, status=status))

    def observe_approval_wait(self, seconds: float) -> None:
        self.approval_wait.observe(seconds)

    def inc_sse_reconnect(self, amount: int = 1) -> None:
        self.sse_reconnects.inc(float(amount))

    def add_recoveries(self, amount: int = 1) -> None:
        self.recoveries.inc(float(amount))

    # -- exposition --------------------------------------------------------

    def render(self) -> str:
        lines: list[str] = []
        for metric in sorted(self._metrics, key=lambda item: item.name):
            lines.extend(metric.render())
        return "\n".join(lines) + "\n"


_DEFAULT_METRICS: Metrics | None = None
_DEFAULT_LOCK = threading.Lock()


def get_metrics() -> Metrics:
    """Process-wide registry used when a caller does not pass its own."""

    global _DEFAULT_METRICS
    with _DEFAULT_LOCK:
        if _DEFAULT_METRICS is None:
            _DEFAULT_METRICS = Metrics()
        return _DEFAULT_METRICS


class LoggedUsageStore:
    """Wrap a usage store: stamp the request id and log one ``usage`` line.

    The wrapped store keeps owning persistence and durability. This wrapper
    only enriches the record with :func:`current_request_id` (when the record
    type supports it) and emits structured logging, so the usage line joins the
    request that produced it without a second JSONL format.
    """

    def __init__(self, inner: Any):
        self.inner = inner

    @property
    def path(self) -> Any:
        return getattr(self.inner, "path", None)

    def records(self) -> list[dict[str, Any]]:
        records = getattr(self.inner, "records", None)
        return records() if callable(records) else []

    def record(self, record: Any) -> None:
        request_id = current_request_id()
        log(
            "usage",
            request_id=request_id,
            thread_id=getattr(record, "session_id", None),
            turn=getattr(record, "turn_id", None),
            model=getattr(record, "model", None),
            input_tokens=getattr(record, "input_tokens", None),
            output_tokens=getattr(record, "output_tokens", None),
            total_tokens=getattr(record, "total_tokens", None),
            cost_usd=getattr(record, "estimated_cost_usd", None),
            duration_ms=getattr(record, "duration_ms", None),
            status=getattr(record, "outcome", None),
        )
        enriched = record
        if request_id and hasattr(record, "request_id"):
            try:
                enriched = replace(record, request_id=request_id)
            except (TypeError, ValueError):  # pragma: no cover - exotic record
                enriched = record
        self.inner.record(enriched)

"""Billing hooks for the service control layer (P5-2).

The only billing seam is a terminal run event. A sink receives an operational
metadata mapping - tokens, cost, model, tenant, and the plan snapshot stamped at
run creation - and never the prompt, message text, tool arguments, tool results,
or a provider response body. That is the same exclusion the run record and the
admin read model follow, and it is enforced here by building the payload from an
allow-list rather than by redacting a larger dictionary.

:class:`CountingBillingSink` is the default: it counts terminal runs and keeps a
bounded, content-free copy of each event so a test or a local operator can see
that the hook fired. It performs no network I/O and no invoicing.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any, Mapping, Protocol

__all__ = [
    "BillingSink",
    "CountingBillingSink",
    "billing_payload",
]

_log = logging.getLogger(__name__)

#: The only keys a billing event may carry. Adding a key is a deliberate edit to
#: this allow-list, so a prompt cannot arrive through a caller's extra field.
_BILLING_KEYS = (
    "run_id",
    "tenant_id",
    "task_type",
    "status",
    "model",
    "provider",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cost_usd",
    "plan",
)


def billing_payload(
    *,
    run_id: str,
    tenant_id: str,
    task_type: str,
    status: str,
    model: str,
    provider: str,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    cost_usd: float,
    plan: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build the allow-listed terminal event.

    ``plan`` is the snapshot captured when the run was created: plan id plus the
    limits that applied. It is copied so a sink cannot mutate the recorder's
    stored copy.
    """

    return {
        "run_id": str(run_id),
        "tenant_id": str(tenant_id),
        "task_type": str(task_type),
        "status": str(status),
        "model": str(model),
        "provider": str(provider),
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "total_tokens": int(total_tokens),
        "cost_usd": float(cost_usd),
        "plan": None if plan is None else dict(plan),
    }


class BillingSink(Protocol):
    """Receives one terminal event per run. Must never see conversation text."""

    def on_terminal_run(self, run_metadata_only: Mapping[str, Any]) -> None:
        ...


class CountingBillingSink:
    """A local, content-free default sink: count, retain a bounded tail, log."""

    def __init__(self, *, max_records: int = 1000):
        self._lock = threading.Lock()
        self._records: deque[dict[str, Any]] = deque(maxlen=max(1, int(max_records)))

    def on_terminal_run(self, run_metadata_only: Mapping[str, Any]) -> None:
        record = {
            key: run_metadata_only.get(key) for key in _BILLING_KEYS
        }
        with self._lock:
            self._records.append(record)
        _log.debug(
            "billing_run run_id=%s tenant_id=%s model=%s input_tokens=%s "
            "output_tokens=%s cost_usd=%s plan_id=%s",
            record["run_id"],
            record["tenant_id"],
            record["model"],
            record["input_tokens"],
            record["output_tokens"],
            record["cost_usd"],
            (record["plan"] or {}).get("plan_id"),
        )

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._records)

    @property
    def total_cost_usd(self) -> float:
        with self._lock:
            return sum(float(record.get("cost_usd") or 0.0) for record in self._records)

    def records(self) -> list[dict[str, Any]]:
        """A copy of the retained events, oldest first."""

        with self._lock:
            return [dict(record) for record in self._records]

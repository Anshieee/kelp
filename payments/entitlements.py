"""Entitlement decisions for the service control layer (P5-1).

``docs/SERVICE_CONTRACT.md`` §4 freezes the decision shape: field order
``decision``, ``code``, ``limit``, ``window``, ``plan_id``. A decision is total -
every request gets an ``allow`` or a ``deny``, and a missing decision is never
treated as an allow.

This module is deliberately small: it holds the plan value object, the decision
value object, the store protocol, and one static store whose plans come from
configuration. It does no tenant CRUD, no invoicing, and no payment-provider
work.

The store is the only thing the gates need. :class:`StaticEntitlementStore`
answers from a plan table and an optional usage counter:

* the owner plan is an explicit ``PlanLimits`` object with no ceilings, not a
  special case that silently bypasses the store;
* an unknown tenant or an unknown task type is denied rather than admitted;
* a configured ceiling is checked against a caller-injected counter, so the
  plan limit and the persisted usage source stay separate concerns.

Configuration (names only; no values are read here):

``KELP_ENTITLEMENT_PLANS_JSON``
    JSON object of ``plan_id`` to plan limits: ``window``, ``max_turns``,
    ``max_runs``, ``max_cost_usd``, ``allowed_task_types``.
``KELP_ENTITLEMENT_TENANT_PLANS_JSON``
    JSON object of ``tenant_id`` to plan id.
``KELP_ENTITLEMENT_PLAN``
    Plan id for tenants with no explicit mapping. Defaults to the owner plan.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

__all__ = [
    "DEFAULT_OWNER_PLAN_ID",
    "ENTITLEMENT_PLANS_ENV",
    "ENTITLEMENT_PLAN_ENV",
    "ENTITLEMENT_TENANT_PLANS_ENV",
    "EntitlementDecision",
    "EntitlementStore",
    "OWNER_UNLIMITED_PLAN",
    "PLAN_WINDOWS",
    "PlanLimits",
    "StaticEntitlementStore",
]

PLAN_WINDOWS = ("minute", "hour", "day", "month")

ENTITLEMENT_PLAN_ENV = "KELP_ENTITLEMENT_PLAN"
ENTITLEMENT_PLANS_ENV = "KELP_ENTITLEMENT_PLANS_JSON"
ENTITLEMENT_TENANT_PLANS_ENV = "KELP_ENTITLEMENT_TENANT_PLANS_JSON"

#: The one plan that exists today. Every ceiling is ``None``, which is an
#: explicit unlimited value rather than an omitted decision.
DEFAULT_OWNER_PLAN_ID = "owner-unlimited"


def _window(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text not in PLAN_WINDOWS:
        raise ValueError(
            f"{field_name} must be one of {', '.join(PLAN_WINDOWS)} or null."
        )
    return text


def _optional_int(value: Any, *, field_name: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a non-negative integer or null.")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"{field_name} must be a non-negative integer or null."
        ) from None
    if number < 0:
        raise ValueError(f"{field_name} must be a non-negative integer or null.")
    return number


def _optional_cost(value: Any, *, field_name: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a non-negative number or null.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a non-negative number or null.") from None
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field_name} must be a non-negative number or null.")
    return number


def _task_types(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise ValueError(
            "allowed_task_types must be an array of task labels or null."
        )
    labels: list[str] = []
    for item in value:
        label = str(item).strip()
        if not label:
            raise ValueError("allowed_task_types entries must be non-empty.")
        labels.append(label)
    return tuple(labels)


@dataclass(frozen=True)
class PlanLimits:
    """One plan's limits, including the explicit unlimited owner plan.

    ``window`` applies to every configured ceiling. ``None`` means the limit is
    not windowed, or (with all ceilings ``None``) that the plan is unlimited.
    ``allowed_task_types`` is ``None`` for "no task-type restriction"; an empty
    tuple denies every task type rather than silently allowing them.
    """

    plan_id: str
    window: str | None = None
    max_turns: int | None = None
    max_runs: int | None = None
    max_cost_usd: float | None = None
    allowed_task_types: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.plan_id, str) or not self.plan_id.strip():
            raise ValueError("plan_id must be a non-empty string.")
        object.__setattr__(self, "plan_id", self.plan_id.strip())
        object.__setattr__(self, "window", _window(self.window, field_name="window"))
        object.__setattr__(
            self, "max_turns", _optional_int(self.max_turns, field_name="max_turns")
        )
        object.__setattr__(
            self, "max_runs", _optional_int(self.max_runs, field_name="max_runs")
        )
        object.__setattr__(
            self,
            "max_cost_usd",
            _optional_cost(self.max_cost_usd, field_name="max_cost_usd"),
        )
        object.__setattr__(
            self,
            "allowed_task_types",
            None
            if self.allowed_task_types is None
            else _task_types(self.allowed_task_types),
        )

    @property
    def unlimited(self) -> bool:
        """True only when no ceiling and no task-type restriction is set."""

        return (
            self.max_turns is None
            and self.max_runs is None
            and self.max_cost_usd is None
            and self.allowed_task_types is None
        )

    def allows(self, task_type: str) -> bool:
        return self.allowed_task_types is None or task_type in self.allowed_task_types

    def snapshot(self) -> dict[str, Any]:
        """A read-only copy safe to attach to a run or a billing event."""

        return {
            "plan_id": self.plan_id,
            "window": self.window,
            "max_turns": self.max_turns,
            "max_runs": self.max_runs,
            "max_cost_usd": self.max_cost_usd,
            "allowed_task_types": (
                None
                if self.allowed_task_types is None
                else list(self.allowed_task_types)
            ),
        }


OWNER_UNLIMITED_PLAN = PlanLimits(plan_id=DEFAULT_OWNER_PLAN_ID)


@dataclass(frozen=True)
class EntitlementDecision:
    """The frozen §4 decision plus an optional read-only limits snapshot.

    The first five fields are the contract's field order. ``limits`` is the
    addition P5 needs to stamp the plan onto a run or billing event without a
    second store lookup; it never reorders the frozen prefix.
    """

    decision: str
    code: str
    limit: int | None = None
    window: str | None = None
    plan_id: str | None = None
    limits: Mapping[str, Any] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.decision not in {"allow", "deny"}:
            raise ValueError("decision must be 'allow' or 'deny'.")
        if not isinstance(self.code, str) or not self.code:
            raise ValueError("code must be a non-empty string.")
        object.__setattr__(self, "window", _window(self.window, field_name="window"))
        if self.limits is not None:
            object.__setattr__(self, "limits", dict(self.limits))

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"

    def to_dict(self) -> dict[str, Any]:
        """The contract fields in order, then the snapshot when present."""

        payload: dict[str, Any] = {
            "decision": self.decision,
            "code": self.code,
            "limit": self.limit,
            "window": self.window,
            "plan_id": self.plan_id,
        }
        if self.limits is not None:
            payload["limits"] = dict(self.limits)
        return payload


class EntitlementStore(Protocol):
    """The gate's only dependency: one total decision per request."""

    def decide(
        self,
        *,
        tenant_id: str,
        task_type: str,
        actor_id: str | None = None,
        role: str | None = None,
        surface: str | None = None,
    ) -> EntitlementDecision:
        ...

    def plan_for(
        self, *, tenant_id: str, task_type: str | None = None
    ) -> PlanLimits | None:
        ...


def _plan_from_record(plan_id: str, record: Mapping[str, Any]) -> PlanLimits:
    if not isinstance(record, Mapping):
        raise ValueError(f"plan {plan_id!r} must be a JSON object.")
    return PlanLimits(
        plan_id=str(record.get("plan_id") or plan_id),
        window=record.get("window"),
        max_turns=record.get("max_turns"),
        max_runs=record.get("max_runs"),
        max_cost_usd=record.get("max_cost_usd"),
        allowed_task_types=record.get("allowed_task_types"),
    )


def parse_plans(raw: str | None) -> dict[str, PlanLimits]:
    """Parse ``KELP_ENTITLEMENT_PLANS_JSON``; malformed input is an error."""

    if raw is None or not str(raw).strip():
        return {}
    try:
        payload = json.loads(raw)
    except ValueError:
        raise ValueError(f"{ENTITLEMENT_PLANS_ENV} must be valid JSON.") from None
    if not isinstance(payload, Mapping):
        raise ValueError(f"{ENTITLEMENT_PLANS_ENV} must be a JSON object.")
    plans: dict[str, PlanLimits] = {}
    for plan_id, record in payload.items():
        key = str(plan_id).strip()
        if not key:
            raise ValueError(f"{ENTITLEMENT_PLANS_ENV} keys must be plan ids.")
        plans[key] = _plan_from_record(key, record)
    return plans


def parse_tenant_plans(raw: str | None) -> dict[str, str]:
    """Parse ``KELP_ENTITLEMENT_TENANT_PLANS_JSON``."""

    if raw is None or not str(raw).strip():
        return {}
    try:
        payload = json.loads(raw)
    except ValueError:
        raise ValueError(
            f"{ENTITLEMENT_TENANT_PLANS_ENV} must be valid JSON."
        ) from None
    if not isinstance(payload, Mapping):
        raise ValueError(f"{ENTITLEMENT_TENANT_PLANS_ENV} must be a JSON object.")
    mapping: dict[str, str] = {}
    for tenant_id, plan_id in payload.items():
        tenant = str(tenant_id).strip()
        plan = str(plan_id).strip()
        if not tenant or not plan:
            raise ValueError(
                f"{ENTITLEMENT_TENANT_PLANS_ENV} maps non-empty tenant ids to "
                "non-empty plan ids."
            )
        mapping[tenant] = plan
    return mapping


class StaticEntitlementStore:
    """A config-defined plan table with an optional usage counter.

    ``counter`` returns the number of turns or runs already counted for
    ``(tenant_id, task_type, window)``; ``cost`` returns the counted cost in US
    dollars for ``(tenant_id, window)``. A caller that wires neither keeps the
    ceilings unenforced, which is why the owner default is an explicit
    unlimited plan instead of an implicit empty check.
    """

    def __init__(
        self,
        plans: Mapping[str, PlanLimits] | None = None,
        *,
        tenant_plans: Mapping[str, str] | None = None,
        default_plan_id: str = DEFAULT_OWNER_PLAN_ID,
        counter: Callable[[str, str, str], int] | None = None,
        cost: Callable[[str, str], float] | None = None,
    ):
        table: dict[str, PlanLimits] = {
            DEFAULT_OWNER_PLAN_ID: OWNER_UNLIMITED_PLAN
        }
        for plan_id, limits in (plans or {}).items():
            table[str(plan_id)] = (
                limits if isinstance(limits, PlanLimits) else _plan_from_record(
                    str(plan_id), limits
                )
            )
        if default_plan_id not in table:
            raise ValueError(f"unknown default plan {default_plan_id!r}.")
        self._plans = table
        self._tenant_plans = {
            str(tenant): str(plan) for tenant, plan in (tenant_plans or {}).items()
        }
        self._default_plan_id = default_plan_id
        self._counter = counter
        self._cost = cost

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None
    ) -> "StaticEntitlementStore":
        source = os.environ if env is None else env
        plans = parse_plans(source.get(ENTITLEMENT_PLANS_ENV))
        tenant_plans = parse_tenant_plans(source.get(ENTITLEMENT_TENANT_PLANS_ENV))
        default_plan_id = (
            str(source.get(ENTITLEMENT_PLAN_ENV) or "").strip()
            or DEFAULT_OWNER_PLAN_ID
        )
        return cls(
            plans,
            tenant_plans=tenant_plans,
            default_plan_id=default_plan_id,
        )

    @property
    def plans(self) -> Mapping[str, PlanLimits]:
        return dict(self._plans)

    @property
    def default_plan_id(self) -> str:
        return self._default_plan_id

    def plan_id_for(self, tenant_id: str) -> str:
        return self._tenant_plans.get(str(tenant_id), self._default_plan_id)

    def plan_for(
        self, *, tenant_id: str, task_type: str | None = None
    ) -> PlanLimits | None:
        del task_type  # a plan is per tenant today; the parameter is the seam
        return self._plans.get(self.plan_id_for(tenant_id))

    def decide(
        self,
        *,
        tenant_id: str,
        task_type: str,
        actor_id: str | None = None,
        role: str | None = None,
        surface: str | None = None,
    ) -> EntitlementDecision:
        del actor_id, role, surface
        plan = self.plan_for(tenant_id=tenant_id, task_type=task_type)
        if plan is None:
            # Fail closed: an unknown tenant has no plan, which is a denial.
            return EntitlementDecision(
                decision="deny",
                code="entitlement_denied",
                plan_id=None,
                limits=None,
            )
        if not plan.allows(task_type):
            return EntitlementDecision(
                decision="deny",
                code="entitlement_denied",
                window=plan.window,
                plan_id=plan.plan_id,
                limits=plan.snapshot(),
            )
        if plan.max_turns is not None and plan.window is not None:
            used = self._count(tenant_id, task_type, plan.window)
            if used >= plan.max_turns:
                return self._exhausted(plan, plan.max_turns)
        if plan.max_runs is not None and plan.window is not None:
            used = self._count(tenant_id, task_type, plan.window)
            if used >= plan.max_runs:
                return self._exhausted(plan, plan.max_runs)
        if plan.max_cost_usd is not None and plan.window is not None:
            used_cost = self._cost_for(tenant_id, plan.window)
            if used_cost >= plan.max_cost_usd:
                # The contract's ``limit`` is an integer; the dollar ceiling is
                # carried in the snapshot so no precision is lost.
                return EntitlementDecision(
                    decision="deny",
                    code="entitlement_exhausted",
                    limit=None,
                    window=plan.window,
                    plan_id=plan.plan_id,
                    limits=plan.snapshot(),
                )
        return EntitlementDecision(
            decision="allow",
            code="ok",
            limit=plan.max_turns,
            window=plan.window,
            plan_id=plan.plan_id,
            limits=plan.snapshot(),
        )

    def _exhausted(self, plan: PlanLimits, limit: int) -> EntitlementDecision:
        return EntitlementDecision(
            decision="deny",
            code="entitlement_exhausted",
            limit=limit,
            window=plan.window,
            plan_id=plan.plan_id,
            limits=plan.snapshot(),
        )

    def _count(self, tenant_id: str, task_type: str, window: str) -> int:
        if self._counter is None:
            return 0
        try:
            value = int(self._counter(str(tenant_id), str(task_type), str(window)))
        except (TypeError, ValueError):
            return 0
        return max(0, value)

    def _cost_for(self, tenant_id: str, window: str) -> float:
        if self._cost is None:
            return 0.0
        try:
            value = float(self._cost(str(tenant_id), str(window)))
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(value) or value < 0:
            return 0.0
        return value

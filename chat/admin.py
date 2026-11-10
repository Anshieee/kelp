"""Read-only admin visibility for the service control layer (P6).

``docs/SERVICE_CONTRACT.md`` §6 freezes the admin read model: the operational
fields of a :class:`~chat.runs.TaskRun`, and nothing that can carry a
conversation or a credential. This module is the only HTTP projection of that
model.

Design rules enforced here, not by the caller:

* **Owner-only.** The routes are mounted behind the ordinary owner auth and
  tenant boundary in :mod:`chat.api`; this module never authenticates anything
  itself.
* **Tenant-scoped.** The caller's bound tenant is the only tenant any query can
  name. A detail request for another tenant is the same 404 as a missing one,
  and every run, usage row, and audit entry is filtered by ``tenant_id``.
* **Content-free.** The run projection is :meth:`TaskRun.to_admin_row`; the
  audit projection is an allow-list of metadata keys. Prompt/message/tool
  payloads and secret values never enter a response: the read model is built
  from fields that cannot hold them, not redacted after the fact.
* **Read-only.** Nothing in this module writes a row, appends an audit entry,
  or changes a plan.

Known P5 behaviour surfaced by these views (documented in
``docs/ADMIN_API.md`` and ``docs/SUPPORT_RUNBOOK.md``):

* entitlement counters are bumped twice for each accepted ``POST /api/runs``;
* ``max_turns`` and ``max_runs`` share one injected usage counter;
* plan snapshots live recorder-side, so a run row has no historical plan. The
  tenant view reports the *current* configured plan, not a reconstructed past.

Run counts are bounded by the store's newest-first 500-row window; the response
names that window so an operator never reads a truncated count as a lifetime
total.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .errors import ChatError
from .logging import current_request_id
from .routers._util import (
    _int_param,
    _json_response,
    _method_not_allowed_response,
    _segment,
)
from .runs import RUN_STATUSES
from .tenancy import current_tenant, is_valid_tenant_id

__all__ = [
    "ADMIN_ENDPOINTS",
    "admin_route",
    "limit_segments",
]

#: The store's ``list_runs`` ceiling. Counts and the run index cover this many
#: newest rows; ``window`` in every payload says so.
RUN_WINDOW = 500
#: One admin page. The audit log is read whole, so its page ceiling matches the
#: run window while the default stays small enough for a terminal.
PAGE_MAX = 200
_DEFAULT_PAGE = 50

#: The only audit keys an index row may carry. ``command``, ``argument_hash``,
#: and ``extra`` are intentionally absent: they are the fields most likely to
#: hold tool input, and an admin index must not become a second copy of it.
_AUDIT_FIELDS = (
    "ts",
    "event",
    "tenant_id",
    "turn_id",
    "request_id",
    "tool",
    "actor",
    "decision",
    "outcome",
    "exit_code",
    "duration_ms",
)

#: The endpoint paths documented in ``docs/ADMIN_API.md``. The support-runbook
#: test asserts every one of these appears there and in the runbook.
ADMIN_ENDPOINTS = (
    "GET /api/admin/summary",
    "GET /api/admin/tenants",
    "GET /api/admin/tenants/{tenant_id}",
    "GET /api/admin/runs",
    "GET /api/admin/runs/{run_id}",
    "GET /api/admin/audit",
)


# -- route classification --------------------------------------------------


def limit_segments(segments: list[str]) -> list[str]:
    """Route ``/api/admin/...`` reads through the existing ``admin`` bucket.

    The limiter class is chosen by ``chat.routers.auth._classify_route``, whose
    only ``admin`` spelling today is a GET of ``metrics`` or ``readyz``. Admin
    queries are monitoring reads with the same generous budget, so they share
    that class rather than spending the ordinary ``read`` bucket. The mapping
    lives here, next to the routes it names, so ``auth.py`` keeps its frozen
    classification table.
    """

    if len(segments) >= 2 and segments[0] == "api" and segments[1] == "admin":
        return ["api", "metrics"]
    return segments


# -- entry point -----------------------------------------------------------


def admin_route(
    api: Any, request: Any, method: str, rest: list[str]
) -> Any:
    """Dispatch one ``/api/admin`` request. Only ``GET`` is served."""

    if not rest:
        raise _not_found()
    head = rest[0]

    if head == "summary":
        _only(rest, 1)
        if method.upper() != "GET":
            return _method_not_allowed_response("GET")
        return _summary(api, request)

    if head == "tenants":
        if len(rest) == 1:
            if method.upper() != "GET":
                return _method_not_allowed_response("GET")
            return _tenants(api, request)
        _only(rest, 2)
        if method.upper() != "GET":
            return _method_not_allowed_response("GET")
        return _tenant_detail(api, request, rest[1])

    if head == "runs":
        if len(rest) == 1:
            if method.upper() != "GET":
                return _method_not_allowed_response("GET")
            return _runs(api, request)
        _only(rest, 2)
        if method.upper() != "GET":
            return _method_not_allowed_response("GET")
        return _run(api, request, rest[1])

    if head == "audit":
        _only(rest, 1)
        if method.upper() != "GET":
            return _method_not_allowed_response("GET")
        return _audit(api, request)

    raise _not_found()


def _not_found() -> ChatError:
    return ChatError("not_found", status=404, message="Unknown endpoint.")


def _only(rest: list[str], length: int) -> None:
    if len(rest) != length:
        raise _not_found()



# -- payloads --------------------------------------------------------------


def _envelope(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Every admin body names its request id; the header carries it too."""

    body = {"ok": True, "request_id": current_request_id()}
    body.update(payload)
    return body


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _tenant_id() -> str:
    return current_tenant().tenant_id


def _summary(api: Any, request: Any) -> Any:
    tenant_id = _tenant_id()
    runs = _run_rows(api, tenant_id)
    usage = _usage_rows(api, tenant_id)
    return _json_response(
        200,
        _envelope(
            {
                "tenant_id": tenant_id,
                "summary": {
                    "tenant_id": tenant_id,
                    "plan_id": _plan_id(api, tenant_id),
                    "runs": _run_counts(runs),
                    "usage": _usage_totals(usage),
                    "spend": _spend_totals(usage, _spend_caps(api), _now()),
                },
            }
        ),
    )


def _tenants(api: Any, request: Any) -> Any:
    """The tenant index, scoped to the caller's tenant.

    There is no cross-tenant listing even for an operator: the contract says a
    caller may not read rows outside its tenant, and the only tenant this
    process admits is the one the credential resolved.
    """

    tenant_id = _tenant_id()
    runs = _run_rows(api, tenant_id)
    usage = _usage_rows(api, tenant_id)
    return _json_response(
        200,
        _envelope(
            {
                "tenants": [
                    {
                        "tenant_id": tenant_id,
                        "plan_id": _plan_id(api, tenant_id),
                        "runs": _run_counts(runs),
                        "usage": _usage_totals(usage),
                        "spend": _spend_totals(
                            usage, _spend_caps(api), _now()
                        ),
                    }
                ]
            }
        ),
    )


def _tenant_detail(api: Any, request: Any, raw_tenant: str) -> Any:
    tenant_id = _segment(raw_tenant)
    caller = _tenant_id()
    # A foreign (or merely invalid) id is the same 404 as a missing one, so the
    # detail route cannot be used to discover another tenant.
    if not is_valid_tenant_id(tenant_id) or tenant_id != caller:
        raise _not_found()

    runs = _run_rows(api, tenant_id)
    usage = _usage_rows(api, tenant_id)
    return _json_response(
        200,
        _envelope(
            {
                "tenant": {
                    "tenant_id": tenant_id,
                    "plan": _plan(api, tenant_id),
                    "runs": _run_counts(runs),
                    "usage": _usage_totals(usage),
                    "spend": _spend_totals(
                        usage, _spend_caps(api), _now()
                    ),
                    "keys": _key_presence(api),
                }
            }
        ),
    )


def _runs(api: Any, request: Any) -> Any:
    tenant_id = _tenant_id()
    limit = _int_param(request, "limit", _DEFAULT_PAGE, 1, PAGE_MAX)
    offset = _int_param(request, "offset", 0, 0, RUN_WINDOW)
    thread_id = request.query_one("thread_id") or request.query_one("thread")
    status = request.query_one("status") or None
    # A named thread is still resolved inside the caller's tenant; an unknown
    # one simply matches no rows.
    page = api.task_runner.list(
        tenant_id=tenant_id,
        thread_id=thread_id or None,
        status=status,
        limit=limit + 1,
        offset=offset,
    )
    has_more = len(page) > limit
    runs = page[:limit]
    return _json_response(
        200,
        _envelope(
            {
                "tenant_id": tenant_id,
                "runs": [run.to_admin_row() for run in runs],
                "next_offset": (offset + len(runs)) if has_more else None,
                "window": RUN_WINDOW,
            }
        ),
    )


def _run(api: Any, request: Any, raw_run_id: str) -> Any:
    run_id = _segment(raw_run_id)
    tenant_id = _tenant_id()
    run = api.task_runner.get(run_id, tenant_id=tenant_id)
    if run is None:
        # Missing and foreign are the same answer; see ``_tenant_detail``.
        raise _not_found()
    return _json_response(200, _envelope({"run": run.to_admin_row()}))


def _audit(api: Any, request: Any) -> Any:
    tenant_id = _tenant_id()
    limit = _int_param(request, "limit", _DEFAULT_PAGE, 1, PAGE_MAX)
    offset = _int_param(request, "offset", 0, 0, RUN_WINDOW)
    event = request.query_one("event") or None

    entries = _audit_entries(api)
    # Tenant scope first, then the event filter. A row without a tenant is not
    # assumed to be the owner's: it is excluded rather than exposed.
    entries = [
        entry
        for entry in entries
        if isinstance(entry, Mapping)
        and str(entry.get("tenant_id") or "") == tenant_id
        and (event is None or entry.get("event") == event)
    ]
    # Newest first, like the run index. ``entries`` reads oldest first.
    entries.reverse()
    page = entries[offset : offset + limit]
    has_more = len(entries) > offset + limit
    return _json_response(
        200,
        _envelope(
            {
                "tenant_id": tenant_id,
                "audit": [_audit_row(entry) for entry in page],
                "next_offset": (offset + len(page)) if has_more else None,
                "window": RUN_WINDOW,
            }
        ),
    )


def _audit_row(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Project only allow-listed metadata keys from one audit line."""

    return {
        key: entry[key]
        for key in _AUDIT_FIELDS
        if key in entry and entry[key] is not None
    }


# -- service adapters ------------------------------------------------------


def _run_rows(api: Any, tenant_id: str) -> list[dict[str, Any]]:
    store = getattr(getattr(api, "service", None), "store", None)
    if store is None:
        return []
    try:
        rows = store.list_runs(tenant_id=tenant_id, limit=RUN_WINDOW)
    except Exception:  # noqa: BLE001 - a read surface reports empty, not 500
        return []
    rows_out: list[dict[str, Any]] = [
        dict(row) for row in rows if isinstance(row, Mapping)
    ]
    return rows_out


def _run_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_status = {status: 0 for status in RUN_STATUSES}
    by_task_type: dict[str, int] = {}
    by_surface: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "")
        if status in by_status:
            by_status[status] += 1
        task_type = str(row.get("task_type") or "unknown")
        by_task_type[task_type] = by_task_type.get(task_type, 0) + 1
        surface = str(row.get("surface") or "unknown")
        by_surface[surface] = by_surface.get(surface, 0) + 1
    return {
        "total": len(rows),
        "window": RUN_WINDOW,
        "by_status": by_status,
        "by_task_type": by_task_type,
        "by_surface": by_surface,
    }


def _usage_rows(api: Any, tenant_id: str) -> list[dict[str, Any]]:
    """Persisted usage records for one tenant, normalised to plain dicts.

    The server writes usage rows with ``tenant_id``; a row that cannot name a
    tenant is skipped rather than attributed to the caller. No prompt, message,
    or provider body is part of a usage record, and this view only reads the
    counter fields it aggregates.
    """

    store = getattr(getattr(api, "service", None), "_usage_store", None)
    if store is None:
        return []
    source = getattr(store, "records", None)
    if source is None:
        return []
    try:
        raw = source() if callable(source) else source
    except Exception:  # noqa: BLE001 - accounting never breaks the read
        return []
    rows: list[dict[str, Any]] = []
    for item in raw or ():
        if isinstance(item, Mapping):
            data = dict(item)
        else:
            to_dict = getattr(item, "to_dict", None)
            data = dict(to_dict()) if callable(to_dict) else {}
        if str(data.get("tenant_id") or "") != tenant_id:
            continue
        rows.append(data)
    return rows


def _usage_totals(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    known_cost = 0.0
    unknown = 0
    for row in rows:
        input_tokens += _as_int(row.get("input_tokens"))
        output_tokens += _as_int(row.get("output_tokens"))
        total_tokens += _as_int(row.get("total_tokens"))
        cost = _as_float(row.get("estimated_cost_usd"))
        if row.get("cost_known") is True and cost is not None:
            known_cost += cost
        else:
            unknown += 1
    return {
        "records": len(rows),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "known_cost_usd": round(known_cost, 6),
        "unknown_price_records": unknown,
    }


def _spend_totals(
    rows: Sequence[Mapping[str, Any]],
    caps: Mapping[str, float | None],
    now: datetime,
) -> dict[str, Any]:
    day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    return {
        "day": _window_totals(rows, day_start, caps.get("day_usd")),
        "month": _window_totals(rows, month_start, caps.get("month_usd")),
    }


def _window_totals(
    rows: Sequence[Mapping[str, Any]], start: datetime, limit: float | None
) -> dict[str, Any]:
    used = 0.0
    unknown = 0
    counted = 0
    for row in rows:
        finished = _parse_timestamp(row.get("finished_at"))
        if finished is None or finished < start:
            continue
        counted += 1
        cost = _as_float(row.get("estimated_cost_usd"))
        if row.get("cost_known") is True and cost is not None:
            used += cost
        else:
            unknown += 1
    payload: dict[str, Any] = {
        "used_usd": round(used, 6),
        "limit_usd": limit,
        "records": counted,
        "unknown_price_records": unknown,
    }
    if limit is not None and limit > 0:
        payload["ratio"] = round(used / limit, 6)
    return payload


def _spend_caps(api: Any) -> dict[str, float | None]:
    guard = getattr(getattr(api, "service", None), "_spend", None)
    caps = getattr(guard, "caps", None)
    return {
        "day_usd": _as_float(getattr(caps, "day_usd", None)),
        "month_usd": _as_float(getattr(caps, "month_usd", None)),
    }


def _plan(api: Any, tenant_id: str) -> dict[str, Any] | None:
    entitlements = getattr(getattr(api, "service", None), "entitlements", None)
    if entitlements is None:
        return None
    try:
        plan = entitlements.plan_for(tenant_id=tenant_id)
    except Exception:  # noqa: BLE001 - configuration errors are not a 500
        return None
    snapshot = getattr(plan, "snapshot", None)
    if callable(snapshot):
        try:
            value = snapshot()
        except Exception:  # noqa: BLE001
            return None
        return dict(value) if isinstance(value, Mapping) else None
    return None


def _plan_id(api: Any, tenant_id: str) -> str | None:
    plan = _plan(api, tenant_id)
    plan_id = plan.get("plan_id") if plan else None
    return str(plan_id) if plan_id is not None else None


def _key_presence(api: Any) -> dict[str, bool]:
    """True/false presence flags only; no value is read into the payload.

    The provider key is described by its configuration object's boolean
    property when one exists. Declared skill secrets are reported by name
    because a name is configuration, never a credential value.
    """

    keys: dict[str, bool] = {}
    service = getattr(api, "service", None)
    provider = getattr(service, "_provider", None)
    config = getattr(provider, "config", None)
    if config is not None:
        present = getattr(config, "api_key_configured", None)
        if not isinstance(present, bool):
            try:
                present = bool(getattr(config, "api_key", None))
            except Exception:  # noqa: BLE001 - an absent property is "not set"
                present = False
        keys["provider_api_key"] = bool(present)
    secrets = getattr(service, "_secrets", None)
    if isinstance(secrets, Mapping):
        for name, value in secrets.items():
            keys[str(name)] = bool(value)
    return keys


def _audit_entries(api: Any) -> list[Any]:
    audit = getattr(getattr(api, "service", None), "_audit", None)
    if audit is None:
        audit = getattr(getattr(api, "health", None), "audit", None)
    entries = getattr(audit, "entries", None)
    if not callable(entries):
        return []
    try:
        return list(entries())
    except Exception:  # noqa: BLE001 - a torn audit file yields an empty index
        return []


# -- scalar helpers --------------------------------------------------------


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return max(0.0, number)


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse one usage ``finished_at``; ``None`` when unusable.

    Mirrors the spend guard's reading so a record counted by one is counted by
    the other. A naive stamp is UTC.
    """

    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

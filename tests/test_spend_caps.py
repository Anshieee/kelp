"""KEL-18 M3-C: persisted day/month spend caps.

The guardrail is derived from usage records on disk on every check, so the
cases here cover the promises that make it a cap rather than a dashboard:
boundary exactness, restart survival, a once-per-window warning, the two
unknown-price modes, and a refused turn that leaves no partial message and no
provider call.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from chat import ChatAPI, ChatService, ChatStore, Request
from chat.errors import ChatError
from chat.metrics import Metrics
from kernel.usage import (
    SPEND_CAP_DAY_ENV,
    SPEND_CAP_MONTH_ENV,
    SPEND_UNKNOWN_PRICE_MODE_ENV,
    SPEND_UNKNOWN_PRICE_USD_ENV,
    UNKNOWN_PRICE_BLOCK,
    UNKNOWN_PRICE_COUNT_AS,
    JsonlUsageStore,
    SpendCaps,
    SpendGuard,
    UsageRecord,
)
from kernel.provider import ProviderFailure
from openshell.audit import AuditLog

from .chatkit import MODEL, StubProvider, text_script

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def usage_record(
    cost: float | None,
    finished_at: datetime = NOW,
    *,
    turn_id: str = "turn_1",
) -> UsageRecord:
    stamp = finished_at.isoformat()
    return UsageRecord(
        turn_id=turn_id,
        session_id="thr_1",
        model=MODEL,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        estimated_cost_usd=cost,
        cost_known=cost is not None,
        usage_reported=True,
        steps=1,
        tool_calls=0,
        started_at=stamp,
        finished_at=stamp,
        duration_ms=10,
        outcome="stop",
    )


def seed(path: Path, costs: list[float | None], at: datetime = NOW) -> JsonlUsageStore:
    store = JsonlUsageStore(path)
    for index, cost in enumerate(costs):
        store.record(usage_record(cost, at, turn_id=f"turn_{index}"))
    return store


def guard(path: Path, caps: SpendCaps, **kwargs: Any) -> SpendGuard:
    return SpendGuard(
        JsonlUsageStore(path),
        caps,
        now_fn=kwargs.pop("now_fn", lambda: NOW),
        **kwargs,
    )


def samples(text: str) -> dict[str, float]:
    parsed: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        series, _, value = line.rpartition(" ")
        parsed[series] = float(value)
    return parsed


# -- windows and boundaries ------------------------------------------------


def test_day_cap_is_refused_exactly_at_the_limit(tmp_path: Path) -> None:
    caps = SpendCaps(day_usd=0.10)

    below = tmp_path / "below.jsonl"
    seed(below, [0.0999999])
    state = guard(below, caps).evaluate()
    assert state.day.used_usd == pytest.approx(0.0999999)
    assert state.blocked is False
    assert state.day.reached is False

    exact = tmp_path / "exact.jsonl"
    seed(exact, [0.10])
    blocked = guard(exact, caps).evaluate()
    assert blocked.day.used_usd == 0.10
    assert blocked.day.reached is True
    assert blocked.blocked is True
    assert blocked.block_window == "day"
    assert blocked.block_reason == "cap"


def test_day_window_excludes_yesterday_but_the_month_keeps_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "usage.jsonl"
    seed(path, [5.00], at=NOW - timedelta(days=1))
    seed(path, [7.00], at=datetime(2026, 9, 30, 23, 0, tzinfo=timezone.utc))

    state = guard(path, SpendCaps(day_usd=10.0, month_usd=10.0)).evaluate()
    assert state.day.used_usd == 0.0
    # Yesterday is still this month; the September record is in neither window.
    assert state.month.used_usd == 5.0
    assert state.blocked is False


def test_month_cap_counts_the_current_month_only(tmp_path: Path) -> None:
    path = tmp_path / "usage.jsonl"
    seed(path, [0.50], at=datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc))
    seed(path, [0.50], at=NOW)

    state = guard(path, SpendCaps(month_usd=1.0)).evaluate()
    assert state.month.used_usd == 1.0
    assert state.blocked is True
    assert state.block_window == "month"


def test_restart_recomputes_the_same_total_from_disk(tmp_path: Path) -> None:
    path = tmp_path / "usage.jsonl"
    caps = SpendCaps(day_usd=1.0)

    seed(path, [0.50])
    first = guard(path, caps)
    assert first.evaluate().day.used_usd == 0.50
    assert first.evaluate().blocked is False

    # A new process (new guard, no shared memory) sees the appended record too.
    seed(path, [0.50])
    second = guard(path, caps)
    state = second.evaluate()
    assert state.day.used_usd == 1.0
    assert state.blocked is True


# -- warning ---------------------------------------------------------------


def test_warning_fires_at_eighty_percent_and_only_once(tmp_path: Path) -> None:
    path = tmp_path / "usage.jsonl"
    state_path = tmp_path / "spend-state.json"
    caps = SpendCaps(day_usd=1.0)

    seed(path, [0.80])
    first = guard(path, caps, state_path=state_path)
    state = first.evaluate()
    assert state.day.ratio == pytest.approx(0.80)
    assert state.warnings == ("day",)
    assert state.blocked is False

    first.mark_warned(state)
    assert first.evaluate().warnings == ()

    # Restart inside the same window: the marker persisted, so no re-warning.
    seed(path, [0.05])
    restarted = guard(path, caps, state_path=state_path)
    assert restarted.evaluate().warnings == ()


def test_warning_marker_resets_in_a_new_window(tmp_path: Path) -> None:
    path = tmp_path / "usage.jsonl"
    state_path = tmp_path / "spend-state.json"
    caps = SpendCaps(day_usd=1.0)
    day_one = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    day_two = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)

    seed(path, [0.80], at=day_one)
    yesterday = guard(path, caps, state_path=state_path, now_fn=lambda: day_one)
    state = yesterday.evaluate()
    assert state.warnings == ("day",)
    yesterday.mark_warned(state)

    seed(path, [0.80], at=day_two)
    today = guard(path, caps, state_path=state_path, now_fn=lambda: day_two).evaluate()
    assert today.warnings == ("day",)


def test_warning_needs_a_configured_cap(tmp_path: Path) -> None:
    path = tmp_path / "usage.jsonl"
    seed(path, [100.0])
    state = guard(path, SpendCaps()).evaluate()
    assert state.warnings == ()
    assert state.blocked is False


# -- unknown prices --------------------------------------------------------


def test_unknown_price_block_mode_refuses_and_surfaces_the_mode(
    tmp_path: Path,
) -> None:
    path = tmp_path / "usage.jsonl"
    seed(path, [None])

    state = guard(path, SpendCaps(day_usd=10.0)).evaluate()
    assert state.unknown_price_mode == UNKNOWN_PRICE_BLOCK
    assert state.day.unknown_price_records == 1
    assert state.day.assumed_unknown_usd == 0.0
    assert state.blocked is True
    assert state.block_window == "day"
    assert state.block_reason == "unknown_price"


def test_unknown_price_count_as_charges_the_configured_amount(
    tmp_path: Path,
) -> None:
    path = tmp_path / "usage.jsonl"
    seed(path, [None])
    caps = SpendCaps(
        day_usd=0.50,
        unknown_price_mode=UNKNOWN_PRICE_COUNT_AS,
        unknown_price_usd=0.25,
    )
    state = guard(path, caps).evaluate()
    assert state.day.unknown_price_records == 1
    assert state.day.assumed_unknown_usd == pytest.approx(0.25)
    assert state.day.used_usd == pytest.approx(0.25)
    assert state.blocked is False

    seed(path, [None])
    capped = guard(path, caps).evaluate()
    assert capped.day.used_usd == pytest.approx(0.50)
    assert capped.blocked is True
    assert capped.block_reason == "cap"


def test_unknown_price_is_irrelevant_without_a_cap(tmp_path: Path) -> None:
    path = tmp_path / "usage.jsonl"
    seed(path, [None, None])
    state = guard(path, SpendCaps()).evaluate()
    assert state.blocked is False


def test_torn_usage_line_counts_as_unknown_not_free(tmp_path: Path) -> None:
    path = tmp_path / "usage.jsonl"
    seed(path, [0.01])
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"turn_id":"torn"\n')

    state = guard(path, SpendCaps(day_usd=1.0)).evaluate()
    assert state.day.unknown_price_records == 1
    assert state.blocked is True
    assert state.block_reason == "unknown_price"


def test_unreadable_usage_blocks_while_a_cap_is_configured(tmp_path: Path) -> None:
    directory = tmp_path / "usage.jsonl"
    directory.mkdir()
    store = JsonlUsageStore(directory)
    state = SpendGuard(store, SpendCaps(day_usd=1.0), now_fn=lambda: NOW).evaluate()
    assert state.blocked is True
    assert state.block_reason == "usage_unavailable"


# -- configuration ---------------------------------------------------------


def test_caps_parse_from_env_and_default_to_block(tmp_path: Path) -> None:
    caps = SpendCaps.from_env(
        {
            SPEND_CAP_DAY_ENV: "1.25",
            SPEND_CAP_MONTH_ENV: "10",
            SPEND_UNKNOWN_PRICE_MODE_ENV: "COUNT_AS",
            SPEND_UNKNOWN_PRICE_USD_ENV: "0.02",
        }
    )
    assert caps.day_usd == 1.25
    assert caps.month_usd == 10.0
    assert caps.unknown_price_mode == UNKNOWN_PRICE_COUNT_AS
    assert caps.unknown_price_usd == 0.02
    assert caps.enabled is True

    blank = SpendCaps.from_env({SPEND_CAP_DAY_ENV: "  "})
    assert blank.day_usd is None
    assert blank.month_usd is None
    assert blank.enabled is False
    assert blank.unknown_price_mode == UNKNOWN_PRICE_BLOCK


@pytest.mark.parametrize(
    "env",
    [
        {SPEND_CAP_DAY_ENV: "abc"},
        {SPEND_CAP_MONTH_ENV: "-1"},
        {SPEND_CAP_DAY_ENV: "nan"},
        {SPEND_UNKNOWN_PRICE_MODE_ENV: "ignore"},
        {SPEND_UNKNOWN_PRICE_USD_ENV: "-0.01"},
    ],
)
def test_bad_cap_configuration_fails_closed(env: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        SpendCaps.from_env(env)


# -- the turn path ---------------------------------------------------------


def make_capped_service(
    tmp_path: Path,
    caps: SpendCaps,
    costs: list[float | None],
    *,
    model: str = MODEL,
    now_fn: Any = lambda: NOW,
) -> tuple[ChatService, ChatStore, StubProvider, JsonlUsageStore, AuditLog, Metrics]:
    usage_path = tmp_path / "usage.jsonl"
    usage = seed(usage_path, costs)
    guard = SpendGuard(usage, caps, now_fn=now_fn)
    audit = AuditLog(tmp_path / "audit.jsonl")
    metrics = Metrics()
    store = ChatStore(tmp_path / "chat.db")
    provider = StubProvider([text_script("Hello")])
    service = ChatService(
        store=store,
        provider=provider,
        model=model,
        usage_store=usage,
        spend_guard=guard,
        audit=audit,
        metrics=metrics,
        # Deterministic turn timestamps so a turn's own usage lands in the
        # same window the guard is looking at.
        now=now_fn,
    )
    return service, store, provider, usage, audit, metrics


def test_capped_send_is_typed_non_retryable_and_leaves_no_partial_message(
    tmp_path: Path,
) -> None:
    service, store, provider, usage, audit, metrics = make_capped_service(
        tmp_path, SpendCaps(day_usd=1.0), [1.0]
    )
    thread = service.create_thread("Budget")
    before = service.get_thread(thread["id"])

    with pytest.raises(ChatError) as caught:
        asyncio.run(
            service.send(thread["id"], text="hi", client_turn_id="ct-1234abcd")
        )

    error = caught.value
    assert error.code == "spend_cap_reached"
    assert error.status == 402
    assert error.error["retryable"] is False
    assert error.error["details"]["window"] == "day"
    assert error.error["details"]["reason"] == "cap"

    # No provider call, no turn, no message, and the usage log is untouched.
    assert provider.requests == []
    assert store.latest_turn(thread["id"]) is None
    after = service.get_thread(thread["id"])
    assert after["messages"] == before["messages"] == []
    assert len(usage.records()) == 1

    blocked = audit.entries(event="spend_cap_blocked")
    assert len(blocked) == 1
    assert blocked[0]["outcome"] == "blocked"
    assert blocked[0]["extra"]["reason"] == "cap"
    assert (
        samples(metrics.render())['spend_cap_blocked_total{window="day"}'] == 1.0
    )


def test_unknown_price_block_mode_refuses_the_turn(tmp_path: Path) -> None:
    service, _, provider, _, audit, metrics = make_capped_service(
        tmp_path, SpendCaps(day_usd=5.0), [None]
    )
    thread = service.create_thread("Budget")
    with pytest.raises(ChatError) as caught:
        asyncio.run(
            service.send(thread["id"], text="hi", client_turn_id="ct-1234abcd")
        )
    assert caught.value.code == "spend_cap_reached"
    assert caught.value.error["details"]["reason"] == "unknown_price"
    assert caught.value.error["details"]["unknown_price_mode"] == UNKNOWN_PRICE_BLOCK
    assert provider.requests == []
    assert audit.entries(event="spend_cap_blocked")
    rendered = samples(metrics.render())
    assert rendered['spend_unknown_price_records{window="day"}'] == 1.0


def test_count_as_mode_admits_then_refuses_when_counted_spend_reaches_the_cap(
    tmp_path: Path,
) -> None:
    caps = SpendCaps(
        day_usd=0.25,
        unknown_price_mode=UNKNOWN_PRICE_COUNT_AS,
        unknown_price_usd=0.25,
    )
    service, _, _, usage, _, _ = make_capped_service(
        tmp_path, caps, [], model="vendor/unpriced-model"
    )
    thread = service.create_thread("Budget")
    payload, _ = asyncio.run(
        service.send(thread["id"], text="hi", client_turn_id="ct-1234abcd")
    )
    asyncio.run(service.wait_for_turn(payload["turn_id"]))
    # The turn itself was unpriced, so count_as charged it one assumed amount.
    assert usage.records()[-1]["estimated_cost_usd"] is None
    assert len(usage.records()) == 1

    with pytest.raises(ChatError) as caught:
        asyncio.run(
            service.send(thread["id"], text="again", client_turn_id="ct-5678efgh")
        )
    assert caught.value.code == "spend_cap_reached"
    assert caught.value.error["details"]["reason"] == "cap"
    assert caught.value.error["details"]["unknown_price_mode"] == UNKNOWN_PRICE_COUNT_AS
    assert len(usage.records()) == 1


def test_warning_is_logged_and_gauge_published_once(tmp_path: Path) -> None:
    service, _, _, _, audit, metrics = make_capped_service(
        tmp_path,
        SpendCaps(day_usd=1.0),
        [0.80],
    )
    thread = service.create_thread("Budget")
    for index, client_id in enumerate(("ct-1234abcd", "ct-5678efgh")):
        payload, _ = asyncio.run(
            service.send(thread["id"], text=f"hi {index}", client_turn_id=client_id)
        )
        asyncio.run(service.wait_for_turn(payload["turn_id"]))

    warnings = audit.entries(event="spend_cap_warning")
    assert len(warnings) == 1
    assert warnings[0]["extra"]["window"] == "day"
    rendered = samples(metrics.render())
    assert rendered['spend_cap_warnings_total{window="day"}'] == 1.0
    assert rendered['spend_cap_usd{window="day"}'] == pytest.approx(1.0)
    assert rendered['spend_used_usd{window="day"}'] >= 0.80
    assert 0.8 <= rendered['spend_cap_ratio{window="day"}'] < 1.0


def test_http_send_returns_402_and_the_typed_code(tmp_path: Path) -> None:
    service, _, _, _, _, _ = make_capped_service(
        tmp_path, SpendCaps(day_usd=1.0), [1.0]
    )
    api = ChatAPI(service, auth_token="tok", csrf_token="csrf")
    thread = service.create_thread("Budget")
    raw = json.dumps(
        {"client_turn_id": "ct-1234abcd", "text": "hi"}
    ).encode()
    response = api.handle(
        Request(
            "POST",
            f"/api/threads/{thread['id']}/turns",
            {},
            {
                "authorization": "Bearer tok",
                "x-csrf-token": "csrf",
                "content-type": "application/json",
            },
            raw,
        )
    )
    assert response.status == 402
    body = json.loads(response.body)
    assert body["error"]["code"] == "spend_cap_reached"
    assert body["error"]["retryable"] is False
    assert body["error"]["details"]["window"] == "day"


def test_retry_is_gated_by_the_same_cap(tmp_path: Path) -> None:
    usage_path = tmp_path / "usage.jsonl"
    usage = JsonlUsageStore(usage_path)
    guard = SpendGuard(usage, SpendCaps(day_usd=0.00001), now_fn=lambda: NOW)
    store = ChatStore(tmp_path / "chat.db")
    provider = StubProvider(
        [
            [ProviderFailure("provider_timeout", "timed out", retryable=True)],
            text_script("Hello"),
        ]
    )
    service = ChatService(
        store=store,
        provider=provider,
        model=MODEL,
        usage_store=usage,
        spend_guard=guard,
        now=lambda: NOW,
    )
    thread = service.create_thread("Budget")
    payload, _ = asyncio.run(
        service.send(thread["id"], text="hi", client_turn_id="ct-1234abcd")
    )
    asyncio.run(service.wait_for_turn(payload["turn_id"]))
    assert store.get_turn(payload["turn_id"])["reason"] == "error"
    assert len(provider.requests) == 1

    # Spend that arrived after the failed turn now exceeds the cap.
    usage.record(usage_record(0.50, turn_id="turn_extra"))
    with pytest.raises(ChatError) as caught:
        asyncio.run(service.retry(payload["turn_id"]))
    assert caught.value.code == "spend_cap_reached"
    assert caught.value.error["retryable"] is False
    assert len(provider.requests) == 1
    # The refused retry created no new turn.
    latest = store.latest_turn(thread["id"])
    assert latest["id"] == payload["turn_id"]


# -- gauge freshness (review M1) -------------------------------------------


def get_metrics_body(api: ChatAPI) -> dict[str, float]:
    response = api.handle(
        Request(
            "GET",
            "/api/metrics",
            {},
            {"authorization": "Bearer tok"},
            b"",
        )
    )
    assert response.status == 200
    return samples(response.body.decode())


def test_metrics_scrape_after_one_turn_shows_spend_without_a_second_send(
    tmp_path: Path,
) -> None:
    service, _, provider, _, _, metrics = make_capped_service(
        tmp_path, SpendCaps(day_usd=1.0), []
    )
    api = ChatAPI(service, auth_token="tok", csrf_token="csrf", metrics=metrics)
    thread = service.create_thread("Budget")
    payload, _ = asyncio.run(
        service.send(thread["id"], text="hi", client_turn_id="ct-1234abcd")
    )
    asyncio.run(service.wait_for_turn(payload["turn_id"]))
    # Exactly one provider call: the scrape below must not need a second send
    # to move the gauge.
    assert len(provider.requests) == 1

    rendered = get_metrics_body(api)
    used = rendered['spend_used_usd{window="day"}']
    assert used > 0.0
    # 10 priced input tokens at $0.30/M + 5 output at $0.90/M.
    assert used == pytest.approx(7.5e-06)
    assert rendered['spend_used_usd{window="month"}'] == pytest.approx(7.5e-06)


def test_metrics_scrape_republishes_from_records_without_a_new_turn(
    tmp_path: Path,
) -> None:
    """A scrape is current even when the settling turn ran in another process."""

    service, _, provider, usage, _, metrics = make_capped_service(
        tmp_path, SpendCaps(day_usd=1.0), [0.0]
    )
    api = ChatAPI(service, auth_token="tok", csrf_token="csrf", metrics=metrics)
    service.create_thread("Budget")
    # Prime the guard's file cache with the seeded record.
    assert get_metrics_body(api)['spend_used_usd{window="day"}'] == 0.0

    # Spend arrives on disk from outside this service: no turn, no gate, so
    # only the scrape can publish it.
    usage.record(usage_record(0.25, turn_id="turn_elsewhere"))
    rendered = get_metrics_body(api)
    assert rendered['spend_used_usd{window="day"}'] == pytest.approx(0.25)
    assert rendered['spend_cap_ratio{window="day"}'] == pytest.approx(0.25)
    assert provider.requests == []

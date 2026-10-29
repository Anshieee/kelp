"""Per-turn usage accounting and the M3 spend guardrail.

Every turn produces exactly one ``UsageRecord``: tokens in/out, estimated cost,
step and tool counts, duration, and outcome. Records are appended to a JSONL
file for the M3 cost guardrail. Unknown models keep ``estimated_cost_usd=None``
so the record is visibly incomplete instead of silently free.

:class:`SpendGuard` derives the day and month spend totals *from the persisted
records on every check*, never from an in-memory counter, so a restart cannot
reset a cap. It refuses nothing itself: it reports a :class:`SpendState` and the
chat layer turns a blocked state into a typed non-retryable error before a turn
(or a provider call) exists. At 80% of a window it asks the caller to warn once;
the marker that makes the warning "once" lives in a small sidecar file, so a
restart inside the same window does not warn again.

Unknown prices are explicit rather than free: a record with no
``estimated_cost_usd`` normally blocks new turns (``block``), because a spend
total that cannot be measured is not a cap. ``count_as`` instead charges each
such record a fixed configured amount, and the mode and the count are surfaced
in the state, the details, and the metrics.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

__all__ = [
    "DEFAULT_UNKNOWN_PRICE_USD",
    "InMemoryUsageStore",
    "JsonlUsageStore",
    "SPEND_CAP_DAY_ENV",
    "SPEND_CAP_MONTH_ENV",
    "SPEND_UNKNOWN_PRICE_MODE_ENV",
    "SPEND_UNKNOWN_PRICE_USD_ENV",
    "SpendCaps",
    "SpendGuard",
    "SpendState",
    "SpendUsageUnavailable",
    "SpendWindow",
    "state_payload",
    "UNKNOWN_PRICE_BLOCK",
    "UNKNOWN_PRICE_COUNT_AS",
    "UsageRecord",
    "UsageStore",
]

SPEND_CAP_DAY_ENV = "KELP_SPEND_CAP_DAY_USD"
SPEND_CAP_MONTH_ENV = "KELP_SPEND_CAP_MONTH_USD"
SPEND_UNKNOWN_PRICE_MODE_ENV = "KELP_SPEND_UNKNOWN_PRICE_MODE"
SPEND_UNKNOWN_PRICE_USD_ENV = "KELP_SPEND_UNKNOWN_PRICE_USD"

#: Fail closed: an unmeasurable window blocks rather than looking cheap.
UNKNOWN_PRICE_BLOCK = "block"
#: Charge each unknown-price record a fixed configured amount instead.
UNKNOWN_PRICE_COUNT_AS = "count_as"
UNKNOWN_PRICE_MODES = (UNKNOWN_PRICE_BLOCK, UNKNOWN_PRICE_COUNT_AS)
DEFAULT_UNKNOWN_PRICE_USD = 0.01
#: Warn at (and above) this fraction of a configured cap.
WARN_RATIO = 0.8
_WARNED_KEY = "warned"
_STATE_VERSION = 1


@dataclass(frozen=True)
class UsageRecord:
    turn_id: str
    session_id: str | None
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost_usd: float | None
    cost_known: bool
    usage_reported: bool
    steps: int
    tool_calls: int
    started_at: str
    finished_at: str
    duration_ms: int
    outcome: str
    #: Correlated HTTP request that produced the turn (KEL-14). ``None`` for
    #: turns started outside an HTTP request (CLI, tests, recovery).
    request_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class UsageStore(Protocol):
    def record(self, record: UsageRecord) -> None: ...


def _jsonl_paths(path: Path) -> list[Path]:
    """Active usage file plus retained rotated segments, oldest first.

    The retention pass renames the active file; a spend total must keep counting
    the segments inside the current day/month window, or a rotation would look
    like a spending reset.
    """

    pattern = re.compile(
        re.escape(path.name) + r"\.\d{8}T\d{12,}Z(?:\.\d{4,})?$"
    )
    try:
        entries = list(path.parent.iterdir())
    except OSError:
        return [path]
    segments = [entry for entry in entries if pattern.fullmatch(entry.name)]
    return sorted(segments, key=lambda entry: entry.name) + [path]


class InMemoryUsageStore:
    """Test/dev store."""

    def __init__(self) -> None:
        self.records: list[UsageRecord] = []
        self._lock = threading.Lock()

    def record(self, record: UsageRecord) -> None:
        with self._lock:
            self.records.append(record)

    def __len__(self) -> int:
        return len(self.records)


class JsonlUsageStore:
    """Append-only JSONL store. Safe for concurrent writers within one process."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def record(self, record: UsageRecord) -> None:
        payload = json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":"))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            is_new = not self.path.exists()
            with self.path.open("a", encoding="utf-8") as handle:
                if is_new:
                    try:
                        os.chmod(self.path, 0o600)
                    except OSError:
                        pass
                handle.write(payload + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def records(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in _jsonl_paths(self.path):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:  # a rotation raced this read
                continue
            rows.extend(json.loads(line) for line in lines if line.strip())
        return rows


# ---------------------------------------------------------------------------
# Spend guardrail (KEL-18, M3-C)
# ---------------------------------------------------------------------------


class SpendUsageUnavailable(RuntimeError):
    """The usage records could not be read, so spend cannot be measured."""


def _optional_usd(
    source: Mapping[str, str], name: str, default: float | None
) -> float | None:
    """Read a non-negative dollar amount. Blank/absent means ``default``.

    A malformed value raises instead of silently disabling a cap: a typo in the
    environment must not turn a spend ceiling into "unlimited".
    """

    raw = source.get(name)
    text = raw.strip() if isinstance(raw, str) else ""
    if text == "":
        return default
    try:
        value = float(text)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a non-negative dollar amount.") from None
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a non-negative dollar amount.")
    return value


@dataclass(frozen=True)
class SpendCaps:
    """The configured ceilings. ``None`` disables that window."""

    day_usd: float | None = None
    month_usd: float | None = None
    unknown_price_mode: str = UNKNOWN_PRICE_BLOCK
    unknown_price_usd: float = DEFAULT_UNKNOWN_PRICE_USD

    def __post_init__(self) -> None:
        for name, value in (
            (SPEND_CAP_DAY_ENV, self.day_usd),
            (SPEND_CAP_MONTH_ENV, self.month_usd),
        ):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError(f"{name} must be a non-negative dollar amount.")
        if self.unknown_price_mode not in UNKNOWN_PRICE_MODES:
            raise ValueError(
                f"{SPEND_UNKNOWN_PRICE_MODE_ENV} must be one of "
                f"{', '.join(UNKNOWN_PRICE_MODES)}."
            )
        if (
            not math.isfinite(self.unknown_price_usd)
            or self.unknown_price_usd < 0
        ):
            raise ValueError(
                f"{SPEND_UNKNOWN_PRICE_USD_ENV} must be a non-negative dollar amount."
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SpendCaps":
        source = os.environ if env is None else env
        mode = (
            source.get(SPEND_UNKNOWN_PRICE_MODE_ENV) or UNKNOWN_PRICE_BLOCK
        )
        mode = mode.strip().lower() if isinstance(mode, str) else UNKNOWN_PRICE_BLOCK
        return cls(
            day_usd=_optional_usd(source, SPEND_CAP_DAY_ENV, None),
            month_usd=_optional_usd(source, SPEND_CAP_MONTH_ENV, None),
            unknown_price_mode=mode,
            unknown_price_usd=_optional_usd(
                source, SPEND_UNKNOWN_PRICE_USD_ENV, DEFAULT_UNKNOWN_PRICE_USD
            ),
        )

    @property
    def enabled(self) -> bool:
        return self.day_usd is not None or self.month_usd is not None


@dataclass(frozen=True)
class SpendWindow:
    """One window's spend, counted from persisted records only."""

    name: str
    started_at: datetime
    limit_usd: float | None
    known_usd: float
    unknown_price_records: int
    assumed_unknown_usd: float

    @property
    def used_usd(self) -> float:
        return self.known_usd + self.assumed_unknown_usd

    @property
    def ratio(self) -> float | None:
        if self.limit_usd is None or self.limit_usd <= 0:
            # A zero cap is a hard stop; a ratio there is meaningless, and
            # division by zero must never reach a log line or a gauge.
            return None
        return self.used_usd / self.limit_usd

    @property
    def reached(self) -> bool:
        return self.limit_usd is not None and self.used_usd >= self.limit_usd

    @property
    def at_warning(self) -> bool:
        ratio = self.ratio
        return ratio is not None and ratio >= WARN_RATIO


@dataclass(frozen=True)
class SpendState:
    """The guardrail's answer for one point in time."""

    checked_at: datetime
    day: SpendWindow
    month: SpendWindow
    unknown_price_mode: str
    unknown_price_usd: float
    blocked: bool
    block_window: str | None
    block_reason: str | None
    warnings: tuple[str, ...]

    @property
    def windows(self) -> tuple[SpendWindow, SpendWindow]:
        return (self.day, self.month)

    @property
    def unknown_price_records(self) -> int:
        # The month total already contains the day's; summing both would
        # double-count the same turn.
        return max(self.day.unknown_price_records, self.month.unknown_price_records)


def _parse_timestamp(value: Any) -> datetime | None:
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


def _day_start(now: datetime) -> datetime:
    return datetime(now.year, now.month, now.day, tzinfo=timezone.utc)


def _month_start(now: datetime) -> datetime:
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


def _next_month_start(now: datetime) -> datetime:
    if now.month == 12:
        return datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    return datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)


class SpendGuard:
    """Compute day/month spend from persisted usage and decide the gate.

    ``store`` is any usage store with a ``path`` (JSONL) or ``records``
    (in-memory). Reading the JSONL directly keeps a torn append from silently
    shrinking the total: an unparseable line is counted as unknown-price spend
    and an unreadable file blocks while a cap is configured.
    """

    def __init__(
        self,
        store: Any,
        caps: SpendCaps | None = None,
        *,
        state_path: str | Path | None = None,
        now_fn: Any = None,
    ):
        self.store = store
        self.caps = caps or SpendCaps()
        self.state_path = Path(state_path) if state_path is not None else None
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._warned: dict[str, str] = {}
        self._cache_key: tuple[Any, ...] | None = None
        self._cache_rows: list[dict[str, Any]] = []
        self._cache_torn = 0

    # -- reading -----------------------------------------------------------

    def _file_path(self) -> Path | None:
        path = getattr(self.store, "path", None)
        return Path(path) if path is not None else None

    def _rows(self) -> tuple[list[dict[str, Any]], int]:
        """Rows plus the count of unreadable (torn) lines."""

        path = self._file_path()
        if path is None:
            return self._memory_rows(), 0
        paths = _jsonl_paths(path)
        stats: list[tuple[str, int, int]] = []
        for candidate in paths:
            try:
                stat = candidate.stat()
            except FileNotFoundError:
                continue
            except OSError as exc:  # pragma: no cover - permission/IO
                raise SpendUsageUnavailable(str(exc)) from None
            stats.append((str(candidate), stat.st_mtime_ns, stat.st_size))
        if not stats:
            return [], 0
        key = tuple(stats)
        with self._lock:
            if key == self._cache_key:
                return list(self._cache_rows), self._cache_torn
        rows: list[dict[str, Any]] = []
        torn = 0
        for candidate in paths:
            try:
                raw = candidate.read_text(encoding="utf-8")
            except FileNotFoundError:  # a rotation raced this read
                continue
            except OSError as exc:
                raise SpendUsageUnavailable(str(exc)) from None
            for line in raw.splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    torn += 1
                    continue
                if isinstance(row, Mapping):
                    rows.append(dict(row))
                else:  # a JSON value that is not an object is not a record
                    torn += 1
        with self._lock:
            self._cache_key = key
            self._cache_rows = rows
            self._cache_torn = torn
        return list(rows), torn

    def _memory_rows(self) -> list[dict[str, Any]]:
        records = getattr(self.store, "records", None)
        if callable(records):
            try:
                rows = records()
            except Exception as exc:  # noqa: BLE001 - fail closed below
                raise SpendUsageUnavailable(str(exc)) from None
        elif isinstance(records, list):
            rows = records
        else:
            return []
        normalised: list[dict[str, Any]] = []
        for row in rows or []:
            if isinstance(row, Mapping):
                normalised.append(dict(row))
            else:
                to_dict = getattr(row, "to_dict", None)
                if callable(to_dict):
                    value = to_dict()
                    if isinstance(value, Mapping):
                        normalised.append(dict(value))
        return normalised

    # -- warning markers ---------------------------------------------------

    def _load_warned(self) -> dict[str, str]:
        if self.state_path is None:
            return dict(self._warned)
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(payload, Mapping):
            return {}
        warned = payload.get(_WARNED_KEY)
        if not isinstance(warned, Mapping):
            return {}
        return {
            str(key): str(value)
            for key, value in warned.items()
            if isinstance(key, str) and isinstance(value, str)
        }

    def _save_warned(self, warned: Mapping[str, str]) -> None:
        if self.state_path is None:
            with self._lock:
                self._warned = dict(warned)
            return
        payload = json.dumps(
            {"version": _STATE_VERSION, _WARNED_KEY: dict(warned)},
            sort_keys=True,
            separators=(",", ":"),
        )
        temporary = self.state_path.with_name(self.state_path.name + ".tmp")
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
        except OSError:  # pragma: no cover - a warning marker is best effort
            try:
                temporary.unlink()
            except OSError:
                pass

    def mark_warned(self, state: SpendState) -> None:
        """Record that the once-per-window warning has been emitted."""

        if not state.warnings:
            return
        warned = self._load_warned()
        changed = False
        for window in state.windows:
            if window.name in state.warnings:
                warned[window.name] = window.started_at.isoformat()
                changed = True
        if changed:
            self._save_warned(warned)

    # -- evaluation --------------------------------------------------------

    def evaluate(self, now: datetime | None = None) -> SpendState:
        moment = (now or self._now()).astimezone(timezone.utc)
        day_start = _day_start(moment)
        month_start = _month_start(moment)
        caps = self.caps
        day_known = month_known = 0.0
        day_unknown = month_unknown = 0
        unavailable = False
        try:
            rows, torn = self._rows()
        except SpendUsageUnavailable:
            rows, torn, unavailable = [], 0, True
        for row in rows:
            finished = _parse_timestamp(row.get("finished_at"))
            if finished is None or finished < month_start:
                # A record that cannot be placed in a window cannot be
                # charged to one; torn lines below still count as unknown.
                continue
            cost = row.get("estimated_cost_usd")
            usable = (
                isinstance(cost, (int, float))
                and not isinstance(cost, bool)
                and math.isfinite(float(cost))
                and float(cost) >= 0
            )
            if usable:
                month_known += float(cost)
                if finished >= day_start:
                    day_known += float(cost)
            else:
                month_unknown += 1
                if finished >= day_start:
                    day_unknown += 1
        if torn:
            # The tail of an append is the newest record. Charge it to both
            # windows rather than let a crash hide spend.
            month_unknown += torn
            day_unknown += torn

        mode = caps.unknown_price_mode
        price = caps.unknown_price_usd

        def window(
            name: str,
            started: datetime,
            limit: float | None,
            known: float,
            unknown: int,
        ) -> SpendWindow:
            assumed = unknown * price if mode == UNKNOWN_PRICE_COUNT_AS else 0.0
            return SpendWindow(
                name=name,
                started_at=started,
                limit_usd=limit,
                known_usd=round(known, 8),
                unknown_price_records=unknown,
                assumed_unknown_usd=round(assumed, 8),
            )

        day = window("day", day_start, caps.day_usd, day_known, day_unknown)
        month = window("month", month_start, caps.month_usd, month_known, month_unknown)

        blocked = False
        block_window: str | None = None
        block_reason: str | None = None
        if caps.enabled:
            if unavailable:
                blocked, block_window, block_reason = True, "month", "usage_unavailable"
            else:
                for candidate in (day, month):
                    if candidate.limit_usd is None:
                        continue
                    if mode == UNKNOWN_PRICE_BLOCK and candidate.unknown_price_records > 0:
                        blocked = True
                        block_window = candidate.name
                        block_reason = "unknown_price"
                        break
                    if candidate.reached:
                        blocked = True
                        block_window = candidate.name
                        block_reason = "cap"
                        break

        warned = self._load_warned()
        pending: list[str] = []
        for candidate in (day, month):
            if candidate.limit_usd is None or not candidate.at_warning:
                continue
            if warned.get(candidate.name) != candidate.started_at.isoformat():
                pending.append(candidate.name)

        return SpendState(
            checked_at=moment,
            day=day,
            month=month,
            unknown_price_mode=mode,
            unknown_price_usd=price,
            blocked=blocked,
            block_window=block_window,
            block_reason=block_reason,
            warnings=tuple(pending),
        )

    # -- presentation ------------------------------------------------------

    def describe(self, now: datetime | None = None) -> dict[str, Any]:
        """A JSON-safe summary for logs, errors, and audit extras."""

        state = self.evaluate(now)
        return state_payload(state)


def state_payload(state: SpendState) -> dict[str, Any]:
    now = state.checked_at
    resets = {
        "day": (_day_start(now) + timedelta(days=1)).isoformat(),
        "month": _next_month_start(now).isoformat(),
    }
    windows: dict[str, Any] = {}
    for window in state.windows:
        windows[window.name] = {
            "limit_usd": window.limit_usd,
            "used_usd": round(window.used_usd, 8),
            "known_usd": window.known_usd,
            "unknown_price_records": window.unknown_price_records,
            "assumed_unknown_usd": window.assumed_unknown_usd,
            "ratio": None if window.ratio is None else round(window.ratio, 6),
            "reached": window.reached,
            "resets_at": resets.get(window.name),
        }
    return {
        "window": state.block_window,
        "reason": state.block_reason,
        "unknown_price_mode": state.unknown_price_mode,
        "unknown_price_usd": state.unknown_price_usd,
        "windows": windows,
    }

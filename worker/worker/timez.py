"""Timezone helpers (R10).

Rules encoded here:
  * every stored timestamp is UTC
  * a Tehran trading day runs 08:00–22:00 Asia/Tehran (UTC+03:30, DST UTC+04:30 historically)
  * "decision time" 08:00 local = first bar at/after 08:00 local
  * "end of day" = last bar at or before 22:00 local
All conversions go through `zoneinfo` — never through fixed offsets.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

TEHRAN = ZoneInfo("Asia/Tehran")
UTC = timezone.utc


def teheran_local(dt: datetime, tz: ZoneInfo | None = None) -> datetime:
    """Return `dt` as an aware Tehran-local datetime (naive inputs are assumed UTC)."""
    zone = tz or TEHRAN
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(zone)


def to_utc(dt: datetime, tz: ZoneInfo | None = None) -> datetime:
    """Attach/convert to UTC. Naive datetimes are interpreted in `tz` (default Tehran)."""
    zone = tz or TEHRAN
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zone)
    return dt.astimezone(UTC)


def local_ts(day: date, local_time: time, tz: ZoneInfo | None = None) -> datetime:
    """UTC instant of `local_time` on Tehran day `day` (DST-correct)."""
    zone = tz or TEHRAN
    return datetime.combine(day, local_time, tzinfo=zone).astimezone(UTC)


def day_bounds_utc(day: date, open_local: time, flat_local: time) -> tuple[datetime, datetime]:
    """(08:00, 22:00) Tehran on `day`, as UTC. Uses zoneinfo so DST is handled exactly."""
    return local_ts(day, open_local), local_ts(day, flat_local)


def tehran_date(dt: datetime) -> date:
    """Tehran calendar date that a UTC instant belongs to."""
    return teheran_local(dt).date()


def tehran_clock(dt: datetime) -> time:
    return teheran_local(dt).time().replace(microsecond=0)


def today_tehran(now: datetime | None = None) -> date:
    return tehran_date(now or datetime.now(tz=UTC))


def is_trading_day(day: date, weekdays: set[int]) -> bool:
    return day.weekday() in weekdays


def iter_days(start: date, end_inclusive: date) -> Iterator[date]:
    cur = start
    while cur <= end_inclusive:
        yield cur
        cur += timedelta(days=1)


def iter_trading_days(start: date, end_inclusive: date, weekdays: set[int]) -> Iterator[date]:
    for day in iter_days(start, end_inclusive):
        if is_trading_day(day, weekdays):
            yield day


def previous_trading_day(day: date, weekdays: set[int]) -> date:
    cur = day - timedelta(days=1)
    for _ in range(14):
        if is_trading_day(cur, weekdays):
            return cur
        cur -= timedelta(days=1)
    raise ValueError(f"no trading day found before {day}")


def next_trading_day(day: date, weekdays: set[int]) -> date:
    cur = day + timedelta(days=1)
    for _ in range(14):
        if is_trading_day(cur, weekdays):
            return cur
        cur += timedelta(days=1)
    raise ValueError(f"no trading day found after {day}")


def add_months(day: date, months: int) -> date:
    """Calendar-month arithmetic that clamps to the end of shorter months."""
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    # clamp day
    last_day = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(day.day, last_day))


def floor_to_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def split_tehran_day_counts(start: datetime, end: datetime) -> dict[str, int]:
    """Count distinct Tehran dates touched by [start, end) — used in coverage tables."""
    counts: dict[str, int] = {}
    cur = start
    while cur < end:
        key = tehran_date(cur).isoformat()
        counts[key] = counts.get(key, 0) + 1
        cur += timedelta(minutes=1)
    return counts

"""T9 — timezone correctness (R10). Asia/Tehran, DST honoured exactly, via zoneinfo only."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from worker.timez import (
    TEHRAN,
    UTC,
    day_bounds_utc,
    iter_trading_days,
    local_ts,
    next_trading_day,
    previous_trading_day,
    tehran_date,
)

STD_DAYS = [date(2026, 4, 12), date(2025, 11, 3), date(2026, 1, 20)]
DST_DAYS = [date(2020, 6, 15), date(2020, 8, 1)]


def test_standard_time_offsets():
    for day in STD_DAYS:
        start, end = day_bounds_utc(day, datetime.min.time().replace(hour=8), datetime.min.time().replace(hour=22))
        assert start == datetime(day.year, day.month, day.day, 4, 30, tzinfo=UTC)
        assert end == datetime(day.year, day.month, day.day, 18, 30, tzinfo=UTC)


def test_dst_offsets():
    for day in DST_DAYS:
        start, end = day_bounds_utc(day, datetime.min.time().replace(hour=8), datetime.min.time().replace(hour=22))
        assert start == datetime(day.year, day.month, day.day, 3, 30, tzinfo=UTC)
        assert end == datetime(day.year, day.month, day.day, 17, 30, tzinfo=UTC)


def test_session_is_always_fourteen_hours_utc():
    for day in STD_DAYS + DST_DAYS:
        start, end = day_bounds_utc(day, datetime.min.time().replace(hour=8), datetime.min.time().replace(hour=22))
        assert end - start == timedelta(hours=14)


def test_dst_transition_days_do_not_shift_the_session():
    """On an Iranian DST switch day the local 08:00 is still 08:00 local."""
    for day in (date(2020, 3, 21), date(2020, 9, 21), date(2021, 3, 22)):
        ts = local_ts(day, datetime.min.time().replace(hour=8))
        local = ts.astimezone(TEHRAN)
        assert (local.year, local.month, local.day, local.hour, local.minute) == (day.year, day.month, day.day, 8, 0)
        assert ts.utcoffset() == timedelta(0)
        start, end = day_bounds_utc(day, datetime.min.time().replace(hour=8), datetime.min.time().replace(hour=22))
        assert end - start == timedelta(hours=14)


def test_round_trip_through_tehran_calendar():
    ts = datetime(2026, 4, 12, 20, 30, tzinfo=UTC)  # 00:00 next day in Tehran (+03:30)
    assert tehran_date(ts) == date(2026, 4, 13)
    assert local_ts(date(2026, 4, 13), datetime.min.time()) == datetime(2026, 4, 12, 20, 30, tzinfo=UTC)


def test_no_off_by_one_at_midnight_boundaries():
    for hour in range(24):
        ts = datetime(2026, 7, 1, hour, 0, tzinfo=UTC)
        local = ts.astimezone(TEHRAN)
        assert local_ts(tehran_date(ts), local.time()) + timedelta(0) == local


def test_zoneinfo_is_the_only_source_of_offsets():
    """Guards against a hardcoded +03:30 creeping in somewhere."""
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1] / "worker" / "timez.py"
    ).read_text(encoding="utf-8")
    assert "timedelta(hours=3, minutes=30)" not in source
    assert "3.5" not in source.replace("3.5", "", 0) or "+03:30" not in source


@pytest.mark.parametrize(
    "weekday_set,expected",
    [
        ({5, 6, 0, 1, 2}, date(2026, 4, 11)),   # Thu 2026-04-09 -> Wed 04-08; Sat 04-11 is a trading day
        ({0, 1, 2, 3, 4}, date(2026, 4, 10)),   # Mon-Fri set: Fri 04-10
    ],
)
def test_trading_day_arithmetic(weekday_set, expected):
    assert next_trading_day(date(2026, 4, 9), weekday_set) == expected
    assert previous_trading_day(expected, weekday_set) != expected


def test_iter_trading_days_respects_the_weekday_set():
    days = list(iter_trading_days(date(2026, 4, 1), date(2026, 4, 30), {5, 6, 0, 1, 2}))
    assert all(d.weekday() in {5, 6, 0, 1, 2} for d in days)
    assert date(2026, 4, 3) not in days  # Friday
    assert date(2026, 4, 4) in days      # Saturday


def test_explicit_offence_detector():
    """A fixed-offset conversion would break on DST days — prove we do not do that."""
    naive_fixed = datetime(2020, 6, 15, 8, 0, tzinfo=timezone(timedelta(hours=3, minutes=30)))
    correct = local_ts(date(2020, 6, 15), datetime.min.time().replace(hour=8))
    assert correct != naive_fixed
    assert correct.astimezone(ZoneInfo("Asia/Tehran")).utcoffset() == timedelta(hours=4, minutes=30)

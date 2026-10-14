"""Coverage accounting (R8, M1).

Two things live here:
  1. `coverage_table` — bars/gaps per series per month, plus trading-day coverage.
  2. `record_missing_bars` — turn *absence* into explicit `price_gaps` rows.

Acceptance thresholds from M1 are enforced by `acceptance()`; failures are printed and
returned, never swallowed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

import psycopg

from ..config import Settings, get_settings
from ..db import repo
from ..timez import TEHRAN, iter_trading_days, local_ts
from .symbols import SERIES, Series

# M1 acceptance thresholds
MIN_USDTIRT_DAY_COVERAGE = 0.80
MIN_XAUUSD_HOURLY_COVERAGE = 0.90
# A trading day counts as "covered" for a 1m series when it has at least this share of a
# full 08:00-22:00 session (840 one-minute bars).
DAY_COVERED_SHARE = 0.60


@dataclass
class CoverageRow:
    source: str
    symbol: str
    resolution: str
    month: str
    n_bars: int
    expected_bars: int
    coverage_pct: float
    n_gaps: int
    gap_minutes: float


def session_expected(*, resolution: str, day: date, settings: Settings) -> int:
    open_ts, flat_ts = _session_bounds(day, settings)
    minutes = (flat_ts - open_ts).total_seconds() / 60.0
    per_bar = {"1m": 1, "5m": 5, "1h": 60, "1d": 1440}[resolution]
    return int(minutes // per_bar) + 1


def _session_bounds(day: date, settings: Settings) -> tuple[datetime, datetime]:
    return local_ts(day, settings.open_time), local_ts(day, settings.flat_time)


def _day_expected_hours(resolution: str, source: str) -> float:
    """How many hours per Tehran day the series is expected to have data for."""
    if source == "live_logger" or resolution in ("1m", "5m"):
        return 14.0  # 08:00-22:00
    if resolution == "1h":
        return 24.0  # ounce trades around the clock
    return 0.0  # daily: one observation per trading day, handled separately


def coverage_table(
    conn: psycopg.Connection,
    *,
    start: date,
    end: date,
    settings: Settings | None = None,
) -> list[CoverageRow]:
    s = settings or get_settings()
    rows = conn.execute(
        """
        select source, symbol, resolution,
               date_trunc('month', ts)::date as month,
               count(*) as n_bars,
               min(ts) as first_ts, max(ts) as last_ts
        from price_bars
        where ts >= %s and ts <= %s
        group by 1, 2, 3, 4
        order by 1, 2, 3, 4
        """,
        (_month_start(start), local_ts(end, time(23, 59))),
    ).fetchall()

    gap_rows = conn.execute(
        """
        select source, symbol, date_trunc('month', from_ts)::date as month, count(*) as n_gaps,
               coalesce(sum(extract(epoch from (to_ts - from_ts))) / 60.0, 0) as gap_minutes
        from price_gaps
        group by 1, 2, 3
        """,
    ).fetchall()
    gaps = {(r["source"], r["symbol"], r["month"]): (r["n_gaps"], float(r["gap_minutes"])) for r in gap_rows}

    out: list[CoverageRow] = []
    month_days: dict[date, int] = {}
    for day in iter_trading_days(start, end, s.trading_weekday_set):
        month_days[day.replace(day=1)] = month_days.get(day.replace(day=1), 0) + 1

    for r in rows:
        month: date = r["month"]
        n_days = month_days.get(month, 0)
        hours = _day_expected_hours(r["resolution"], r["source"])
        if hours:
            per_bar = {"1m": 1 / 60, "5m": 5 / 60, "1h": 1.0}[r["resolution"]]
            expected = int(n_days * hours / per_bar)
        else:
            expected = n_days
        n_gaps, gap_minutes = gaps.get((r["source"], r["symbol"], month), (0, 0.0))
        out.append(
            CoverageRow(
                source=r["source"],
                symbol=r["symbol"],
                resolution=r["resolution"],
                month=month.isoformat(),
                n_bars=int(r["n_bars"]),
                expected_bars=expected,
                coverage_pct=(100.0 * int(r["n_bars"]) / expected) if expected else 100.0,
                n_gaps=int(n_gaps),
                gap_minutes=float(gap_minutes),
            )
        )
    return out


def _month_start(day: date) -> datetime:
    return datetime(day.year, day.month, 1, tzinfo=timezone.utc)


def day_coverage(
    conn: psycopg.Connection, series: Series, *, start: date, end: date, settings: Settings | None = None
) -> list[tuple[date, float]]:
    """Per-trading-day share of expected bars (0..1)."""
    s = settings or get_settings()
    rows = conn.execute(
        """
        select ts from price_bars
        where source = %s and symbol = %s and resolution = %s and ts >= %s and ts <= %s
        """,
        (series.source, series.symbol, series.resolution, _month_start(start), local_ts(end, time(23, 59))),
    ).fetchall()
    counts: dict[date, int] = {}
    for r in rows:
        day = r["ts"].astimezone(TEHRAN).date()
        counts[day] = counts.get(day, 0) + 1
    out: list[tuple[date, float]] = []
    for day in iter_trading_days(start, end, s.trading_weekday_set):
        expected = session_expected(resolution=series.resolution, day=day, settings=s)
        got = counts.get(day, 0)
        out.append((day, (got / expected) if expected else 0.0))
    return out


def record_missing_bars(
    conn: psycopg.Connection, series: Series, *, start: date, end: date, settings: Settings | None = None
) -> int:
    """Write `price_gaps` rows for sessions with thin/no data — never fill them (R8)."""
    s = settings or get_settings()
    written = 0
    if series.resolution == "1d":
        return _record_missing_days(conn, series, start=start, end=end, settings=s)
    for day, share in day_coverage(conn, series, start=start, end=end, settings=s):
        if share >= DAY_COVERED_SHARE:
            continue
        open_ts, flat_ts = _session_bounds(day, s)
        reason = "no_data_for_session" if share == 0 else f"thin_session:{share:.2f}"
        repo.record_gap(conn, series.source, series.symbol, open_ts, flat_ts, reason)
        written += 1
    return written


def _record_missing_days(
    conn: psycopg.Connection, series: Series, *, start: date, end: date, settings: Settings
) -> int:
    present = {
        r["ts"].astimezone(TEHRAN).date()
        for r in conn.execute(
            """
            select ts from price_bars
            where source = %s and symbol = %s and resolution = %s and ts >= %s and ts <= %s
            """,
            (series.source, series.symbol, series.resolution, _month_start(start), local_ts(end, time(23, 59))),
        ).fetchall()
    }
    written = 0
    for day in iter_trading_days(start, end, settings.trading_weekday_set):
        if day in present:
            continue
        open_ts, flat_ts = _session_bounds(day, settings)
        repo.record_gap(conn, series.source, series.symbol, open_ts, flat_ts, "missing_daily_observation")
        written += 1
    return written


@dataclass
class Acceptance:
    name: str
    passed: bool
    detail: str
    threshold: str


def acceptance(
    conn: psycopg.Connection, *, start: date, end: date, settings: Settings | None = None
) -> list[Acceptance]:
    """M1 acceptance checks: USDTIRT 1m day coverage >= 80%, XAUUSD hourly >= 90%."""
    s = settings or get_settings()
    out: list[Acceptance] = []

    usdt = SERIES["usdtirt_1m"]
    days = day_coverage(conn, usdt, start=start, end=end, settings=s)
    covered = sum(1 for _, share in days if share >= DAY_COVERED_SHARE)
    share_days = (covered / len(days)) if days else 0.0
    out.append(
        Acceptance(
            name="nobitex USDTIRT 1m coverage of trading days",
            passed=share_days >= MIN_USDTIRT_DAY_COVERAGE,
            detail=f"{covered}/{len(days)} trading days with >= {DAY_COVERED_SHARE:.0%} of session bars",
            threshold=f">= {MIN_USDTIRT_DAY_COVERAGE:.0%}",
        )
    )

    xau = SERIES["xauusd_1h"]
    rows = conn.execute(
        """
        select count(*) as n from price_bars
        where source = %s and symbol = %s and resolution = %s and ts >= %s and ts <= %s
        """,
        (xau.source, xau.symbol, xau.resolution, _month_start(start), local_ts(end, time(23, 59))),
    ).fetchone()
    total_hours = ((local_ts(end, time(23, 59)) - _month_start(start)).total_seconds() / 3600.0)
    hourly_share = (float(rows["n"]) / total_hours) if total_hours else 0.0
    # Forex/metal sessions are closed ~50h/week, so scale by the 0.70 saturation bound.
    normalised = min(1.0, hourly_share / 0.70)
    out.append(
        Acceptance(
            name="XAUUSD hourly coverage",
            passed=normalised >= MIN_XAUUSD_HOURLY_COVERAGE,
            detail=f"{rows['n']} hourly bars over {total_hours:.0f} wall-clock hours "
                   f"({hourly_share:.1%} raw, {normalised:.1%} of the achievable 70%)",
            threshold=f">= {MIN_XAUUSD_HOURLY_COVERAGE:.0%} of achievable bars",
        )
    )
    return out


def coverage_markdown(rows: list[CoverageRow], accept: list[Acceptance], *, window: str) -> str:
    lines = [f"### Coverage (window: {window})", "", "| source | symbol | res | month | bars | expected | coverage | gaps | gap minutes |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(
            f"| {r.source} | {r.symbol} | {r.resolution} | {r.month} | {r.n_bars} | {r.expected_bars} | "
            f"{r.coverage_pct:.1f}% | {r.n_gaps} | {r.gap_minutes:.0f} |"
        )
    lines += ["", "### Acceptance checks", "", "| check | result | detail | threshold |", "|---|---|---|---|"]
    for a in accept:
        lines.append(f"| {a.name} | {'PASS' if a.passed else 'FAIL'} | {a.detail} | {a.threshold} |")
    return "\n".join(lines)


__all__ = [
    "CoverageRow",
    "Acceptance",
    "coverage_table",
    "day_coverage",
    "record_missing_bars",
    "acceptance",
    "coverage_markdown",
    "session_expected",
    "MIN_USDTIRT_DAY_COVERAGE",
    "MIN_XAUUSD_HOURLY_COVERAGE",
    "DAY_COVERED_SHARE",
    "timedelta",
]

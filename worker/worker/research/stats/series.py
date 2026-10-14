"""Measurement series + forward returns for event studies.

Honesty constraint (README §11): domestic gold has minute history only where the live logger
ran (or, in demo mode, synthetic data). So every event-study observation records WHICH series
measured it (`measure`), and the report always carries the per-measure coverage:

    domestic_gold_1m   best: a genuine domestic intraday gold return
    usdtirt_1m         free-market USD proxy (Nobitex) — a proxy for gold's direction
    xauusd_1h          global ounce, ±1h resolution
    daily_proxy        no intraday data: the day's 08:00->22:00 realized move (coarse)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import numpy as np
import psycopg

from ...timez import day_bounds_utc

log = logging.getLogger(__name__)

HORIZON_MINUTES = {"30m": 30, "1h": 60, "3h": 180}
ALL_HORIZONS = ("30m", "1h", "3h", "eod")


@dataclass(frozen=True)
class MeasurementSeries:
    source: str
    symbol: str
    resolution: str
    asset: str
    measure: str


CANDIDATES: tuple[MeasurementSeries, ...] = (
    MeasurementSeries("live_logger", "gold_18k_toman_per_gram", "1m", "gold", "domestic_gold_1m"),
    MeasurementSeries("synthetic", "gold_18k_toman_per_gram", "1m", "gold", "domestic_gold_1m"),
    MeasurementSeries("synthetic", "emami_coin_toman", "1m", "coin", "domestic_coin_1m"),
    MeasurementSeries("nobitex", "USDTIRT", "1m", "usdtirt", "usdtirt_1m"),
    MeasurementSeries("synthetic", "USDTIRT", "1m", "usdtirt", "usdtirt_1m"),
    MeasurementSeries("yfinance", "XAUUSD", "1h", "xauusd", "xauusd_1h"),
    MeasurementSeries("synthetic", "XAUUSD", "1h", "xauusd", "xauusd_1h"),
)


@dataclass
class DayMinutes:
    day: date
    series: MeasurementSeries
    ts: np.ndarray          # epoch seconds, sorted
    close: np.ndarray       # prices

    def index_at_or_after(self, when: datetime) -> int | None:
        if len(self.ts) == 0:
            return None
        pos = int(np.searchsorted(self.ts, when.timestamp(), side="left"))
        return pos if pos < len(self.ts) else None

    def index_at_or_before(self, when: datetime) -> int | None:
        if len(self.ts) == 0:
            return None
        pos = int(np.searchsorted(self.ts, when.timestamp(), side="right")) - 1
        return pos if pos >= 0 else None


def pick_series(conn: psycopg.Connection, *, start: date, end: date) -> list[MeasurementSeries]:
    """Series that actually have data in the window, most informative first."""
    out: list[MeasurementSeries] = []
    for cand in CANDIDATES:
        row = conn.execute(
            """
            select count(*) as n from price_bars
            where source = %s and symbol = %s and resolution = %s
              and ts >= %s and ts <= %s
            """,
            (cand.source, cand.symbol, cand.resolution, datetime(start.year, start.month, start.day, tzinfo=None).replace(tzinfo=__import__("datetime").timezone.utc),
             datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=__import__("datetime").timezone.utc)),
        ).fetchone()
        if row and int(row["n"]) > 0:
            out.append(cand)
    return out


def load_day_minutes(
    conn: psycopg.Connection, series: MeasurementSeries, day: date, *, open_local, flat_local, pad_hours: int = 0
) -> DayMinutes | None:
    open_ts, flat_ts = day_bounds_utc(day, open_local, flat_local)
    start = open_ts - timedelta(hours=pad_hours)
    end = flat_ts + timedelta(minutes=1)
    rows = conn.execute(
        """
        select ts, close from price_bars
        where source = %s and symbol = %s and resolution = %s and ts >= %s and ts <= %s
        order by ts asc
        """,
        (series.source, series.symbol, series.resolution, start, end),
    ).fetchall()
    if not rows:
        return None
    ts = np.array([r["ts"].timestamp() for r in rows], dtype=float)
    close = np.array([float(r["close"]) for r in rows], dtype=float)
    return DayMinutes(day=day, series=series, ts=ts, close=close)


def forward_return_bps(day_minutes: DayMinutes, when: datetime, horizon_minutes: int, *, flat_ts: datetime) -> float | None:
    """Log-return (bps) from the first bar at/after `when` to the bar at/after `when+h`,
    capped at the session end (22:00 Tehran). None when either end is unavailable."""
    entry_idx = day_minutes.index_at_or_after(when)
    if entry_idx is None:
        return None
    target = min(when + timedelta(minutes=horizon_minutes), flat_ts)
    exit_idx = day_minutes.index_at_or_after(target)
    if exit_idx is None:
        exit_idx = day_minutes.index_at_or_before(flat_ts)
    if exit_idx is None or exit_idx <= entry_idx:
        return None
    entry = day_minutes.close[entry_idx]
    exit_price = day_minutes.close[exit_idx]
    if entry <= 0 or exit_price <= 0:
        return None
    return math.log(exit_price / entry) * 1e4


@dataclass
class EventReturn:
    day: date
    channel_id: int
    username: str
    direction: str
    kind: str
    horizon: str
    gross_bps: float
    net_bps: float
    cost_bps: float
    measure: str
    asset: str
    message_id: int
    published_at: datetime


def signed(gross_bps: float, direction: str) -> float:
    """Signed by the signal's direction: up-signals want positive returns, down-signals
    want negative ones (so a correct down call yields a positive lift)."""
    if direction == "up":
        return gross_bps
    if direction == "down":
        return -gross_bps
    return 0.0


__all__ = [
    "MeasurementSeries",
    "DayMinutes",
    "EventReturn",
    "CANDIDATES",
    "ALL_HORIZONS",
    "HORIZON_MINUTES",
    "pick_series",
    "load_day_minutes",
    "forward_return_bps",
    "signed",
]

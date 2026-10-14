"""Decision / end-of-day price resolution (R10).

A day-level call needs two numbers: the price at 08:00 (the decision) and the price at 22:00
(the outcome). Which series supplies them depends on what history actually exists:

  * if minute-level domestic data exists (live logger, or synthetic demo data) it is used —
    decision = first bar at/after 08:00 Tehran, EOD = last bar at/before 22:00 Tehran
  * otherwise the daily series is used — decision = last daily bar published before the
    session started (i.e. yesterday's close), EOD = today's daily close

The chosen method is returned alongside the price and stored in the state, so a report can
always say which was used. Missing data yields `None` — never an interpolated number (R8).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .asof import ReadContext

# Series candidates in priority order: (source, symbol, resolution)
DOMESTIC_GOLD: list[tuple[str, str, str]] = [
    ("live_logger", "gold_18k_toman_per_gram", "1m"),
    ("synthetic", "gold_18k_toman_per_gram", "1m"),
    ("tgju", "gold_18k_toman_per_gram", "1d"),
    ("synthetic", "gold_18k_toman_per_gram", "1d"),
]
DOMESTIC_COIN: list[tuple[str, str, str]] = [
    ("live_logger", "emami_coin_toman", "1m"),
    ("synthetic", "emami_coin_toman", "1m"),
    ("tgju", "emami_coin_toman", "1d"),
    ("synthetic", "emami_coin_toman", "1d"),
]
USDTIRT: list[tuple[str, str, str]] = [
    ("nobitex", "USDTIRT", "1m"),
    ("synthetic", "USDTIRT", "1m"),
    ("tgju", "usd_free_market_toman", "1d"),
]
XAUUSD: list[tuple[str, str, str]] = [
    ("yfinance", "XAUUSD", "1h"),
    ("synthetic", "XAUUSD", "1h"),
    ("synthetic", "XAUUSD", "1d"),
]


@dataclass
class PricePoint:
    price: float | None
    source: str | None
    resolution: str | None
    ts: datetime | None
    method: str
    detail: str = ""

    @property
    def available(self) -> bool:
        return self.price is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "price": self.price,
            "source": self.source,
            "resolution": self.resolution,
            "ts": self.ts.isoformat() if self.ts else None,
            "method": self.method,
        }


def _intraday_point(ctx: ReadContext, candidates: list[tuple[str, str, str]], at: datetime, *, after: bool) -> PricePoint | None:
    for source, symbol, resolution in candidates:
        if resolution == "1d":
            continue
        if after:
            row = ctx.conn.execute(
                """
                select ts, close, source, resolution from price_bars
                where source = %s and symbol = %s and resolution = %s and ts >= %s and ts <= %s
                order by ts asc limit 1
                """,
                (source, symbol, resolution, at, min(ctx.as_of, at + timedelta(hours=3))),
            ).fetchone()
        else:
            row = ctx.conn.execute(
                """
                select ts, close, source, resolution from price_bars
                where source = %s and symbol = %s and resolution = %s and ts <= %s and ts >= %s
                order by ts desc limit 1
                """,
                (source, symbol, resolution, min(ctx.as_of, at), at - timedelta(hours=14)),
            ).fetchone()
        if row:
            return PricePoint(
                price=float(row["close"]), source=row["source"], resolution=row["resolution"], ts=row["ts"],
                method="intraday_first_at_or_after" if after else "intraday_last_at_or_before",
            )
    return None


def _daily_before(ctx: ReadContext, candidates: list[tuple[str, str, str]], at: datetime) -> PricePoint | None:
    for source, symbol, resolution in candidates:
        if resolution != "1d":
            continue
        row = ctx.conn.execute(
            """
            select ts, close, source, resolution from price_bars
            where source = %s and symbol = %s and resolution = %s and ts <= %s
            order by ts desc limit 1
            """,
            (source, symbol, resolution, min(ctx.as_of, at)),
        ).fetchone()
        if row:
            return PricePoint(
                price=float(row["close"]), source=row["source"], resolution=row["resolution"], ts=row["ts"],
                method="daily_previous_close",
            )
    return None


def _daily_on(ctx: ReadContext, candidates: list[tuple[str, str, str]], session_end: datetime) -> PricePoint | None:
    for source, symbol, resolution in candidates:
        if resolution != "1d":
            continue
        row = ctx.conn.execute(
            """
            select ts, close, source, resolution from price_bars
            where source = %s and symbol = %s and resolution = %s and ts <= %s and ts >= %s
            order by ts desc limit 1
            """,
            (source, symbol, resolution, min(ctx.as_of, session_end), session_end - timedelta(hours=30)),
        ).fetchone()
        if row:
            return PricePoint(
                price=float(row["close"]), source=row["source"], resolution=row["resolution"], ts=row["ts"],
                method="daily_session_close",
            )
    return None


def decision_price(ctx: ReadContext, day: date, candidates: list[tuple[str, str, str]], *, at: datetime) -> PricePoint:
    point = _intraday_point(ctx, candidates, at, after=True)
    if point:
        return point
    point = _daily_before(ctx, candidates, at)
    if point:
        return point
    return PricePoint(price=None, source=None, resolution=None, ts=None, method="unavailable",
                      detail=f"no price for {day} {at.isoformat()}")


def eod_price(ctx: ReadContext, day: date, candidates: list[tuple[str, str, str]], *, at: datetime) -> PricePoint:
    point = _intraday_point(ctx, candidates, at, after=False)
    if point:
        return point
    point = _daily_on(ctx, candidates, at)
    if point:
        return point
    return PricePoint(price=None, source=None, resolution=None, ts=None, method="unavailable",
                      detail=f"no end-of-day price for {day}")


def session_low_high(
    ctx: ReadContext, candidates: list[tuple[str, str, str]], *, start: datetime, end: datetime
) -> tuple[float | None, float | None, str | None]:
    """Intraday low/high for stop-loss simulation. Returns (low, high, source)."""
    for source, symbol, resolution in candidates:
        if resolution == "1d":
            continue
        row = ctx.conn.execute(
            """
            select min(low) as low, max(high) as high from price_bars
            where source = %s and symbol = %s and resolution = %s and ts >= %s and ts <= %s
            """,
            (source, symbol, resolution, start, min(ctx.as_of, end)),
        ).fetchone()
        if row and row["low"] is not None:
            return float(row["low"]), float(row["high"]), source
    return None, None, None


__all__ = [
    "PricePoint",
    "decision_price",
    "eod_price",
    "session_low_high",
    "DOMESTIC_GOLD",
    "DOMESTIC_COIN",
    "USDTIRT",
    "XAUUSD",
]

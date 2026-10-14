"""State builder (§5) — 100% code-computed numbers, compact ENGLISH, zero raw Persian.

Every value here comes from `ReadContext`, i.e. only from rows whose event time is <= the
decision instant. The builder is deterministic: same inputs -> byte-identical JSON (T4).

Structure (matches §5 exactly, plus `history.flat_band_bps` which the question text refers to):

    day / decision_price / moves_since_open / history / signals / market
"""

from __future__ import annotations

import logging
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from ..config import Settings
from ..timez import TEHRAN, local_ts, previous_trading_day
from .asof import ReadContext
from . import prices_view as pv

log = logging.getLogger(__name__)

SNIPPET_LIMIT = 8
SNIPPET_CHARS = 220


@dataclass
class DayPrices:
    day: date
    gold_decision: pv.PricePoint
    gold_eod: pv.PricePoint
    coin_decision: pv.PricePoint
    usdt_decision: pv.PricePoint
    usdt_eod: pv.PricePoint

    def as_dict(self) -> dict[str, Any]:
        return {
            "gold_decision": self.gold_decision.as_dict(),
            "gold_eod": self.gold_eod.as_dict(),
            "coin_decision": self.coin_decision.as_dict(),
            "usdt_decision": self.usdt_decision.as_dict(),
            "usdt_eod": self.usdt_eod.as_dict(),
        }


def _pct(now: float | None, then: float | None) -> float | None:
    if now is None or then in (None, 0):
        return None
    return (now / then - 1.0) * 100.0


def _bps(now: float | None, then: float | None) -> float | None:
    if now is None or then in (None, 0):
        return None
    return (now / then - 1.0) * 1e4


def _r(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def day_prices(ctx: ReadContext, day: date, settings: Settings) -> DayPrices:
    open_ts, flat_ts = ctx.tehran_session(day)
    return DayPrices(
        day=day,
        gold_decision=pv.decision_price(ctx, day, pv.DOMESTIC_GOLD, at=open_ts),
        gold_eod=pv.eod_price(ctx, day, pv.DOMESTIC_GOLD, at=flat_ts),
        coin_decision=pv.decision_price(ctx, day, pv.DOMESTIC_COIN, at=open_ts),
        usdt_decision=pv.decision_price(ctx, day, pv.USDTIRT, at=open_ts),
        usdt_eod=pv.eod_price(ctx, day, pv.USDTIRT, at=flat_ts),
    )


def historic_day_moves(ctx: ReadContext, day: date, settings: Settings, *, window: int = 7) -> dict[str, Any]:
    """EOD-vs-open moves for the previous trading days that are FULLY visible at ctx.as_of."""
    weekdays = settings.trading_weekday_set
    out: dict[str, Any] = {"gold": [], "usdtirt": []}
    cursor = previous_trading_day(day, weekdays)
    tries = 0
    while len(out["gold"]) < window and tries < window * 3:
        open_ts, flat_ts = ctx.tehran_session(cursor)
        if flat_ts <= ctx.as_of:
            prices = day_prices(ctx, cursor, settings)
            gold = _bps(prices.gold_eod.price, prices.gold_decision.price)
            usdt = _bps(prices.usdt_eod.price, prices.usdt_decision.price)
            if gold is not None:
                out["gold"].append(gold)
            if usdt is not None:
                out["usdtirt"].append(usdt)
        cursor = previous_trading_day(cursor, weekdays)
        tries += 1
    return out


def _signal_window_stats(
    ctx: ReadContext, day: date, *, now: datetime, settings: Settings
) -> dict[str, Any]:
    # The signal window is the 3 hours BEFORE the checkpoint, deliberately including
    # pre-open chatter: overnight/early-morning channel traffic is exactly what a 08:00
    # decision should be allowed to see. (Nothing after `now` is ever visible — R3.)
    _ = day
    since_3h = now - timedelta(hours=3)
    rows = ctx.messages(since=since_3h)
    extracted = [r for r in rows if r.get("extraction_json") and r.get("extraction_status") == "ok"]

    def counted(rows_: list[dict[str, Any]]) -> dict[str, int]:
        c = Counter()
        for r in rows_:
            ej = r["extraction_json"]
            direction = ej.get("direction")
            if direction in ("up", "down"):
                c[direction] += 1
        return {"bull": c["up"], "bear": c["down"], "net": c["up"] - c["down"]}

    horizons = {
        "last_30m": now - timedelta(minutes=30),
        "last_1h": now - timedelta(hours=1),
        "last_3h": since_3h,
    }
    bull_minus_bear: dict[str, int] = {}
    for name, since in horizons.items():
        subset = [r for r in extracted if r["published_at"] >= since]
        bull_minus_bear[name] = counted(subset)["net"]

    intensities = [float(r["extraction_json"].get("intensity", 0)) for r in extracted]
    kinds = Counter(r["extraction_json"].get("kind", "opinion") for r in extracted)

    # "k channels agree in direction within 30 minutes" — sweep k and report the largest.
    agreeing = _max_agreeing_channels(extracted, now=now, lookback=timedelta(hours=3), window=timedelta(minutes=30))

    # quoted-price deviation vs the real price at the same timestamp
    worst = _top_quoted_deviation(ctx, extracted)

    ranked = sorted(
        extracted,
        key=lambda r: (
            float(r["extraction_json"].get("intensity", 0)) * float(r["extraction_json"].get("confidence", 0)),
            r["published_at"],
        ),
        reverse=True,
    )
    snippets: list[str] = []
    for r in ranked[:SNIPPET_LIMIT]:
        text = str(r["extraction_json"].get("english_text", "")).strip().replace("\n", " ")
        if text:
            snippets.append(f"[{r['username']} @ {r['published_at'].strftime('%H:%M')}Z] {text[:SNIPPET_CHARS]}")

    return {
        "n_messages_last_3h": len(rows),
        "n_extracted_last_3h": len(extracted),
        "n_channels_active": len({r["channel_id"] for r in rows}),
        "bullish_minus_bearish_count": bull_minus_bear,
        "mean_intensity": _r(statistics.fmean(intensities), 2) if intensities else 0.0,
        "high_intensity_count": sum(1 for i in intensities if i >= 4),
        "kind_counts": {k: int(kinds.get(k, 0)) for k in ("news", "rumor", "price_quote", "opinion")},
        "top_quoted_deviation": worst,
        "agreeing_channel_count_within_30m": agreeing,
        "lead_channels_today": _leaderboard_names(ctx, day, "lead"),
        "lag_channels_today": _leaderboard_names(ctx, day, "lag"),
        "recent_signal_snippets": snippets,
    }


def _max_agreeing_channels(
    rows: list[dict[str, Any]], *, now: datetime, lookback: timedelta, window: timedelta
) -> int:
    """Largest k such that k distinct channels pushed the same direction within `window`."""
    events = [
        (r["published_at"], r["username"], r["extraction_json"].get("direction"))
        for r in rows
        if r["extraction_json"].get("direction") in ("up", "down")
        and r["published_at"] >= now - lookback
    ]
    events.sort()
    best = 0
    for i, (start, _, direction) in enumerate(events):
        channels = set()
        for ts, username, d in events[i:]:
            if ts - start > window:
                break
            if d == direction:
                channels.add(username)
        best = max(best, len(channels))
    return best


def _top_quoted_deviation(ctx: ReadContext, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    asset_pct: dict[str, list[float]] = {}
    for r in rows:
        ej = r["extraction_json"]
        quoted = ej.get("quoted_price")
        asset = ej.get("quoted_asset") or "unknown"
        if not quoted or asset == "xauusd_ounce":
            continue
        real = _real_price_at(ctx, asset, r["published_at"])
        if not real:
            continue
        asset_pct.setdefault(asset, []).append((float(quoted) / real - 1.0) * 100.0)
    if not asset_pct:
        return None
    asset, pcts = max(asset_pct.items(), key=lambda kv: abs(statistics.fmean(kv[1])))
    return {"asset": asset, "pct_vs_real_price": _r(statistics.fmean(pcts), 2), "n_quotes": len(pcts)}


def _real_price_at(ctx: ReadContext, asset: str, ts: datetime) -> float | None:
    candidates = {
        "emami_coin": pv.DOMESTIC_COIN,
        "abshodeh": pv.DOMESTIC_GOLD,
        "gold_18k_toman_per_gram": pv.DOMESTIC_GOLD,
        "usdtirt": pv.USDTIRT,
        "usd_free_market": pv.USDTIRT,
    }.get(asset)
    if candidates is None:
        return None
    for source, symbol, resolution in candidates:
        row = ctx.conn.execute(
            """
            select close from price_bars
            where source = %s and symbol = %s and resolution = %s and ts <= %s
            order by ts desc limit 1
            """,
            (source, symbol, resolution, min(ctx.as_of, ts)),
        ).fetchone()
        if row:
            return float(row["close"])
    return None


def _leaderboard_names(ctx: ReadContext, day: date, label: str, *, limit: int = 5) -> list[str]:
    """Leaderboard labels computed STRICTLY before the decision day (no look-ahead)."""
    rows = ctx.conn.execute(
        """
        select c.username
        from channel_scores cs
        join channels c on c.id = cs.channel_id
        where cs.horizon = 'eod' and cs.label = %s and cs.asof_date < %s
        order by cs.asof_date desc, cs.channel_id
        limit %s
        """,
        (label, day, limit),
    ).fetchall()
    seen: list[str] = []
    for r in rows:
        if r["username"] not in seen:
            seen.append(r["username"])
    return seen


def build_state(
    ctx: ReadContext,
    day: date,
    *,
    checkpoint_local: Any,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Build the compact English state object for one decision checkpoint."""
    settings = settings or ctx.settings
    now = local_ts(day, checkpoint_local)
    if now > ctx.as_of:
        now = ctx.as_of  # never look past the context bound
    open_ts, flat_ts = ctx.tehran_session(day)
    prices = day_prices(ctx, day, settings)
    moves = {
        "usdtirt_pct_08_to_now": _r(_pct(_price_now(ctx, pv.USDTIRT, now), prices.usdt_decision.price)),
        "xauusd_pct_08_to_now": _r(_pct(_price_now(ctx, pv.XAUUSD, now), _ounce_open(ctx, day, now))),
    }
    history = historic_day_moves(ctx, day, settings)
    gold_hist = history["gold"]
    usdt_hist = history["usdtirt"]
    signals = _signal_window_stats(ctx, day, now=now, settings=settings)
    cost_table = settings.cost_bps

    state = {
        "day": {"date": day.isoformat(), "session": "08:00-22:00 Tehran"},
        "decision_price": {
            "gold_18k_toman_per_gram": _r(prices.gold_decision.price, 0),
            "emami_coin_toman": _r(prices.coin_decision.price, 0),
            "decision_time_local": checkpoint_local.strftime("%H:%M"),
        },
        "moves_since_open": moves,
        "history": {
            "gold_eod_vs_08_bps_yesterday": _r(gold_hist[0] if gold_hist else None, 1),
            "gold_eod_vs_08_bps_7d_mean": _r(statistics.fmean(gold_hist), 1) if gold_hist else None,
            "usdtirt_eod_vs_08_bps_7d_mean": _r(statistics.fmean(usdt_hist), 1) if usdt_hist else None,
            "flat_band_bps": round(float(settings.flat_band_bps), 1),
        },
        "signals": signals,
        "market": {
            "xauusd_gap_from_daily_open_pct": _r(_pct(_price_now(ctx, pv.XAUUSD, now), _ounce_open(ctx, day, now))),
            "usdt_spread_bps": float(cost_table["usdtirt"]["spread_bps"]),
            "gold_spread_bps": float(cost_table["gold"]["spread_bps"]),
        },
        "diagnostics": {
            "decision_prices": prices.as_dict(),
            "session_utc": {"open": open_ts.isoformat(), "flat": flat_ts.isoformat()},
            "as_of_utc": ctx.as_of.isoformat(),
            "split": ctx.split_name,
        },
    }
    return state


def _price_now(ctx: ReadContext, candidates: list[tuple[str, str, str]], now: datetime) -> float | None:
    for source, symbol, resolution in candidates:
        row = ctx.conn.execute(
            """
            select close from price_bars
            where source = %s and symbol = %s and resolution = %s and ts <= %s
            order by ts desc limit 1
            """,
            (source, symbol, resolution, min(ctx.as_of, now)),
        ).fetchone()
        if row:
            return float(row["close"])
    return None


def _ounce_open(ctx: ReadContext, day: date, now: datetime) -> float | None:
    """Ounce level at the start of the UTC day (proxy for 'daily open')."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=TEHRAN).astimezone(
        __import__("datetime").timezone.utc
    )
    for source, symbol, resolution in pv.XAUUSD:
        row = ctx.conn.execute(
            """
            select close from price_bars
            where source = %s and symbol = %s and resolution = %s and ts >= %s and ts <= %s
            order by ts asc limit 1
            """,
            (source, symbol, resolution, start, min(ctx.as_of, now)),
        ).fetchone()
        if row:
            return float(row["close"])
    return None


__all__ = ["build_state", "day_prices", "historic_day_moves", "DayPrices"]

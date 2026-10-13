"""Synthetic market generator (DEMO ONLY).

Produces a *coherent* multi-series world:
  * USDT/IRT 1m bars inside each Tehran session (08:00–22:00)
  * XAUUSD hourly bars (with the weekend gap)
  * TGJU-style daily series: 18k gold/gram, Emami coin, free-market USD, ounce
  * TSETMC-style daily gold-fund series
  * live-logger 1m bars for the most recent sessions only (mirrors the real world, where
    minute-level domestic gold history starts when the logger starts)

The generator also exposes the latent per-minute USD return path to `telegram.py`, which
uses it to create genuinely leading / lagging / pumping channels. That is what lets the
statistics module demonstrate a true positive in demo mode.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from ..timez import TEHRAN, iter_trading_days, local_ts

OUNCE_TROY_GRAM = 31.1035
PURITY_18K = 18 / 24
COIN_GOLD_GRAMS = 8.133  # Emami (full) coin: 900-purity gold content ~7.32g fine gold
COIN_MULTIPLE = 9.42     # market rule of thumb: full coin ≈ 9.4 × 18k gram price (pre-bubble)

DEMO_SOURCES = "synthetic"


@dataclass
class DemoMarket:
    seed: int
    start: date
    end: date
    weekdays: set[int]
    base_usd_toman: float = 118_000.0
    base_ounce_usd: float = 3_320.0
    live_logger_days: int = 3

    rng: random.Random = field(init=False)
    trading_days: list[date] = field(init=False, default_factory=list)
    # day -> list of per-minute log returns for the 840 session minutes
    usd_minute_returns: dict[date, list[float]] = field(init=False, default_factory=dict)
    usd_paths: dict[date, list[float]] = field(init=False, default_factory=dict)
    usd_opens: dict[date, float] = field(init=False, default_factory=dict)
    usd_closes: dict[date, float] = field(init=False, default_factory=dict)
    ox_opens: dict[date, float] = field(init=False, default_factory=dict)
    ox_closes: dict[date, float] = field(init=False, default_factory=dict)
    ox_hourly: dict[date, list[float]] = field(init=False, default_factory=dict)
    gold_close: dict[date, float] = field(init=False, default_factory=dict)
    premium: dict[date, float] = field(init=False, default_factory=dict)
    bubble: dict[date, float] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)
        self.trading_days = list(iter_trading_days(self.start, self.end, self.weekdays))
        self._simulate()

    # ------------------------------------------------------------------ simulation

    def _simulate(self) -> None:
        rng = self.rng
        usd = self.base_usd_toman
        ounce = self.base_ounce_usd
        premium = 0.020
        bubble = 0.035
        for day in self.trading_days:
            # ---- USDT/IRT intraday path
            open_price = usd
            returns: list[float] = []
            path = [usd]
            # a small deterministic diurnal vol shape (quiet around midday, busier at open/close)
            for minute in range(840):
                hours_in = minute / 60.0
                vol = 2.6e-4 * (1.0 + 0.55 * math.exp(-hours_in) + 0.35 * math.exp(-(14 - hours_in)))
                r = rng.gauss(0.0, vol)
                if rng.random() < 0.0025:
                    r += rng.choice((-1.0, 1.0)) * rng.uniform(0.0008, 0.0035)
                returns.append(r)
                usd *= math.exp(r)
                path.append(usd)
            self.usd_minute_returns[day] = returns
            self.usd_paths[day] = path
            self.usd_opens[day] = open_price
            self.usd_closes[day] = usd

            # ---- XAUUSD hourly path (24/7 with a Fri-night->Sun-night weekend gap)
            ounce_open = ounce
            hourly: list[float] = []
            for hour in range(24):
                r = rng.gauss(0.0, 1.9e-4)
                if rng.random() < 0.01:
                    r += rng.choice((-1.0, 1.0)) * rng.uniform(0.001, 0.004)
                ounce *= math.exp(r)
                hourly.append(ounce)
            self.ox_hourly[day] = hourly
            self.ox_opens[day] = ounce_open
            self.ox_closes[day] = ounce

            # ---- domestic premium + coin bubble (mean-reverting, occasionally spiking)
            premium = 0.985 * premium + 0.015 * 0.020 + rng.gauss(0.0, 0.0016)
            bubble = 0.94 * bubble + 0.06 * 0.035 + rng.gauss(0.0, 0.006)
            if rng.random() < 0.05:  # "hojoom" episode
                bubble += rng.uniform(0.01, 0.05)
            self.premium[day] = premium
            self.bubble[day] = bubble

            gold = ounce * usd / OUNCE_TROY_GRAM * PURITY_18K * (1.0 + premium)
            self.gold_close[day] = round(gold / 1000.0) * 1000.0

    # ------------------------------------------------------------------ series

    def usdtirt_1m_rows(self) -> list[tuple]:
        rows: list[tuple] = []
        for day in self.trading_days:
            path = self.usd_paths[day]
            start = local_ts(day, datetime.min.time().replace(hour=8), TEHRAN)
            for i, price in enumerate(path[:-1]):
                ts = start + timedelta(minutes=i)
                price = round(price, 1)
                rows.append((DEMO_SOURCES, "USDTIRT", ts, "1m", price, price, price, price, None))
        return rows

    def xauusd_1h_rows(self) -> list[tuple]:
        rows: list[tuple] = []
        price = self.base_ounce_usd
        for day in self.trading_days:
            for hour, close in enumerate(self.ox_hourly[day]):
                ts = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
                if ts.weekday() == 4 and hour >= 17:  # Friday evening: market closed
                    continue
                if ts.weekday() == 5:  # Saturday
                    continue
                if ts.weekday() == 6 and hour < 22:  # Sunday until 22:00 UTC
                    continue
                open_ = price
                price = close
                rows.append((DEMO_SOURCES, "XAUUSD", ts, "1h", open_, max(open_, close), min(open_, close), close, None))
        return rows

    def tgju_daily_rows(self) -> list[tuple]:
        rows: list[tuple] = []
        for day in self.trading_days:
            stamp = local_ts(day, datetime.min.time().replace(hour=12), TEHRAN)
            gold = self.gold_close[day]
            coin = round(gold * COIN_MULTIPLE * (1.0 + self.bubble[day]) / 1000.0) * 1000.0
            usd_free = round(self.usd_closes[day] * (1.0 + 0.004), 1)
            ounce = self.ox_closes[day]
            for symbol, close in (
                ("gold_18k_toman_per_gram", gold),
                ("emami_coin_toman", coin),
                ("usd_free_market_toman", usd_free),
                ("xauusd_ounce_usd", round(ounce, 2)),
            ):
                rows.append((DEMO_SOURCES, symbol, stamp, "1d", close, close, close, close, None))
        return rows

    def tsetmc_daily_rows(self, symbol: str = "goldfund_demo") -> list[tuple]:
        rows: list[tuple] = []
        rng = random.Random(self.seed + 7)
        for day in self.trading_days:
            stamp = local_ts(day, datetime.min.time().replace(hour=17), TEHRAN)
            # a gold fund tracks 18k gold per unit with a small tracking error + NAV premium
            close = round(self.gold_close[day] * 0.980 * (1 + rng.gauss(0, 0.004)) / 10.0) * 10.0
            rows.append((DEMO_SOURCES, symbol, stamp, "1d", close, close, close, close, rng.uniform(1e9, 9e9)))
        return rows

    def live_logger_rows(self) -> list[tuple]:
        """Minute-level domestic gold + coin for the most recent sessions (P6 behaviour)."""
        rows: list[tuple] = []
        recent = self.trading_days[-self.live_logger_days :]
        for day in recent:
            prev_gold = self.gold_close[self.trading_days[max(0, self.trading_days.index(day) - 1)]]
            target = self.gold_close[day]
            start = local_ts(day, datetime.min.time().replace(hour=8), TEHRAN)
            path = self.usd_paths[day]
            usd_open = self.usd_opens[day]
            for minute in range(840):
                # domestic gold follows the intraday USD path plus noise, converging to the close
                usd_rel = path[minute] / usd_open - 1.0
                drift = (target / prev_gold - 1.0) * (minute / 840.0)
                noise = math.exp(self.rng.gauss(0, 0.00035))
                price = prev_gold * math.exp(usd_rel * 0.9) * (1 + drift) * noise
                ts = start + timedelta(minutes=minute)
                price = round(price / 1000.0) * 1000.0
                coin = round(price * COIN_MULTIPLE * (1.0 + self.bubble[day]) / 1000.0) * 1000.0
                usd_price = round(path[minute], 1)
                rows.append((DEMO_SOURCES, "gold_18k_toman_per_gram", ts, "1m", price, price, price, price, None))
                rows.append((DEMO_SOURCES, "emami_coin_toman", ts, "1m", coin, coin, coin, coin, None))
                rows.append((DEMO_SOURCES, "USDTIRT", ts, "1m", usd_price, usd_price, usd_price, usd_price, None))
        return rows

    # ------------------------------------------------------------------ helpers used by demo channels

    def usd_forward_return(self, day: date, minute: int, horizon: int) -> float:
        path = self.usd_paths[day]
        if minute >= len(path) - 1:
            return 0.0
        end = min(len(path) - 1, minute + horizon)
        return math.log(path[end] / path[minute])

    def usd_trailing_return(self, day: date, minute: int, lookback: int) -> float:
        path = self.usd_paths[day]
        start = max(0, minute - lookback)
        return math.log(path[minute] / path[start])

    def real_gold_at(self, day: date, minute: int) -> float:
        """Approximate the live domestic gold price at an intraday minute (for quote bias)."""
        idx = self.trading_days.index(day)
        prev_gold = self.gold_close[self.trading_days[max(0, idx - 1)]]
        target = self.gold_close[day]
        usd_rel = self.usd_paths[day][minute] / self.usd_opens[day] - 1.0
        drift = (target / prev_gold - 1.0) * (minute / 840.0)
        return prev_gold * math.exp(usd_rel * 0.9) * (1 + drift)

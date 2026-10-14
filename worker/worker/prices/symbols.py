"""Canonical series registry (symbol names are part of the data contract — never rename
without a migration, reports and UI reference them verbatim)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Series:
    source: str
    symbol: str
    resolution: str
    label: str
    asset: str  # cost bucket: gold | coin | usdtirt | xauusd
    primary: bool = False


SERIES: dict[str, Series] = {
    "usdtirt_1m": Series("nobitex", "USDTIRT", "1m", "USDT/IRT (Nobitex)", "usdtirt", primary=True),
    "usdtirt_1h": Series("nobitex", "USDTIRT", "1h", "USDT/IRT hourly", "usdtirt"),
    "xauusd_1h": Series("yfinance", "XAUUSD", "1h", "Gold ounce (USD) hourly", "xauusd", primary=True),
    "xauusd_1d": Series("yfinance", "XAUUSD", "1d", "Gold ounce (USD) daily", "xauusd"),
    "gold18k_1d": Series("tgju", "gold_18k_toman_per_gram", "1d", "18k gold / gram (TGJU)", "gold", primary=True),
    "emami_1d": Series("tgju", "emami_coin_toman", "1d", "Emami coin (TGJU)", "coin", primary=True),
    "usd_free_1d": Series("tgju", "usd_free_market_toman", "1d", "Free-market USD (TGJU)", "usdtirt"),
    "gold18k_1m": Series("live_logger", "gold_18k_toman_per_gram", "1m", "18k gold / gram (live logger)", "gold", primary=True),
    "emami_1m": Series("live_logger", "emami_coin_toman", "1m", "Emami coin (live logger)", "coin"),
    "usdtirt_live_1m": Series("live_logger", "USDTIRT", "1m", "USDT/IRT (live logger)", "usdtirt"),
    "xauusd_live_1m": Series("live_logger", "XAUUSD", "1m", "Gold ounce (live logger)", "xauusd"),
}

# The series a day-level call is scored against (decision price 08:00 → 22:00).
DECISION_SERIES = "gold_18k_toman_per_gram"

# Horizons used everywhere (R5).
HORIZONS = {"30m": 30, "1h": 60, "3h": 180, "eod": None}


def decision_series() -> str:
    return DECISION_SERIES


def series_for(asset: str, resolution: str) -> Series | None:
    for s in SERIES.values():
        if s.asset == asset and s.resolution == resolution:
            return s
    return None

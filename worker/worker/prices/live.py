"""P6 — live minute logger.

Polls every 60s during the Tehran session and writes `price_bars(source='live_logger')`.
This is the dataset that removes the historical intraday limitation: from day 1 of running,
domestic gold has genuine minute history (R8 — we never back-fill it retroactively).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
import psycopg

from ..config import Settings
from ..db import repo
from ..timez import TEHRAN, local_ts, tehran_clock, today_tehran
from ..util.http import get_json
from .base import Bar, bars_to_rows

log = logging.getLogger(__name__)


@dataclass
class LiveQuote:
    symbol: str
    price: float
    source: str
    detail: dict[str, Any] | None = None


async def poll_tgju(client: httpx.AsyncClient, api_base: str) -> list[LiveQuote]:
    quotes: list[LiveQuote] = []
    for slug, symbol in (("geram18", "gold_18k_toman_per_gram"), ("sekeb", "emami_coin_toman")):
        try:
            payload = await get_json(
                client, f"{api_base}/v1/market/indicator/summary-table-data/{slug}", max_attempts=2
            )
        except Exception as exc:  # noqa: BLE001 - recorded as a gap by the caller
            log.warning("tgju live poll failed for %s: %s", slug, exc)
            continue
        price = _extract_price(payload)
        if price is not None:
            quotes.append(LiveQuote(symbol=symbol, price=price, source="tgju", detail={"slug": slug}))
    return quotes


async def poll_nobitex(client: httpx.AsyncClient, base: str) -> list[LiveQuote]:
    try:
        payload = await get_json(
            client, f"{base.rstrip('/')}/market/stats", params={"srcCurrency": "usdt", "dstCurrency": "rls"},
            max_attempts=2,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("nobitex live poll failed: %s", exc)
        return []
    stats = (payload or {}).get("stats") or {}
    for key in ("usdt-rls", "USDTIRT", "usdt-irt"):
        entry = stats.get(key)
        if entry and entry.get("latest"):
            return [LiveQuote(symbol="USDTIRT", price=float(entry["latest"]) / 10.0, source="nobitex",
                              detail={"raw_key": key, "unit": "toman_from_rial"})]
    return []


def _extract_price(payload: object) -> float | None:
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                for key in ("value", "price", "close", "p"):
                    if key in row:
                        try:
                            return float(str(row[key]).replace(",", ""))
                        except (TypeError, ValueError):
                            continue
    return None


def upsert_quotes(conn: psycopg.Connection, quotes: list[LiveQuote], *, ts: datetime | None = None) -> int:
    stamp = (ts or datetime.now(tz=timezone.utc)).replace(second=0, microsecond=0)
    rows: list[tuple[Any, ...]] = []
    for q in quotes:
        bars = [Bar(ts=stamp, close=q.price, open=q.price, high=q.price, low=q.price)]
        for row in bars_to_rows("live_logger", q.symbol, "1m", bars):
            extra = list(row)
            rows.append(tuple(extra))
    if not rows:
        return 0
    return repo.upsert_price_bars(conn, rows)


async def log_once(settings: Settings, conn: psycopg.Connection) -> int:
    """One polling pass. Returns the number of bars written."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        quotes: list[LiveQuote] = []
        if settings.tgju_enabled:
            quotes += await poll_tgju(client, "https://api.tgju.org")
        quotes += await poll_nobitex(client, settings.nobitex_base)
        ounce = await poll_xauusd_hourly(client)
        quotes += ounce
    written = upsert_quotes(conn, quotes)
    if not quotes:
        now = datetime.now(tz=timezone.utc)
        repo.record_gap(conn, "live_logger", "*", now.replace(second=0, microsecond=0), now, "no_quote_returned")
    return written


async def poll_xauusd_hourly(client: httpx.AsyncClient) -> list[LiveQuote]:  # pragma: no cover - network
    """Live ounce is the hourly yfinance bar re-read (documented ±1h resolution)."""
    try:
        from .xauusd import YFinanceXauProvider

        provider = YFinanceXauProvider()
        today = today_tehran()
        bars = await provider.fetch(local_ts(today, __import__("datetime").time(0, 0)), datetime.now(tz=timezone.utc))
    except Exception as exc:  # noqa: BLE001
        log.warning("live XAUUSD poll failed: %s", exc)
        return []
    if not bars:
        return []
    last = bars[-1]
    return [LiveQuote(symbol="XAUUSD", price=last.close, source="yfinance", detail={"bar_ts": last.ts.isoformat()})]


def in_session(settings: Settings, now: datetime | None = None) -> bool:
    clock = tehran_clock(now or datetime.now(tz=timezone.utc))
    return settings.open_time <= clock <= settings.flat_time


async def run_forever(settings: Settings, conn: psycopg.Connection, *, interval_s: float = 60.0) -> None:
    log.info("live logger started (session %s-%s Tehran, interval %.0fs)",
             settings.open_at_local, settings.flat_at_local, interval_s)
    while True:
        try:
            if in_session(settings):
                n = await log_once(settings, conn)
                conn.commit()
                log.info("live logger wrote %d bar(s) at %s", n, tehran_clock(datetime.now(tz=timezone.utc)))
            else:
                log.info("outside session (%s Tehran) — sleeping", tehran_clock(datetime.now(tz=timezone.utc)))
        except Exception as exc:  # noqa: BLE001 - the logger must survive transient failures
            log.warning("live logger iteration failed: %s", exc)
        await asyncio.sleep(interval_s)


__all__ = ["LiveQuote", "log_once", "upsert_quotes", "in_session", "run_forever", "TEHRAN"]

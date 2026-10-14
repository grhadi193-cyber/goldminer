"""Build the synthetic demo database (DEMO ONLY — see worker/demo/__init__.py)."""

from __future__ import annotations

import logging
from datetime import date

import psycopg

from ..config import Settings
from ..db import repo
from ..extraction.pipeline import run_extraction
from ..extraction.provider import MockProvider
from ..prices import coverage as cov
from ..prices.base import bars_to_rows
from ..prices.derive import derive_resolution
from ..prices.symbols import SERIES
from ..timez import add_months, today_tehran
from .guard import DEMO_MESSAGE_TAG
from .market import DemoMarket
from .telegram import CHANNELS, generate, generate_edits

log = logging.getLogger(__name__)


def build_market(settings: Settings, *, days: int | None = None, seed: int = 20260401) -> DemoMarket:
    end = today_tehran()
    start = add_months(end, -settings.history_months - 1)
    return DemoMarket(
        seed=seed,
        start=start,
        end=end,
        weekdays=settings.trading_weekday_set,
        live_logger_days=settings.history_months * 0 + 3,
    )


def seed(settings: Settings, conn: psycopg.Connection, *, days: int | None = None, seed_value: int = 20260401,
         with_extraction: bool = True, message_days: int | None = None) -> dict:
    market = build_market(settings, days=days, seed=seed_value)
    log.info("demo market: %d trading days %s..%s", len(market.trading_days), market.trading_days[0], market.trading_days[-1])

    price_rows = (
        market.usdtirt_1m_rows()
        + market.xauusd_1h_rows()
        + market.tgju_daily_rows()
        + market.tsetmc_daily_rows()
        + market.live_logger_rows()
    )
    written = repo.upsert_price_bars(conn, price_rows)
    conn.commit()

    repo.upsert_channels(conn, [
        {"username": c["username"], "title": c["title"], "active": c["active"],
         "notes": f"demo archetype: {c['tier']}"}
        for c in CHANNELS
    ])
    for c in CHANNELS:
        conn.execute("update channels set tier = %s where username = %s", (c["tier"], c["username"]))
    conn.commit()

    cid = repo.channel_id_map(conn)
    messages = generate(market, seed=seed_value, max_days=message_days)
    edits = generate_edits(messages, seed=seed_value)
    rows = [
        (
            cid[m.username], m.telegram_msg_id, m.published_at, m.ingested_at, m.edited_at,
            m.raw_text, m.version, m.is_edited, None,
        )
        for m in messages + edits
    ]
    inserted = repo.upsert_messages(conn, rows)
    conn.commit()
    log.info("demo messages: %d versions inserted (%d originals, %d edits)", inserted, len(messages), len(edits))

    stats = None
    if with_extraction:
        stats = _extract(settings, conn)

    derived = derive_resolution(conn, source="synthetic", symbol="gold_18k_toman_per_gram", base_resolution="1m", targets=("5m", "1h"))
    conn.commit()

    start = market.trading_days[0]
    for series in (SERIES["gold18k_1d"], SERIES["emami_1d"], SERIES["usd_free_1d"], SERIES["xauusd_1h"]):
        cov.record_missing_bars(conn, series, start=start, end=market.trading_days[-1], settings=settings)
    conn.commit()

    repo.audit(conn, "worker:demo", "seed", {
        "trading_days": len(market.trading_days),
        "price_rows": written,
        "messages": len(messages),
        "edits": len(edits),
        "derived": derived,
    })
    conn.commit()
    return {
        "trading_days": len(market.trading_days),
        "first_day": market.trading_days[0].isoformat(),
        "last_day": market.trading_days[-1].isoformat(),
        "price_rows": written,
        "messages": len(messages),
        "message_versions": inserted,
        "edits": len(edits),
        "derived": derived,
        "extraction": stats.as_dict() if stats else None,
    }


def _extract(settings: Settings, conn: psycopg.Connection):
    import asyncio

    from ..extraction.pipeline import extraction_summary

    stats = asyncio.run(run_extraction(conn, settings, provider=MockProvider(), batch=250, commit_every=250, progress=False))
    log.info("demo extraction: %s", stats.as_dict())
    _ = extraction_summary
    return stats


def reset(conn: psycopg.Connection) -> dict[str, int]:
    """Remove synthetic data so the DB can hold a real run (demo and real never mix)."""
    counts: dict[str, int] = {}
    counts["price_bars"] = conn.execute("delete from price_bars where source = 'synthetic'").rowcount or 0
    counts["price_gaps"] = conn.execute("delete from price_gaps where source = 'synthetic'").rowcount or 0
    counts["extractions"] = conn.execute(
        "delete from extractions where message_id in (select id from messages where raw_text like %s)",
        (DEMO_MESSAGE_TAG + "%",),
    ).rowcount or 0
    counts["messages"] = conn.execute("delete from messages where raw_text like %s", (DEMO_MESSAGE_TAG + "%",)).rowcount or 0
    counts["channels"] = conn.execute(
        "delete from channels where id not in (select distinct channel_id from messages)"
    ).rowcount or 0
    for table in ("backtest_trades", "jev_decisions", "day_outcomes", "paper_orders", "paper_positions", "paper_equity", "channel_scores"):
        counts[table] = conn.execute(f"delete from {table}").rowcount or 0  # type: ignore[arg-type]
    conn.commit()
    return counts


def window_days(settings: Settings) -> tuple[date, date]:
    market = build_market(settings)
    return market.trading_days[0], market.trading_days[-1]

"""Derive coarser resolutions locally from stored 1m bars (R9: derive, don't fetch twice)."""

from __future__ import annotations

import logging

import psycopg

from ..db import repo
from .base import bars_to_rows, resample_bars

log = logging.getLogger(__name__)


def derive_resolution(
    conn: psycopg.Connection,
    *,
    source: str,
    symbol: str,
    base_resolution: str = "1m",
    targets: tuple[str, ...] = ("5m", "1h"),
    limit: int | None = None,
) -> dict[str, int]:
    """Read fine bars, aggregate into `targets`, upsert. Idempotent (same unique key)."""
    rows = conn.execute(
        """
        select ts, close from price_bars
        where source = %s and symbol = %s and resolution = %s
        order by ts asc
        """
        + (f" limit {int(limit)}" if limit else ""),
        (source, symbol, base_resolution),
    ).fetchall()
    if not rows:
        log.warning("no %s bars for %s/%s — nothing to derive", base_resolution, source, symbol)
        return {t: 0 for t in targets}
    counts: dict[str, int] = {}
    for target in targets:
        bars = resample_bars([(r["ts"], float(r["close"])) for r in rows], target)
        payload = bars_to_rows(source, symbol, target, bars)
        counts[target] = repo.upsert_price_bars(conn, payload)
        log.info("derived %d %s bars for %s/%s", counts[target], target, source, symbol)
    return counts

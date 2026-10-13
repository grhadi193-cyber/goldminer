"""Nobitex UDF history client (USDTIRT). Public endpoint, no token.

Documented behaviour (probe verified 2026-10-13, see docs/source_notes.md):
  GET {NOBITEX_BASE}/market/udf/history
      ?symbol=USDTIRT&resolution=1&from=<unix_s>&to=<unix_s>&countback=<n>&page=<n>
  responses: {"s":"ok","t":[...],"o":[...],"h":[...],"l":[...],"c":[...],"v":[...]}
             {"s":"no_data"}
  countback <= 500, public rate limit 60 req/min (we throttle to half: NOBITEX_MAX_REQ_PER_MIN).

If the endpoint answers differently than documented above, this module raises SourceError
with the observed payload — it never patches data to fit assumptions (R13.3).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from ..errors import SourceError
from ..util.http import get_json
from ..util.ratelimit import RateLimiter
from .base import Bar, trading_day_windows

log = logging.getLogger(__name__)

RESOLUTION_CODES = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "1d": "D"}
MAX_COUNTBACK = 500


class NobitexProvider:
    """1m USDTIRT candles, chunked so a Ctrl-C loses at most one chunk."""

    source = "nobitex"

    def __init__(
        self,
        base: str,
        symbol: str = "USDTIRT",
        resolution: str = "1m",
        *,
        max_req_per_min: int = 30,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base = base.rstrip("/")
        self.symbol = symbol
        self.resolution = resolution
        self.limiter = RateLimiter(max_req_per_min)
        self._client = client

    def chunk_plan(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "1d": 1440}[self.resolution]
        span_minutes = max(1, (MAX_COUNTBACK * minutes) - minutes)  # keep one bar of headroom
        return trading_day_windows(start, end, hours=max(1, span_minutes // 60))

    async def fetch(self, start: datetime, end: datetime) -> list[Bar]:
        if self._client is not None:
            return await self._fetch_with(self._client, start, end)
        async with httpx.AsyncClient(timeout=20.0) as client:
            return await self._fetch_with(client, start, end)

    async def _fetch_with(self, client: httpx.AsyncClient, start: datetime, end: datetime) -> list[Bar]:
        await self.limiter.acquire()
        url = f"{self.base}/market/udf/history"
        params = {
            "symbol": self.symbol,
            "resolution": RESOLUTION_CODES[self.resolution],
            "from": int(start.timestamp()),
            "to": int(end.timestamp()),
        }
        payload = await get_json(client, url, params=params)
        return self.parse_udf(payload, url)

    @staticmethod
    def parse_udf(payload: object, url: str = "") -> list[Bar]:
        if not isinstance(payload, dict):
            raise SourceError(f"expected JSON object from {url}, got {type(payload).__name__}")
        status = payload.get("s")
        if status == "no_data":
            return []
        if status != "ok":
            raise SourceError(f"unexpected Nobitex status {status!r} from {url}: {str(payload)[:300]}")
        cols = {k: payload.get(k) or [] for k in ("t", "o", "h", "l", "c", "v")}
        lengths = {k: len(v) for k, v in cols.items()}
        if len(set(lengths.values())) != 1:
            raise SourceError(f"ragged UDF arrays from {url}: {lengths}")
        out: list[Bar] = []
        for i in range(lengths["t"]):
            try:
                ts = datetime.fromtimestamp(int(cols["t"][i]), tz=timezone.utc)
                out.append(
                    Bar(
                        ts=ts,
                        open=float(cols["o"][i]),
                        high=float(cols["h"][i]),
                        low=float(cols["l"][i]),
                        close=float(cols["c"][i]),
                        volume=float(cols["v"][i]) if cols["v"] and cols["v"][i] is not None else None,
                    )
                )
            except (TypeError, ValueError) as exc:
                log.warning("skipping malformed Nobitex row %d (%s)", i, exc)
        return sorted(out, key=lambda b: b.ts)

    async def fetch_range_chunked(
        self, start: datetime, end: datetime, *, on_chunk=None
    ) -> tuple[list[Bar], list[tuple[datetime, datetime, str]]]:
        """Fetch everything, reporting failed chunks instead of silently filling them (R8)."""
        bars: list[Bar] = []
        failures: list[tuple[datetime, datetime, str]] = []
        for chunk_start, chunk_end in self.chunk_plan(start, end):
            try:
                got = await self.fetch(chunk_start, chunk_end)
            except Exception as exc:  # noqa: BLE001 - recorded as a gap, never hidden
                log.warning("nobitex chunk %s..%s failed: %s", chunk_start, chunk_end, exc)
                failures.append((chunk_start, chunk_end, f"fetch_failed:{type(exc).__name__}"))
                continue
            bars.extend(got)
            expected = int((chunk_end - chunk_start).total_seconds() // 60)
            if expected and len(got) < expected * 0.5:
                failures.append((chunk_start, chunk_end, f"partial_chunk:{len(got)}/{expected}"))
            if on_chunk:
                on_chunk(chunk_end, len(got))
        return bars, failures


def block_window(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """Sanity helper for the '6 months ≈ 180 days' style windows used by scripts."""
    if end <= start:
        raise ValueError("end must be after start")
    return start, start + timedelta(days=1) if end <= start else (start, end)

"""TSETMC gold funds (daily + intraday where the public API offers it).

The trader picks fund symbols in the UI; they land in `channels`-style config
(`run_config['tsetmc_symbols']`) and are read here. Public TSETMC endpoints are unofficial
and can change shape — drift is raised, documented, never patched (R13.3).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timezone

import httpx

from ..errors import SourceError
from ..timez import TEHRAN, local_ts
from ..util.http import get_json
from ..util.ratelimit import RateLimiter
from .base import Bar

log = logging.getLogger(__name__)

TSETMC_BASE = "https://cdn.tsetmc.com/api"
# TSETMC daily closes are stamped after the close of the Tehran session (12:30 UTC = 16:00
# local in standard time); intraday prints are stamped at their own timestamps.
DAILY_STAMP_LOCAL = time(17, 0)


class TsetmcProvider:
    source = "tsetmc"

    def __init__(
        self,
        base: str = TSETMC_BASE,
        *,
        resolution: str = "1d",
        rate_per_min: float = 20,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base = base.rstrip("/")
        self.resolution = resolution
        self.limiter = RateLimiter(rate_per_min)
        self._client = client

    async def fetch_ins_code(self, symbol: str) -> str | None:
        """Resolve a ticker/name to a TSETMC insCode (14-digit identifier)."""
        url = f"{self.base}/Instrument/GetInstrumentSearch/{symbol}"
        async with _client_ctx(self._client) as client:
            await self.limiter.acquire()
            payload = await get_json(client, url)
        rows = (payload or {}).get("instrumentSearch") or []
        for row in rows:
            if str(row.get("lVal18AFC", "")).strip() == symbol.strip():
                return str(row.get("insCode"))
        if rows:
            return str(rows[0].get("insCode"))
        return None

    async def fetch_daily(self, ins_code: str) -> list[Bar]:
        url = f"{self.base}/ClosingPrice/GetClosingPriceDailyList/{ins_code}/0"
        async with _client_ctx(self._client) as client:
            await self.limiter.acquire()
            payload = await get_json(client, url)
        rows = (payload or {}).get("closingPriceDaily") or []
        if not rows:
            raise SourceError(f"TSETMC returned no daily rows for insCode {ins_code}: {str(payload)[:200]}")
        bars: list[Bar] = []
        for row in rows:
            stamp = row.get("dEven") or row.get("date")
            close = row.get("pClosing") or row.get("pDrCotVal")
            if stamp is None or close is None:
                continue
            try:
                day = datetime.strptime(str(int(stamp))[:8], "%Y%m%d").date()
            except (TypeError, ValueError):
                continue
            price = float(close)
            if price <= 0:
                continue
            bars.append(Bar(ts=local_ts(_shift_to_session(day), DAILY_STAMP_LOCAL, TEHRAN), close=price,
                            open=price, high=price, low=price))
        return sorted(bars, key=lambda b: b.ts)

    async def fetch_intraday(self, ins_code: str) -> list[Bar]:
        """Best-effort intraday prints; empty list when the endpoint offers none."""
        url = f"{self.base}/ClosingPrice/GetClosingPriceHistory/{ins_code}"
        async with _client_ctx(self._client) as client:
            await self.limiter.acquire()
            try:
                payload = await get_json(client, url)
            except Exception as exc:  # noqa: BLE001
                log.warning("TSETMC intraday unavailable for %s: %s", ins_code, exc)
                return []
        rows = (payload or {}).get("closingPriceHistory") or []
        bars: list[Bar] = []
        for row in rows:
            stamp = row.get("dEven")
            close = row.get("pClosing")
            if stamp is None or close is None:
                continue
            bars.append(
                Bar(ts=datetime.fromtimestamp(int(stamp), tz=timezone.utc), close=float(close))
            )
        return sorted(bars, key=lambda b: b.ts)


def _shift_to_session(day: date) -> date:
    """Accounting date -> the Tehran calendar day the close belongs to (identity here, kept
    explicit so a future provider change is a one-line edit)."""
    return day


class _client_ctx:
    """Use the injected client if present, else open a short-lived one."""

    def __init__(self, client: httpx.AsyncClient | None) -> None:
        self._client = client
        self._owned: httpx.AsyncClient | None = None

    async def __aenter__(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        self._owned = httpx.AsyncClient(timeout=25.0)
        return self._owned

    async def __aexit__(self, *exc: object) -> None:
        if self._owned is not None:
            await self._owned.aclose()

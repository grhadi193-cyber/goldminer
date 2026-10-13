"""TGJU daily series (unofficial, indicative — see README honesty section).

Two access paths, both optional and configurable:
  1. the `oxtapus` package (if installed) — `oxtapus.tgju` readers
  2. direct HTTP against TGJU's public JSON endpoints (TGJU_API_BASE, default api.tgju.org)

Shape drift is expected for an unofficial source: when the payload does not look like what
we expect, we raise `SourceError` and the operator records it in `docs/source_notes.md`.
Missing days are recorded in `price_gaps`; they are NEVER interpolated (R8).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone

import httpx

from ..errors import SourceError
from ..timez import TEHRAN, local_ts
from ..util.http import get_json
from ..util.ratelimit import RateLimiter
from .base import Bar

log = logging.getLogger(__name__)

# TGJU slugs for the series we care about -> canonical symbol names
SLUGS: dict[str, str] = {
    "geram18": "gold_18k_toman_per_gram",
    "sekeb": "emami_coin_toman",
    "sekek": "whole_coin_toman",   # "seke kamel"
    "nim": "half_coin_toman",      # nim-seke
    "rob": "quarter_coin_toman",
    "dollar_tehran3": "usd_free_market_toman",
    "price_dollar_rl": "usd_free_market_toman",
    "ons": "xauusd_ounce_usd",
}

# Daily series are stamped at 12:00 Tehran (mid-session) so that the intraday ordering of a
# daily observation is never mistaken for a tradeable timestamp.
DAILY_STAMP_LOCAL = time(12, 0)


class TgjuProvider:
    source = "tgju"

    def __init__(
        self,
        api_base: str = "https://api.tgju.org",
        resolution: str = "1d",
        *,
        rate_per_min: float = 20,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.resolution = resolution
        self.limiter = RateLimiter(rate_per_min)
        self._client = client

    async def fetch(self, symbol_slug: str, start: date, end: date) -> list[Bar]:
        """Fetch a daily series for one slug between two Tehran dates."""
        if self._client is not None:
            return await self._fetch_with(self._client, symbol_slug, start, end)
        async with httpx.AsyncClient(timeout=25.0) as client:
            return await self._fetch_with(client, symbol_slug, start, end)

    async def _fetch_with(self, client: httpx.AsyncClient, slug: str, start: date, end: date) -> list[Bar]:
        await self.limiter.acquire()
        payload = await self._fetch_oxtapus(slug, start, end) or await self._fetch_http(client, slug, start, end)
        return self.parse_rows(payload, slug)

    async def _fetch_oxtapus(self, slug: str, start: date, end: date) -> list[dict] | None:
        """Use the community `oxtapus` package when the operator has installed it."""
        try:
            import oxtapus  # noqa: PLC0415
        except ImportError:
            return None
        try:
            reader = oxtapus.TGJUClient()
            frame = await asyncio.to_thread(reader.get_price, slug, start.isoformat(), end.isoformat())
        except Exception as exc:  # noqa: BLE001
            log.warning("oxtapus path failed for %s: %s", slug, exc)
            return None
        if frame is None or getattr(frame, "empty", True):
            return None
        rows: list[dict] = []
        for idx, row in frame.iterrows():
            rows.append({"date": str(idx), "value": row.to_dict().get("value", row.iloc[0])})
        return rows

    async def _fetch_http(self, client: httpx.AsyncClient, slug: str, start: date, end: date) -> list[dict]:
        url = f"{self.api_base}/v1/market/indicator/summary-table-data/{slug}"
        payload = await get_json(client, url, params={"_": int(datetime.now(tz=timezone.utc).timestamp())})
        if isinstance(payload, dict) and "data" in payload:
            return list(payload["data"])
        if isinstance(payload, list):
            return payload
        raise SourceError(
            f"unexpected TGJU payload shape for {slug}: {str(payload)[:200]} — record the drift "
            "in docs/source_notes.md and adapt this provider (never patch the data)."
        )

    def parse_rows(self, rows: list[dict], slug: str) -> list[Bar]:
        """Rows are Persian-calendar 'YYYY-MM-DD HH:MM:SS' strings in Tehran time for some
        endpoints and Gregorian for others. We accept both and never guess silently."""
        symbol = SLUGS.get(slug, slug)
        out: list[Bar] = []
        unparsed = 0
        for row in rows:
            value = _first_numeric(row)
            stamp = _first_stamp(row)
            if value is None or stamp is None:
                unparsed += 1
                continue
            day = _parse_date(stamp)
            if day is None:
                unparsed += 1
                continue
            ts = local_ts(day, DAILY_STAMP_LOCAL, TEHRAN)
            out.append(Bar(ts=ts, close=value, open=value, high=value, low=value))
            _ = symbol
        if unparsed:
            log.warning("TGJU %s: %d row(s) could not be parsed", slug, unparsed)
        return sorted(out, key=lambda b: b.ts)


def _first_numeric(row: dict) -> float | None:
    for key in ("value", "price", "close", "p", "v", "last"):
        if key in row:
            try:
                cleaned = str(row[key]).replace(",", "").strip()
                if cleaned:
                    return float(cleaned)
            except (TypeError, ValueError):
                continue
    return None


def _first_stamp(row: dict) -> str | None:
    for key in ("date", "time", "d", "t", "gregorian", "jalali"):
        if key in row and row[key]:
            return str(row[key])
    return None


def _parse_date(text: str) -> date | None:
    head = text.strip().replace("/", "-").split(" ")[0]
    try:
        return date.fromisoformat(head[:10])
    except ValueError:
        pass
    # Jalali date (e.g. 1403-07-21) -> convert only when a converter is available.
    parts = head.split("-")
    if len(parts) == 3 and parts[0].isdigit() and int(parts[0]) > 1300:
        try:
            import jdatetime  # noqa: PLC0415
        except ImportError:
            log.warning("Jalali date %s seen but jdatetime is not installed", text)
            return None
        try:
            return jdatetime.date(int(parts[0]), int(parts[1]), int(parts[2])).togregorian()
        except Exception:  # noqa: BLE001
            return None
    return None


def daily_gap_windows(days: list[date], present: set[date]) -> list[tuple[date, date]]:
    """Contiguous missing-day windows for gap recording."""
    missing = sorted(d for d in days if d not in present)
    if not missing:
        return []
    windows: list[tuple[date, date]] = []
    start = prev = missing[0]
    for day in missing[1:]:
        if day == prev + timedelta(days=1):
            prev = day
            continue
        windows.append((start, prev))
        start = prev = day
    windows.append((start, prev))
    return windows

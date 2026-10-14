"""Provider protocol + shared bar maths used by every ingestion path."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

RESOLUTION_MINUTES = {"1m": 1, "5m": 5, "1h": 60, "1d": 1440}


@dataclass(slots=True)
class Bar:
    ts: datetime  # UTC
    close: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None


@runtime_checkable
class PriceProvider(Protocol):
    """Every provider yields bars for one (symbol, resolution) over a UTC window."""

    source: str
    symbol: str
    resolution: str

    async def fetch(self, start: datetime, end: datetime) -> list[Bar]:  # pragma: no cover - protocol
        ...


def bars_to_rows(source: str, symbol: str, resolution: str, bars: list[Bar]) -> list[tuple[Any, ...]]:
    """Convert to `price_bars` upsert tuples in canonical column order
    (source, symbol, ts, resolution, open, high, low, close, volume), dropping malformed or
    duplicate timestamps."""
    seen: dict[datetime, tuple[Any, ...]] = {}
    for b in bars:
        if b.close is None:
            continue
        close = float(b.close)
        if close != close or close <= 0:  # NaN or non-positive
            continue
        open_ = float(b.open) if b.open is not None else close
        high = float(b.high) if b.high is not None else close
        low = float(b.low) if b.low is not None else close
        volume = float(b.volume) if b.volume is not None else None
        seen[b.ts] = (source, symbol, b.ts, resolution, open_, high, low, close, volume)
    return [seen[k] for k in sorted(seen)]


def expected_bars_between(*, resolution: str, start: datetime, end: datetime) -> int:
    minutes = RESOLUTION_MINUTES[resolution]
    total = max(0.0, (end - start).total_seconds() / 60.0)
    return int(total // minutes)


def session_expected_bars(
    *, resolution: str, open_ts: datetime, flat_ts: datetime, session_minutes: int = 14 * 60
) -> int:
    """Expected bars inside one Tehran session (08:00–22:00 = 840 minutes)."""
    minutes = RESOLUTION_MINUTES[resolution]
    return int(session_minutes // minutes)


def coverage_stats(
    bars: list[dict[str, Any]] | list[datetime],
    *,
    resolution: str,
    start: datetime,
    end: datetime,
) -> dict[str, float]:
    """Fraction of expected bars actually present in [start, end]."""
    stamps = [b["ts"] if isinstance(b, dict) else b for b in bars]
    expected = expected_bars_between(resolution=resolution, start=start, end=end)
    got = len({s for s in stamps if start <= s <= end})
    return {
        "expected": float(expected),
        "present": float(got),
        "coverage_pct": (100.0 * got / expected) if expected else 100.0,
    }


def merge_bars(existing: list[Bar], new: list[Bar]) -> list[Bar]:
    by_ts = {b.ts: b for b in existing}
    by_ts.update({b.ts: b for b in new})
    return [by_ts[k] for k in sorted(by_ts)]


def resample_bars(bars: list[tuple[datetime, float]], resolution: str) -> list[Bar]:
    """Derive a coarser resolution from finer bars locally (never fetch twice, R9/P2).

    Buckets are aligned to UTC boundaries for 1h (and to the UTC day for 1d); a bucket that
    has no underlying bars is simply absent — we never interpolate (R8).
    """
    minutes = RESOLUTION_MINUTES[resolution]
    buckets: dict[datetime, list[float]] = {}
    for ts, close in bars:
        if minutes == 1440:
            key = ts.replace(hour=0, minute=0, second=0, microsecond=0)
        elif minutes == 60:
            key = ts.replace(minute=0, second=0, microsecond=0)
        else:
            floor_min = (ts.minute // minutes) * minutes
            key = ts.replace(minute=floor_min, second=0, microsecond=0)
        buckets.setdefault(key, []).append(close)
    out: list[Bar] = []
    for key in sorted(buckets):
        prices = buckets[key]
        out.append(Bar(ts=key, open=prices[0], high=max(prices), low=min(prices), close=prices[-1]))
    return out


def trading_day_windows(start: datetime, end: datetime, *, hours: int = 6) -> list[tuple[datetime, datetime]]:
    """Split [start, end] into fixed-length fetch chunks (resumable units of work)."""
    out: list[tuple[datetime, datetime]] = []
    cur = start
    step = timedelta(hours=hours)
    while cur < end:
        nxt = min(cur + step, end)
        out.append((cur, nxt))
        cur = nxt
    return out

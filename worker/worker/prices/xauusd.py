"""XAUUSD provider interface. Default implementation: yfinance hourly (pluggable, R9).

Accuracy trade-off is explicit and reported: hourly bars mean ounce-based signals are only
resolved to ±1h. A better free intraday source can be dropped in behind the same
`PriceProvider` protocol and the same `price_bars` schema.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from ..errors import SourceError
from .base import Bar

log = logging.getLogger(__name__)

DEFAULT_TICKERS = ("GC=F", "XAUUSD=X")


class YFinanceXauProvider:
    source = "yfinance"

    def __init__(
        self,
        symbol: str = "XAUUSD",
        resolution: str = "1h",
        *,
        tickers: tuple[str, ...] = DEFAULT_TICKERS,
    ) -> None:
        self.symbol = symbol
        self.resolution = resolution
        self.tickers = tickers

    def _interval(self) -> str:
        return {"1h": "1h", "1d": "1d"}.get(self.resolution, "1h")

    async def fetch(self, start: datetime, end: datetime) -> list[Bar]:
        return await asyncio.to_thread(self._fetch_sync, start, end)

    def _fetch_sync(self, start: datetime, end: datetime) -> list[Bar]:
        try:
            import yfinance  # noqa: PLC0415 - optional dependency, imported lazily
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise SourceError(
                "yfinance is not installed; install the 'prices' extra or configure a "
                "different provider via XAUUSD_PROVIDER."
            ) from exc

        last_error: Exception | None = None
        for ticker in self.tickers:
            try:
                frame = yfinance.download(
                    ticker,
                    start=start.date().isoformat(),
                    end=end.date().isoformat(),
                    interval=self._interval(),
                    progress=False,
                    auto_adjust=False,
                    threads=False,
                )
            except Exception as exc:  # noqa: BLE001 - try the next ticker, report if all fail
                last_error = exc
                log.warning("yfinance failed for %s: %s", ticker, exc)
                continue
            bars = _frame_to_bars(frame)
            if bars:
                log.info("yfinance %s returned %d bars (%s)", ticker, len(bars), self.resolution)
                return bars
            last_error = SourceError(f"yfinance returned no rows for {ticker}")
        raise SourceError(f"all XAUUSD tickers failed: {last_error}")


def _frame_to_bars(frame: object) -> list[Bar]:
    """Convert a yfinance DataFrame to Bars without assuming a MultiIndex shape."""
    if frame is None or getattr(frame, "empty", True):
        return []
    df = frame
    if hasattr(df, "columns") and hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df = df.droplevel(1, axis=1)
    rename = {c: str(c).lower() for c in df.columns}
    df = df.rename(columns=rename)
    out: list[Bar] = []
    for idx, row in df.iterrows():
        ts = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else idx
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts = ts.astimezone(timezone.utc)
        close = row.get("close")
        if close is None or close != close:
            continue
        out.append(
            Bar(
                ts=ts,
                open=_f(row.get("open")),
                high=_f(row.get("high")),
                low=_f(row.get("low")),
                close=float(close),
                volume=_f(row.get("volume")),
            )
        )
    return out


def _f(value: object) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)  # type: ignore[arg-type]
        return None if out != out else out
    except (TypeError, ValueError):
        return None


def provider_for(name: str, symbol: str = "XAUUSD", resolution: str = "1h") -> object:
    """Factory driven by XAUUSD_PROVIDER (only 'yfinance' ships in Phase 1)."""
    key = (name or "yfinance").strip().lower()
    if key in ("yfinance", "yahoo", ""):
        return YFinanceXauProvider(symbol=symbol, resolution=resolution)
    raise SourceError(
        f"Unknown XAUUSD_PROVIDER {name!r}. Phase 1 ships 'yfinance' only; add a new "
        "PriceProvider subclass and select it here — do not special-case research code."
    )

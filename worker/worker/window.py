"""Research window arithmetic (R4).

  history_start ────────────────── discovery (4 months) ────────── out-of-sample (2 months)
      START_DATE - HISTORY_MONTHS           (by TRADING DAY)                  (touched once)

The split is frozen in `run_config` under key `split` on the first backtest run, so a later
config edit cannot silently move the out-of-sample boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import psycopg

from .config import Settings
from .db import repo
from .timez import add_months, iter_trading_days, today_tehran

SPLIT_KEY = "split"
METHOD_VERSION = "phase1-v1"


@dataclass(frozen=True)
class Split:
    history_start: date
    start_date: date
    discovery_end: date
    outofsample_end: date
    trading_weekdays: set[int]

    def days(self, name: str) -> list[date]:
        if name == "discovery":
            return list(iter_trading_days(self.start_date, self.discovery_end, self.trading_weekdays))
        if name == "outofsample":
            return list(iter_trading_days(self.discovery_end, self.outofsample_end, self.trading_weekdays))
        raise ValueError(f"unknown split {name!r} (use discovery|outofsample)")

    def as_dict(self) -> dict[str, str]:
        return {
            "history_start": self.history_start.isoformat(),
            "start_date": self.start_date.isoformat(),
            "discovery_end": self.discovery_end.isoformat(),
            "outofsample_end": self.outofsample_end.isoformat(),
        }


def compute_split(settings: Settings, *, end: date | None = None) -> Split:
    if settings.start_date is None:
        raise ValueError("START_DATE is required (first replayed decision day)")
    start = settings.start_date
    discovery_end = add_months(start, settings.discovery_months)
    outofsample_end = add_months(start, settings.discovery_months + 2)
    if end and end > outofsample_end:
        outofsample_end = end
    return Split(
        history_start=add_months(start, -settings.history_months),
        start_date=start,
        discovery_end=discovery_end,
        outofsample_end=outofsample_end,
        trading_weekdays=settings.trading_weekday_set,
    )


def frozen_split(conn: psycopg.Connection, settings: Settings, *, freeze: bool = True) -> Split:
    """Read the frozen split, creating it on first use. Out-of-sample never moves once set."""
    live = compute_split(settings, end=today_tehran())
    stored = repo.get_run_config(conn, SPLIT_KEY)
    if stored is None:
        payload = {**live.as_dict(), "frozen_at": today_tehran().isoformat(), "source": "compute_split"}
        repo.set_run_config(conn, SPLIT_KEY, payload, set_by="worker:window", freeze=freeze)
        return live
    same_days = {k: stored.get(k) for k in ("history_start", "start_date", "discovery_end", "outofsample_end")}
    if same_days != live.as_dict():
        # The frozen window and the current config disagree: the frozen window wins for any
        # out-of-sample work, and we say so loudly rather than silently re-slicing.
        return Split(
            history_start=date.fromisoformat(stored["history_start"]),
            start_date=date.fromisoformat(stored["start_date"]),
            discovery_end=date.fromisoformat(stored["discovery_end"]),
            outofsample_end=date.fromisoformat(stored["outofsample_end"]),
            trading_weekdays=settings.trading_weekday_set,
        )
    return live


def assert_in_split(day: date, split: Split, name: str) -> None:
    """Hard guard used by the query builder: a 'discovery' run may not read OOS days (T5)."""
    if name == "discovery" and day >= split.discovery_end:
        raise PermissionError(
            f"{day} is in the out-of-sample window (starts {split.discovery_end}); the run is "
            "configured for discovery. Re-run with --split outofsample if that is intended."
        )
    if name == "outofsample" and day < split.discovery_end:
        raise PermissionError(
            f"{day} is a discovery day; a run configured for outofsample may not use it."
        )

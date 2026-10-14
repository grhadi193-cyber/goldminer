"""The anti-leakage read layer (R3/R4/T1–T6).

Everything a replay, a decision, a state snapshot or an exploratory UI request reads must
come through `ReadContext`. It enforces three things at one place:

  1. EVENT-TIME BOUND — a row is visible only if its event timestamp (price `ts`,
     message `published_at`) is <= `as_of`.
  2. VERSION RULE — for messages, the highest version whose *edit* was ingested by `as_of`
     wins (implemented once in SQL: `messages_asof`, migration 0003).
  3. SPLIT BOUND — a run tagged `discovery` cannot read a day at/after `discovery_end`, and
     an `outofsample` run cannot read discovery days (T5). Frozen split lives in run_config.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

import psycopg

from ..config import Settings
from ..db import repo
from ..errors import AntiLeakageError
from ..timez import day_bounds_utc, local_ts
from ..window import Split, frozen_split


def split_bounds(split: Split, name: str, settings: Settings) -> tuple[datetime, datetime]:
    """Inclusive UTC [start, end] of the split's readable window."""
    if name == "discovery":
        start_day, end_day = split.start_date, split.discovery_end - timedelta(days=1)
    elif name == "outofsample":
        start_day = split.discovery_end
        end_day = split.outofsample_end
    elif name == "demo":
        start_day, end_day = split.history_start, split.outofsample_end
    elif name == "live":
        # Live mode is not bounded by the research split: it reads "now".
        return datetime(2000, 1, 1, tzinfo=timezone.utc), datetime(2100, 1, 1, tzinfo=timezone.utc)
    else:
        raise ValueError(f"unknown split {name!r}")
    start, _ = day_bounds_utc(start_day, settings.open_time, settings.flat_time)
    _, end = day_bounds_utc(end_day, settings.open_time, settings.flat_time)
    return start, end


@dataclass
class ReadContext:
    """A time-bounded view of the database. Construct it, pass it around, nothing else."""

    conn: psycopg.Connection
    as_of: datetime
    split_name: str
    split: Split
    settings: Settings
    locked: bool = True

    # ------------------------------------------------------------------ construction

    @classmethod
    def build(
        cls, conn: psycopg.Connection, settings: Settings, *, as_of: datetime, split_name: str
    ) -> "ReadContext":
        split = frozen_split(conn, settings)
        start, end = split_bounds(split, split_name, settings)
        if split_name == "live":
            return cls(conn=conn, as_of=as_of, split_name=split_name, split=split, settings=settings)
        if not (start <= as_of <= end + timedelta(hours=1)):
            raise AntiLeakageError(
                f"as_of {as_of.isoformat()} is outside the {split_name} window "
                f"[{start.isoformat()} .. {end.isoformat()}]"
            )
        return cls(conn=conn, as_of=as_of, split_name=split_name, split=split, settings=settings)

    # ------------------------------------------------------------------ guards

    def _check(self, ts: datetime, what: str) -> datetime:
        if ts.tzinfo is None:
            raise AntiLeakageError(f"{what}: naive timestamp {ts!r} — always pass UTC-aware values")
        if self.locked and ts > self.as_of:
            raise AntiLeakageError(
                f"{what}: event time {ts.isoformat()} is after as_of {self.as_of.isoformat()} (R3)"
            )
        start, end = split_bounds(self.split, self.split_name, self.settings)
        if self.split_name != "live" and not (start - timedelta(days=1) <= ts <= end + timedelta(hours=1)):
            raise AntiLeakageError(
                f"{what}: event time {ts.isoformat()} is outside the {self.split_name} window (R4/T5)"
            )
        return ts

    def assert_day_allowed(self, day: date) -> None:
        """Cheap day-level guard used by the backtest loop (T5)."""
        if self.split_name == "discovery" and day >= self.split.discovery_end:
            raise AntiLeakageError(f"{day} is an out-of-sample day but this run is 'discovery' (T5)")
        if self.split_name == "outofsample" and day < self.split.discovery_end:
            raise AntiLeakageError(f"{day} is a discovery day but this run is 'outofsample' (T5)")

    # ------------------------------------------------------------------ prices

    def last_bar(self, symbol: str, resolution: str, *, source: str | None = None) -> dict[str, Any] | None:
        self._check(self.as_of, f"last_bar({symbol},{resolution})")
        return repo.last_bar_at_or_before(self.conn, source, symbol, resolution, self.as_of)

    def first_bar_at_or_after(
        self, symbol: str, resolution: str, *, source: str | None = None, until: datetime | None = None
    ) -> dict[str, Any] | None:
        if until is not None:
            self._check(until, f"first_bar_at_or_after({symbol},{resolution}).until")
        return repo.first_bar_at_or_after(
            self.conn, source, symbol, resolution, self.as_of, until=until or self.as_of + timedelta(hours=24)
        )

    def bars_between(
        self,
        symbol: str,
        resolution: str,
        start: datetime,
        end: datetime,
        *,
        source: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        self._check(start, f"bars_between({symbol},{resolution}).start")
        self._check(end, f"bars_between({symbol},{resolution}).end")
        return repo.bars_between(self.conn, symbol, resolution, start, end, source=source, limit=limit)

    def session_price(self, symbol: str, resolution: str, *, at: datetime, source: str | None = None) -> float | None:
        """Price of the first bar at/after `at` (used for 08:00 decision prices)."""
        self._check(at, f"session_price({symbol}).at")
        row = repo.first_bar_at_or_after(
            self.conn, source, symbol, resolution, at, until=min(self.as_of, at + timedelta(hours=3))
        )
        return float(row["close"]) if row else None

    # ------------------------------------------------------------------ messages

    def messages(
        self,
        *,
        channel_ids: Sequence[int] | None = None,
        since: datetime | None = None,
        with_extractions: bool = True,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if since is not None:
            self._check(since, "messages.since")
        rows = repo.asof_messages(
            self.conn, self.as_of, channel_ids=channel_ids, since=since, with_extractions=with_extractions
        )
        return rows[:limit] if limit else rows

    def messages_since(self, since: datetime) -> list[dict[str, Any]]:
        """Visible messages with published_at >= since (used by the state builder and tests)."""
        return self.messages(since=since)

    # ------------------------------------------------------------------ extractions

    def extraction_for(self, message_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            select e.extraction_json, e.status, e.model
            from extractions e
            join messages_asof(%s) v on v.id = e.message_id
            where e.message_id = %s
            """,
            (self.as_of, message_id),
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------ day outcome helpers

    def tehran_session(self, day: date) -> tuple[datetime, datetime]:
        return day_bounds_utc(day, self.settings.open_time, self.settings.flat_time)

    def decision_moment(self, day: date, checkpoint_local: Any) -> datetime:
        ts = local_ts(day, checkpoint_local)
        self._check(ts, f"decision_moment({day},{checkpoint_local})")
        return ts


__all__ = ["ReadContext", "split_bounds"]

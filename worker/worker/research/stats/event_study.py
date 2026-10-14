"""Event study over signal messages (7.2).

For every extracted message event we compute the forward LOG-return of the measurement
series at +30m/+1h/+3h/EOD, signed by the message's stated direction, and subtract the
one-sided cost of the measured asset (`spread + slippage` — exactly what T8 checks).

Aggregation is per (channel, kind, direction, horizon), reporting:
  * net mean lift in bps        (event-weighted)
  * bootstrap 95% CI            (block bootstrap by DAY)
  * n events and n days
  * a day-level t-test p-value  (only used to rank tests inside the FDR family)
  * which measurement series produced the number
"""

from __future__ import annotations

import logging
from collections import defaultdict

import numpy as np
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import psycopg

from ...config import Settings
from ...timez import day_bounds_utc, iter_trading_days
from ..costs import cost_model
from .bootstrap import block_bootstrap_ci, sign_agreement, t_p_value
from .fdr import Family
from .series import (
    ALL_HORIZONS,
    HORIZON_MINUTES,
    DayMinutes,
    EventReturn,
    MeasurementSeries,
    forward_return_bps,
    load_day_minutes,
    signed,
)

log = logging.getLogger(__name__)

KIND_VARIANTS = ("all", "news", "rumor", "price_quote", "opinion")
COIN_ASSETS = {"emami_coin", "whole_coin", "half_coin", "quarter_coin"}
DIRECTION_VARIANTS = ("up", "down")
PRE_OPEN_PAD_HOURS = 3


@dataclass
class EventStudyResult:
    events: list[EventReturn] = field(default_factory=list)
    measure_counts: dict[str, int] = field(default_factory=dict)
    days_covered: int = 0
    days_missing: int = 0
    assets_used: set[str] = field(default_factory=set)


def _message_rows(conn: psycopg.Connection, start: datetime, end: datetime) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            """
            select m.id as message_id, m.channel_id, c.username, m.published_at, e.extraction_json
            from messages m
            join channels c on c.id = m.channel_id
            join extractions e on e.message_id = m.id
            where e.status = 'ok'
              and m.published_at >= %s and m.published_at <= %s
              and c.active
            order by m.published_at asc
            """,
            (start, end),
        ).fetchall()
    ]


def build_event_returns(
    conn: psycopg.Connection,
    settings: Settings,
    series: MeasurementSeries,
    *,
    start_day: date,
    end_day: date,
    daily_realized_bps: dict[date, float] | None = None,
    progress: bool = True,
) -> EventStudyResult:
    """All event-level forward returns for one measurement series over a day range."""
    result = EventStudyResult()
    days = list(iter_trading_days(start_day, end_day, settings.trading_weekday_set))
    for i, day in enumerate(days):
        open_ts, flat_ts = day_bounds_utc(day, settings.open_time, settings.flat_time)
        minutes = load_day_minutes(conn, series, day, open_local=settings.open_time,
                                   flat_local=settings.flat_time, pad_hours=PRE_OPEN_PAD_HOURS)
        if minutes is None:
            result.days_missing += 1
            continue
        result.days_covered += 1
        rows = _message_rows(conn, open_ts - timedelta(hours=PRE_OPEN_PAD_HOURS), flat_ts)
        for row in rows:
            ej = row["extraction_json"] or {}
            direction = ej.get("direction")
            if direction not in ("up", "down"):
                continue
            kind = ej.get("kind") or "opinion"
            published: datetime = row["published_at"]
            # The cost subtracted is the cost of the asset the TRADER would trade on this
            # signal (gold by default, coin for coin-specific signals) — not the cost of
            # whatever series we happened to measure the return with.
            traded_asset = "coin" if ej.get("quoted_asset") in COIN_ASSETS else "gold"
            cm = cost_model(settings, traded_asset)
            for horizon in ALL_HORIZONS:
                gross, measure = _horizon_return(minutes, published, horizon, flat_ts, daily_realized_bps, day)
                if gross is None:
                    continue
                net = signed(gross, direction) - cm.signal_cost_bps
                result.events.append(
                    EventReturn(
                        day=day, channel_id=int(row["channel_id"]), username=row["username"],
                        direction=direction, kind=kind, horizon=horizon,
                        gross_bps=signed(gross, direction), net_bps=net, cost_bps=cm.signal_cost_bps,
                        measure=measure, asset=traded_asset,
                        message_id=int(row["message_id"]), published_at=published,
                    )
                )
                result.measure_counts[measure] = result.measure_counts.get(measure, 0) + 1
                result.assets_used.add(traded_asset)
        if progress and (i + 1) % 25 == 0:
            log.info("event study: %d/%d days, %d events", i + 1, len(days), len(result.events))
    return result


def _horizon_return(
    minutes: DayMinutes,
    published: datetime,
    horizon: str,
    flat_ts: datetime,
    daily_realized_bps: dict[date, float] | None,
    day: date,
) -> tuple[float | None, str]:
    if horizon == "eod":
        value = forward_return_bps(minutes, published, _minutes_to_close(published, flat_ts), flat_ts=flat_ts)
        if value is not None:
            return value, minutes.series.measure
        if daily_realized_bps and day in daily_realized_bps:
            return daily_realized_bps[day], "daily_proxy"
        return None, minutes.series.measure
    value = forward_return_bps(minutes, published, HORIZON_MINUTES[horizon], flat_ts=flat_ts)
    return (value, minutes.series.measure) if value is not None else (None, minutes.series.measure)


def _minutes_to_close(published: datetime, flat_ts: datetime) -> int:
    delta = (flat_ts - published).total_seconds() / 60.0
    return int(max(1.0, delta))


@dataclass
class Effect:
    channel_id: int
    username: str
    kind: str
    direction: str
    horizon: str
    n_events: int
    n_days: int
    net_bps: float
    gross_bps: float
    ci_low: float
    ci_high: float
    p_value: float
    sign_consistency: float
    measures: dict[str, int]

    def key(self) -> tuple:
        return ("event", self.username, self.kind, self.direction, self.horizon)


def aggregate(
    events: list[EventReturn], *, channels: dict[int, str] | None = None
) -> list[Effect]:
    """Group events into channel x kind x direction x horizon effects (the FDR family)."""
    names = channels or {e.channel_id: e.username for e in events}
    buckets: dict[tuple[int, str, str, str], list[EventReturn]] = defaultdict(list)
    for event in events:
        buckets[(event.channel_id, event.kind, event.direction, event.horizon)].append(event)
        buckets[(event.channel_id, "all", event.direction, event.horizon)].append(event)

    out: list[Effect] = []
    for (channel_id, kind, direction, horizon), rows in buckets.items():
        samples = [(r.day, r.net_bps) for r in rows]
        mean, lo, hi = block_bootstrap_ci(samples)
        if mean != mean:  # NaN
            continue
        gross_samples = [(r.day, r.gross_bps) for r in rows]
        gross_mean = float(np.mean([v for _, v in gross_samples])) if gross_samples else float("nan")
        measures: dict[str, int] = defaultdict(int)
        for r in rows:
            measures[r.measure] += 1
        out.append(
            Effect(
                channel_id=channel_id,
                username=names.get(channel_id, str(channel_id)),
                kind=kind,
                direction=direction,
                horizon=horizon,
                n_events=len(rows),
                n_days=len({r.day for r in rows}),
                net_bps=mean,
                gross_bps=gross_mean,
                ci_low=lo,
                ci_high=hi,
                p_value=t_p_value(samples),
                sign_consistency=sign_agreement(samples),
                measures=dict(measures),
            )
        )
    return sorted(out, key=lambda e: (e.username, e.kind, e.direction, e.horizon))


def add_effects_to_family(effects: list[Effect], family: Family, *, min_events: int = 5) -> None:
    for effect in effects:
        if effect.n_events < min_events:
            continue
        family.add(
            effect.key(),
            effect.p_value,
            net_bps=round(effect.net_bps, 4),
            gross_bps=round(effect.gross_bps, 4),
            ci_low=round(effect.ci_low, 4),
            ci_high=round(effect.ci_high, 4),
            n_events=effect.n_events,
            n_days=effect.n_days,
            sign_consistency=round(effect.sign_consistency, 4) if effect.sign_consistency == effect.sign_consistency else None,
            measures=effect.measures,
        )


# --------------------------------------------------------------------------- agreement events

@dataclass
class AgreementEvent:
    day: date
    start_ts: datetime
    direction: str
    k_channels: int
    channels: list[str]
    net_bps: dict[str, float] = field(default_factory=dict)   # horizon -> net lift for UP direction
    gross_bps: dict[str, float] = field(default_factory=dict)


def agreement_events(
    settings: Settings,
    series: MeasurementSeries,
    day_minutes: DayMinutes,
    rows: list[dict],
    *,
    window_minutes: int = 30,
    max_k: int = 5,
) -> list[AgreementEvent]:
    """'k channels agree in direction within 30m' events, greedily de-overlapped.

    For each direction we sweep the day's messages; whenever >= 2 distinct channels post the
    same direction inside a 30-minute window we emit ONE event at the window start (with the
    number of distinct channels that agreed) and skip ahead past that window.
    """
    flat_ts = day_minutes.ts[-1] if len(day_minutes.ts) else None
    if flat_ts is None:
        return []
    from datetime import timezone as _tz

    flat_dt = datetime.fromtimestamp(float(flat_ts), tz=_tz.utc)
    events: list[AgreementEvent] = []
    for direction in ("up", "down"):
        relevant = sorted(
            (r for r in rows if (r["extraction_json"] or {}).get("direction") == direction),
            key=lambda r: r["published_at"],
        )
        i = 0
        while i < len(relevant):
            start = relevant[i]["published_at"]
            window_end = start + timedelta(minutes=window_minutes)
            inside = [r for r in relevant[i:] if r["published_at"] <= window_end]
            channels = sorted({r["username"] for r in inside})
            if len(channels) >= 2:
                event = AgreementEvent(day=day_minutes.day, start_ts=start, direction=direction,
                                       k_channels=len(channels), channels=channels)
                for horizon in ALL_HORIZONS:
                    if horizon == "eod":
                        gross = forward_return_bps(day_minutes, start, _minutes_to_close(start, flat_dt), flat_ts=flat_dt)
                    else:
                        gross = forward_return_bps(day_minutes, start, HORIZON_MINUTES[horizon], flat_ts=flat_dt)
                    if gross is not None:
                        event.gross_bps[horizon] = gross
                        cm = cost_model(settings, "gold")
                        event.net_bps[horizon] = signed(gross, direction) - cm.signal_cost_bps
                events.append(event)
                i = next((j for j, r in enumerate(relevant) if r["published_at"] > window_end), len(relevant))
            else:
                i += 1
    return events


__all__ = [
    "EventStudyResult",
    "Effect",
    "AgreementEvent",
    "build_event_returns",
    "aggregate",
    "add_effects_to_family",
    "agreement_events",
    "KIND_VARIANTS",
    "DIRECTION_VARIANTS",
]

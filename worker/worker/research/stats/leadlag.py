"""Lead/lag analysis (7.2) — signed signal intensity vs. returns, lags -60..+60 minutes.

Construction, per channel per day:
  * intensity[t] = sum of extraction intensities of messages published in minute t, signed by
    direction (up = +1, down = -1). "None"/"flat" messages contribute nothing.
  * returns[t]   = log-return of the measurement series in minute t.

SIGN CONVENTION (the source of most confusion in this area, so it is stated once, here):

        lag = (signal minute) - (return minute)

  * lag <= 0  -> the signal comes BEFORE the correlated move  -> the channel LEADS
  * lag >  0  -> the correlated move already happened         -> the channel LAGS

This matches the spec's rule verbatim: "a channel leads if its significant positive lift
occurs at lags <= 0 (signal before move)". Correlations are positive when up-signals line up
with up-moves, so a LEAD is a significant POSITIVE correlation at lag <= 0.

Significance is a day-level t-test on the per-day correlations (blocking by day keeps the
intraday overlap from inflating n), and it enters the FDR family, so `p_value` here is a
ranking device — the reported effect is the correlation itself with a bootstrap CI.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import numpy as np
import psycopg
from scipy import stats

from ...config import Settings
from ...timez import day_bounds_utc, iter_trading_days
from .bootstrap import block_bootstrap_ci
from .fdr import Family
from .series import DayMinutes, MeasurementSeries, load_day_minutes

log = logging.getLogger(__name__)

LAG_GRID = list(range(-60, 61, 5))       # minutes, lag = signal minute - return minute
MIN_EVENTS_FOR_LABEL = 20                # M7: channels need >= 20 events to be labelled


@dataclass
class ChannelLagPoint:
    lag: int
    corr: float
    p_value: float
    n_days: int
    day_corrs: list[float]


@dataclass
class ChannelLeadLag:
    channel_id: int
    username: str
    n_events: int
    peak_lag: int
    peak_corr: float
    best_leading: tuple[int, float, float] | None      # (lag, corr, q) at lags <= 0
    best_lagging: tuple[int, float, float] | None      # (lag, corr, q) at lags > 0
    points: list[ChannelLagPoint]
    label: str = "noise"
    label_reason: str = ""


def _intensity_series(rows: list[dict], minutes: DayMinutes) -> np.ndarray:
    """Signed intensity per bar of the day (0 where nothing was posted)."""
    out = np.zeros(len(minutes.ts), dtype=float)
    if len(minutes.ts) == 0:
        return out
    for row in rows:
        ej = row["extraction_json"] or {}
        direction = ej.get("direction")
        if direction not in ("up", "down"):
            continue
        idx = minutes.index_at_or_after(row["published_at"])
        if idx is None:
            continue
        sign = 1.0 if direction == "up" else -1.0
        out[idx] += sign * float(ej.get("intensity") or 1.0)
    return out


def _returns_series(minutes: DayMinutes) -> np.ndarray:
    if len(minutes.close) < 2:
        return np.zeros(len(minutes.close), dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        rets = np.zeros(len(minutes.close), dtype=float)
        rets[1:] = np.log(minutes.close[1:] / minutes.close[:-1]) * 1e4
    return rets


def _corr_at_lag(intensity: np.ndarray, returns: np.ndarray, lag: int) -> float:
    """Correlation of intensity[t] with the return at minute t - lag.

    lag = signal - return, so:
        lag = -30 -> intensity[t] pairs with returns[t + 30]  (signal BEFORE the move)
        lag = +30 -> intensity[t] pairs with returns[t - 30]  (signal AFTER the move)
    """
    n = len(intensity)
    if n < 10:
        return float("nan")
    if lag <= 0:
        shift = -lag
        x = intensity[: n - shift]
        y = returns[shift:]
    else:
        shift = lag
        x = intensity[shift:]
        y = returns[: n - shift]
    if len(x) < 5 or np.std(x) == 0 or np.std(y) == 0:
        return float("nan")
    return float(np.corrcoef(x, y)[0, 1])


def analyse_channel(
    channel_id: int,
    username: str,
    per_day: list[tuple[date, list[dict], DayMinutes]],
) -> ChannelLeadLag:
    points: list[ChannelLagPoint] = []
    n_events = sum(
        sum(1 for r in rows if (r["extraction_json"] or {}).get("direction") in ("up", "down"))
        for _, rows, _ in per_day
    )
    for lag in LAG_GRID:
        day_corrs = [
            _corr_at_lag(_intensity_series(rows, minutes), _returns_series(minutes), lag)
            for _, rows, minutes in per_day
        ]
        day_corrs = [c for c in day_corrs if c == c]
        if len(day_corrs) < 3:
            points.append(ChannelLagPoint(lag=lag, corr=float("nan"), p_value=1.0, n_days=len(day_corrs), day_corrs=day_corrs))
            continue
        values = np.array(day_corrs)
        corr = float(np.mean(values))
        sd = float(np.std(values, ddof=1))
        if sd == 0:
            p = 0.0 if corr != 0 else 1.0
        else:
            t_stat = corr / (sd / np.sqrt(len(values)))
            p = float(2 * stats.t.sf(abs(t_stat), df=len(values) - 1))
        points.append(ChannelLagPoint(lag=lag, corr=corr, p_value=p, n_days=len(values), day_corrs=day_corrs))

    finite = [p for p in points if p.corr == p.corr]
    if not finite:
        return ChannelLeadLag(channel_id, username, n_events, 0, float("nan"), None, None, points,
                              "insufficient", "no usable correlation (missing data)")
    peak = max(finite, key=lambda p: abs(p.corr))
    return ChannelLeadLag(
        channel_id=channel_id,
        username=username,
        n_events=n_events,
        peak_lag=peak.lag,
        peak_corr=peak.corr,
        best_leading=None,
        best_lagging=None,
        points=points,
        label="noise",
        label_reason="pending FDR",
    )


def add_to_family(analysis: list[ChannelLeadLag], family: Family) -> None:
    for channel in analysis:
        for point in channel.points:
            if point.corr != point.corr or point.n_days < 3:
                continue
            family.add(
                ("leadlag", channel.username, f"lag{point.lag:+d}"),
                point.p_value,
                corr=round(point.corr, 5),
                n_days=point.n_days,
            )


def apply_labels(analysis: list[ChannelLeadLag], corrected: list[dict]) -> list[ChannelLeadLag]:
    """Assign lead/lag/noise/insufficient using the FDR-corrected significance."""
    significance: dict[tuple[str, int], dict] = {}
    for row in corrected:
        key = row["key"]
        if len(key) != 3 or key[0] != "leadlag":
            continue
        _, username, lag_token = key
        lag = int(str(lag_token).replace("lag", ""))
        significance[(username, lag)] = row
    for channel in analysis:
        if channel.n_events < MIN_EVENTS_FOR_LABEL:
            channel.label, channel.label_reason = "insufficient", f"only {channel.n_events} events (< {MIN_EVENTS_FOR_LABEL})"
            continue

        def best(candidates: list[ChannelLagPoint]) -> tuple[int, float, float] | None:
            scored = []
            for point in candidates:
                row = significance.get((channel.username, point.lag))
                if row and row["fdr_significant"] and point.corr > 0:
                    scored.append((point.lag, point.corr, row["q_value"]))
            if not scored:
                return None
            return max(scored, key=lambda t: t[1])

        leading = best([p for p in channel.points if p.lag <= 0])
        lagging = best([p for p in channel.points if p.lag > 0])
        channel.best_leading, channel.best_lagging = leading, lagging
        if leading and (not lagging or leading[1] >= lagging[1]):
            channel.label = "lead"
            channel.label_reason = f"significant positive corr {leading[1]:.3f} at lag {leading[0]:+d}m (q={leading[2]:.3f})"
            channel.peak_lag, channel.peak_corr = leading[0], leading[1]
        elif lagging:
            channel.label = "lag"
            channel.label_reason = f"significant positive corr only at lag {lagging[0]:+d}m (q={lagging[2]:.3f})"
            channel.peak_lag, channel.peak_corr = lagging[0], lagging[1]
        else:
            channel.label = "noise"
            channel.label_reason = "no FDR-significant positive correlation on the lag grid"
    return analysis


def collect_per_day(
    conn: psycopg.Connection,
    settings: Settings,
    series: MeasurementSeries,
    days: list[date],
    *,
    channel_usernames: dict[int, str] | None = None,
) -> tuple[dict[int, list[tuple[date, list[dict], DayMinutes]]], dict[int, str]]:
    """Per-channel, per-day (day, message rows, minute series) tuples."""
    per_channel: dict[int, list[tuple[date, list[dict], DayMinutes]]] = defaultdict(list)
    names: dict[int, str] = dict(channel_usernames or {})
    for day in days:
        minutes = load_day_minutes(conn, series, day, open_local=settings.open_time, flat_local=settings.flat_time)
        if minutes is None:
            continue
        open_ts, flat_ts = day_bounds_utc(day, settings.open_time, settings.flat_time)
        rows = [
            dict(r)
            for r in conn.execute(
                """
                select m.id as message_id, m.channel_id, c.username, m.published_at, e.extraction_json
                from messages m
                join channels c on c.id = m.channel_id
                join extractions e on e.message_id = m.id
                where e.status = 'ok' and c.active
                  and m.published_at >= %s and m.published_at <= %s
                """,
                (open_ts, flat_ts),
            ).fetchall()
        ]
        grouped: dict[int, list[dict]] = defaultdict(list)
        for row in rows:
            grouped[int(row["channel_id"])].append(row)
            names[int(row["channel_id"])] = row["username"]
        for channel_id, channel_rows in grouped.items():
            per_channel[channel_id].append((day, channel_rows, minutes))
    return per_channel, names


def run_leadlag(
    conn: psycopg.Connection,
    settings: Settings,
    series: MeasurementSeries,
    days: list[date],
    family: Family,
    *,
    channel_usernames: dict[int, str] | None = None,
) -> list[ChannelLeadLag]:
    per_channel, names = collect_per_day(conn, settings, series, days, channel_usernames=channel_usernames)
    analysis = [analyse_channel(cid, names.get(cid, str(cid)), rows) for cid, rows in sorted(per_channel.items())]
    add_to_family(analysis, family)
    return analysis


def bootstrap_peak_ci(channel: ChannelLeadLag, *, n_boot: int = 1000, seed: int = 7) -> tuple[float, float]:
    """CI for the peak correlation, resampling DAY-correlations with replacement."""
    point = next((p for p in channel.points if p.lag == channel.peak_lag), None)
    if point is None or not point.day_corrs:
        return (float("nan"), float("nan"))
    samples = [(date(2026, 1, 1) + timedelta(days=i), c) for i, c in enumerate(point.day_corrs)]
    mean, lo, hi = block_bootstrap_ci(samples, n_boot=n_boot, seed=seed)
    return (lo, hi)


__all__ = [
    "ChannelLeadLag",
    "ChannelLagPoint",
    "LAG_GRID",
    "MIN_EVENTS_FOR_LABEL",
    "analyse_channel",
    "add_to_family",
    "apply_labels",
    "run_leadlag",
    "collect_per_day",
    "bootstrap_peak_ci",
]

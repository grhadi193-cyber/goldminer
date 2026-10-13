"""Channel scoring + leaderboard material (7.2, M7).

Orchestrates the pre-registered family, applies BH-FDR once, writes `channel_scores`, and
returns a plain-data artifact that the report and the UI both read.

Pre-registered family (FDR q=0.10), assembled here and ONLY here:
    event study      channel x kind ∈ {all,news,rumor,price_quote,opinion}
                     x direction ∈ {up,down} x horizon ∈ {30m,1h,3h,eod}
    agreement        k ∈ {2,3,4,5} x horizon ∈ {30m,1h,3h,eod}
    lead/lag         channel x lag grid (minutes, step 5, -60..+60)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

import psycopg

from ...config import Settings
from ...db import repo
from ...jsonutil import jsonb
from ...timez import day_bounds_utc, iter_trading_days
from .. import prices_view as pv
from ..costs import cost_model
from . import event_study, leadlag
from .fdr import Family, bh_correct
from .series import ALL_HORIZONS, MeasurementSeries, load_day_minutes, pick_series

log = logging.getLogger(__name__)

METHOD_VERSION = "phase1-v1"
PUMPER_DEVIATION_PCT = 1.0
PUMPER_MIN_QUOTES = 10


@dataclass
class ChannelSummary:
    channel_id: int
    username: str
    active: bool
    n_events: int
    n_quotes: int
    lifts: dict[str, float] = field(default_factory=dict)
    gross_lifts: dict[str, float] = field(default_factory=dict)
    cis: dict[str, tuple[float, float]] = field(default_factory=dict)
    q_values: dict[str, float] = field(default_factory=dict)
    lead_lag_minutes: int | None = None
    label: str = "insufficient"
    label_reason: str = ""
    pumper_flag: bool = False
    quoted_bias_pct: float | None = None
    burst_share: float | None = None
    measures: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "username": self.username,
            "active": self.active,
            "n_events": self.n_events,
            "n_quotes": self.n_quotes,
            "lifts_net_bps": self.lifts,
            "lifts_gross_bps": self.gross_lifts,
            "ci_95": {k: list(v) for k, v in self.cis.items()},
            "q_values": self.q_values,
            "leads_lag_minutes": self.lead_lag_minutes,
            "label": self.label,
            "label_reason": self.label_reason,
            "pumper_flag": self.pumper_flag,
            "quoted_bias_pct": self.quoted_bias_pct,
            "burst_share": self.burst_share,
            "measures": self.measures,
        }


@dataclass
class ScoringArtifact:
    split: str
    method_version: str
    start_day: str
    end_day: str
    measurement_series: str
    family: dict[str, Any]
    channels: list[dict[str, Any]] = field(default_factory=list)
    agreement: list[dict[str, Any]] = field(default_factory=list)
    measure_coverage: dict[str, int] = field(default_factory=dict)
    days_covered: int = 0
    days_missing: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "split": self.split,
            "method_version": self.method_version,
            "window": {"start": self.start_day, "end": self.end_day},
            "measurement_series": self.measurement_series,
            "family": self.family,
            "channels": self.channels,
            "agreement": self.agreement,
            "measure_coverage": self.measure_coverage,
            "days_covered": self.days_covered,
            "days_missing": self.days_missing,
            "notes": self.notes,
        }


def run_scoring(
    conn: psycopg.Connection,
    settings: Settings,
    *,
    split_name: str,
    days: list[date],
    persist: bool = True,
    asof_date: date | None = None,
) -> ScoringArtifact:
    if not days:
        raise ValueError("no days to score")
    start_day, end_day = days[0], days[-1]
    series_list = pick_series(conn, start=start_day, end=end_day)
    if not series_list:
        raise ValueError("no measurement series has any data in this window (R8: nothing to measure)")
    series = series_list[0]
    log.info("scoring on measurement series %s/%s (%s)", series.source, series.symbol, series.measure)

    daily_realized = {
        row["day_date"]: float(row["realized_bps"])
        for row in conn.execute(
            "select day_date, realized_bps from day_outcomes where day_date = any(%s)", (days,)
        ).fetchall()
    }

    family = Family(name="phase1-preregistered", q=0.10)

    # ---- 1) event study -------------------------------------------------------
    study = event_study.build_event_returns(
        conn, settings, series, start_day=start_day, end_day=end_day, daily_realized_bps=daily_realized
    )
    effects = event_study.aggregate(study.events)
    event_study.add_effects_to_family(effects, family)
    log.info("event study: %d events, %d effects", len(study.events), len(effects))

    # ---- 2) agreement events --------------------------------------------------
    agreement_effects = _agreement_family(conn, settings, series, days, family)

    # ---- 3) lead/lag ----------------------------------------------------------
    channels_meta = {
        r["id"]: {"username": r["username"], "active": r["active"]}
        for r in conn.execute("select id, username, active from channels").fetchall()
    }
    ll_analysis = leadlag.run_leadlag(
        conn, settings, series, days, family,
        channel_usernames={cid: meta["username"] for cid, meta in channels_meta.items()},
    )

    # ---- 4) BH-FDR over the whole family -------------------------------------
    corrected = bh_correct(family)
    family_summary = {
        "name": family.name,
        "size": len(corrected),
        "q_threshold": family.q,
        "significant": sum(1 for r in corrected if r["fdr_significant"]),
    }
    log.info("FDR family %s: %d tests, %d significant", family.name, len(corrected), family_summary["significant"])
    ll_analysis = leadlag.apply_labels(ll_analysis, corrected)

    # ---- 5) assemble per-channel summaries ------------------------------------
    by_channel: dict[int, ChannelSummary] = {}
    for effect in effects:
        summary = by_channel.setdefault(
            effect.channel_id,
            ChannelSummary(
                channel_id=effect.channel_id,
                username=effect.username,
                active=bool(channels_meta.get(effect.channel_id, {}).get("active", True)),
                n_events=0,
                n_quotes=0,
            ),
        )
        summary.n_events = max(summary.n_events, effect.n_events)
        if effect.kind in ("all",):
            summary.lifts[effect.horizon] = round(effect.net_bps, 4)
            summary.gross_lifts[effect.horizon] = round(effect.gross_bps, 4)
            summary.cis[effect.horizon] = (round(effect.ci_low, 4), round(effect.ci_high, 4))
            for key, value in (effect.measures or {}).items():
                summary.measures[key] = summary.measures.get(key, 0) + value
    by_username = {summary.username: summary for summary in by_channel.values()}
    for row in corrected:
        key = row["key"]
        if len(key) != 5 or key[0] != "event":
            continue
        _, username, kind_name, direction, horizon = key
        if kind_name == "all" and direction == "up":
            summary = by_username.get(username)
            if summary is not None:
                summary.q_values[horizon] = row["q_value"]
    for analysis in ll_analysis:
        summary = by_channel.setdefault(
            analysis.channel_id,
            ChannelSummary(
                channel_id=analysis.channel_id,
                username=analysis.username,
                active=bool(channels_meta.get(analysis.channel_id, {}).get("active", True)),
                n_events=analysis.n_events,
                n_quotes=0,
            ),
        )
        summary.lead_lag_minutes = analysis.peak_lag
        summary.label = analysis.label
        summary.label_reason = analysis.label_reason
        summary.n_events = max(summary.n_events, analysis.n_events)

    _attach_quote_behaviour(conn, settings, days, by_channel)

    # ---- 6) persist -----------------------------------------------------------
    if persist:
        _persist(conn, settings, split_name=split_name, asof_date=asof_date or end_day,
                 summaries=list(by_channel.values()), family_summary=family_summary)
    return ScoringArtifact(
        split=split_name,
        method_version=METHOD_VERSION,
        start_day=start_day.isoformat(),
        end_day=end_day.isoformat(),
        measurement_series=f"{series.source}/{series.symbol}/{series.resolution} ({series.measure})",
        family=family_summary,
        channels=[s.as_dict() for s in sorted(by_channel.values(), key=lambda s: -(s.lifts.get("eod") or -1e9))],
        agreement=agreement_effects,
        measure_coverage=study.measure_counts,
        days_covered=study.days_covered,
        days_missing=study.days_missing,
        notes=[
            "lifts are NET of one-sided costs (spread + slippage) on the measured asset",
            "CIs are 95% block-bootstrap by day; q-values are Benjamini-Hochberg over the pre-registered family",
            "anything outside this family is exploratory and uncorrected",
        ],
    )


def _agreement_family(
    conn: psycopg.Connection,
    settings: Settings,
    series: MeasurementSeries,
    days: list[date],
    family: Family,
) -> list[dict[str, Any]]:
    """'k channels agree within 30m' — pre-registered variants k=2..5 x horizon."""
    buckets: dict[tuple[int, str], list[tuple[date, float]]] = {}
    for day in days:
        minutes = load_day_minutes(conn, series, day, open_local=settings.open_time, flat_local=settings.flat_time)
        if minutes is None:
            continue
        open_ts, flat_ts = day_bounds_utc(day, settings.open_time, settings.flat_time)
        rows = [
            dict(r)
            for r in conn.execute(
                """
                select m.published_at, c.username, c.id as channel_id, e.extraction_json
                from messages m
                join channels c on c.id = m.channel_id
                join extractions e on e.message_id = m.id
                where e.status = 'ok' and c.active and m.published_at >= %s and m.published_at <= %s
                """,
                (open_ts, flat_ts),
            ).fetchall()
        ]
        for event in event_study.agreement_events(settings, series, minutes, rows):
            for horizon, net in event.net_bps.items():
                for k in range(2, 6):
                    if event.k_channels >= k:
                        buckets.setdefault((k, horizon), []).append((day, net))
    out: list[dict[str, Any]] = []
    from .bootstrap import block_bootstrap_ci, sign_agreement, t_p_value

    for (k, horizon), samples in sorted(buckets.items()):
        mean, lo, hi = block_bootstrap_ci(samples)
        p = t_p_value(samples)
        family.add(("agreement", f"k{k}", horizon), p, net_bps=round(mean, 4), n_events=len(samples))
        out.append(
            {
                "k": k,
                "horizon": horizon,
                "n_events": len(samples),
                "net_bps": round(mean, 4),
                "ci_low": round(lo, 4),
                "ci_high": round(hi, 4),
                "p_value": round(p, 6),
                "sign_consistency": round(sign_agreement(samples), 4),
            }
        )
    return out


def _attach_quote_behaviour(
    conn: psycopg.Connection, settings: Settings, days: list[date], by_channel: dict[int, ChannelSummary]
) -> None:
    """Quoted-price bias + burst share, computed from visible data only (per channel)."""
    start = min(days)
    end = max(days)
    start_ts, _ = day_bounds_utc(start, settings.open_time, settings.flat_time)
    _, end_ts = day_bounds_utc(end, settings.open_time, settings.flat_time)
    rows = conn.execute(
        """
        select m.channel_id, c.username, c.active, m.published_at, e.extraction_json
        from messages m
        join channels c on c.id = m.channel_id
        join extractions e on e.message_id = m.id
        where e.status = 'ok' and m.published_at >= %s and m.published_at <= %s
        order by m.channel_id, m.published_at
        """,
        (start_ts, end_ts),
    ).fetchall()
    per_channel: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        per_channel.setdefault(int(row["channel_id"]), []).append(dict(row))

    for channel_id, channel_rows in per_channel.items():
        first = channel_rows[0]
        summary = by_channel.setdefault(
            channel_id,
            ChannelSummary(channel_id=channel_id, username=str(first["username"]),
                           active=bool(first["active"]), n_events=0, n_quotes=0),
        )
        deviations: list[float] = []
        for row in channel_rows:
            ej = row["extraction_json"] or {}
            quoted = ej.get("quoted_price")
            asset = ej.get("quoted_asset")
            if not quoted or asset not in ("emami_coin", "abshodeh", "gold_18k_toman_per_gram", "whole_coin"):
                continue
            real = _real_price_at(conn, asset, row["published_at"])
            if not real:
                continue
            deviations.append((float(quoted) / real - 1.0) * 100.0)
        summary.n_quotes = len(deviations)
        if deviations:
            summary.quoted_bias_pct = round(sum(deviations) / len(deviations), 3)
        times = [r["published_at"] for r in channel_rows]
        bursts = 0
        for i, ts in enumerate(times):
            if any(abs((other - ts).total_seconds()) <= 600 for j, other in enumerate(times) if j != i):
                bursts += 1
        summary.burst_share = round(bursts / len(times), 4) if times else None
        summary.pumper_flag = bool(
            summary.quoted_bias_pct is not None
            and summary.quoted_bias_pct >= PUMPER_DEVIATION_PCT
            and summary.n_quotes >= PUMPER_MIN_QUOTES
        )


def _real_price_at(conn: psycopg.Connection, asset: str, when: datetime) -> float | None:
    candidates = {
        "emami_coin": pv.DOMESTIC_COIN,
        "whole_coin": pv.DOMESTIC_COIN,
        "abshodeh": pv.DOMESTIC_GOLD,
        "gold_18k_toman_per_gram": pv.DOMESTIC_GOLD,
    }.get(asset)
    if candidates is None:
        return None
    for source, symbol, resolution in candidates:
        row = conn.execute(
            """
            select close from price_bars
            where source = %s and symbol = %s and resolution = %s and ts <= %s
            order by ts desc limit 1
            """,
            (source, symbol, resolution, when),
        ).fetchone()
        if row:
            return float(row["close"])
    return None


def _persist(
    conn: psycopg.Connection,
    settings: Settings,
    *,
    split_name: str,
    asof_date: date,
    summaries: list[ChannelSummary],
    family_summary: dict[str, Any],
) -> None:
    cost = cost_model(settings, "gold")
    for summary in summaries:
        conn.execute(
            """
            insert into channel_scores
                (channel_id, asof_date, horizon, lead_lag_minutes, lift_bps_30m, lift_bps_1h, lift_bps_3h, lift_bps_eod,
                 net_of_costs, n_events, ci_low_bps, ci_high_bps, fdr_corrected, q_value, label, pumper_flag,
                 quoted_bias_pct, method_version, extra_json, computed_at)
            values (%s, %s, 'eod', %s, %s, %s, %s, %s, true, %s, %s, %s, true, %s, %s, %s, %s, %s, %s, now())
            on conflict (channel_id, asof_date, horizon, method_version) do update
              set lead_lag_minutes = excluded.lead_lag_minutes,
                  lift_bps_30m = excluded.lift_bps_30m, lift_bps_1h = excluded.lift_bps_1h,
                  lift_bps_3h = excluded.lift_bps_3h, lift_bps_eod = excluded.lift_bps_eod,
                  n_events = excluded.n_events, ci_low_bps = excluded.ci_low_bps, ci_high_bps = excluded.ci_high_bps,
                  q_value = excluded.q_value, label = excluded.label, pumper_flag = excluded.pumper_flag,
                  quoted_bias_pct = excluded.quoted_bias_pct, extra_json = excluded.extra_json, computed_at = now()
            """,
            (
                summary.channel_id,
                asof_date,
                summary.lead_lag_minutes,
                summary.lifts.get("30m"),
                summary.lifts.get("1h"),
                summary.lifts.get("3h"),
                summary.lifts.get("eod"),
                summary.n_events,
                summary.cis.get("eod", (None, None))[0],
                summary.cis.get("eod", (None, None))[1],
                summary.q_values.get("eod"),
                summary.label,
                summary.pumper_flag,
                summary.quoted_bias_pct,
                METHOD_VERSION,
                jsonb({
                    "split": split_name,
                    "family": family_summary,
                    "measures": summary.measures,
                    "burst_share": summary.burst_share,
                    "label_reason": summary.label_reason,
                    "cost_model": cost.as_dict(),
                    "cis": {k: list(v) for k, v in summary.cis.items()},
                    "q_values": summary.q_values,
                }),
            ),
        )
    repo.audit(conn, "worker:stats", "channel_scores_persisted", {
        "asof_date": asof_date.isoformat(), "channels": len(summaries), "split": split_name,
    })
    conn.commit()


__all__ = ["ChannelSummary", "ScoringArtifact", "run_scoring", "METHOD_VERSION", "ALL_HORIZONS"]

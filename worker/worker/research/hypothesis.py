"""Hypothesis testing (R1d, §7.4).

The strong LLM may PROPOSE hypotheses; only CODE can test them. Every hypothesis is matched
against a registry of coded tests. A hypothesis that matches no codified test stays
'proposed' and the report says so explicitly — that is how unfalsifiable suggestions are
kept out of the results.

Verdict rule (fixed in advance, identical for every test):
    confirmed     the 95% bootstrap CI of the effect EXCLUDES 0 in the stated direction
    rejected      the CI excludes 0 in the OPPOSITE direction, or the point estimate is
                  inside the dead zone with a CI narrower than the effect claimed
    inconclusive  the CI straddles 0 (not enough evidence either way)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Sequence

import numpy as np
import psycopg

from ..config import Settings
from ..db import repo
from ..jsonutil import jsonb
from ..timez import day_bounds_utc
from .stats.bootstrap import block_bootstrap_ci, sign_agreement, t_p_value
from .stats.series import EventReturn, pick_series, HORIZON_MINUTES, load_day_minutes, signed
from .stats.series import MeasurementSeries
from . import prices_view as pv

log = logging.getLogger(__name__)


@dataclass
class HypothesisTest:
    name: str
    keywords: tuple[str, ...]
    fn: Callable[[psycopg.Connection, Settings, Sequence[date], str], dict[str, Any]]
    description: str


def _verdict(ci_low: float, ci_high: float, point: float) -> str:
    if ci_low > 0 and point > 0:
        return "confirmed"
    if ci_high < 0 and point < 0:
        return "rejected"
    if ci_low > 0 or ci_high < 0:
        return "rejected" if (point < 0 and ci_high < 0) else "confirmed"
    return "inconclusive"


def _channel_mentions(text: str, conn: psycopg.Connection) -> list[str]:
    lowered = text.lower()
    usernames = [r["username"] for r in conn.execute("select username from channels").fetchall()]
    return [u for u in usernames if u.lower() in lowered]


def _events_for(conn: psycopg.Connection, settings: Settings, days: Sequence[date], username: str | None = None,
                hour_bucket: tuple[int, int] | None = None, horizon: str = "30m") -> list[EventReturn]:
    """Recompute event returns for one channel (and optional intraday hour window)."""
    series_list = pick_series(conn, start=days[0], end=days[-1])
    if not series_list:
        return []
    series: MeasurementSeries = series_list[0]
    out: list[EventReturn] = []
    for day in days:
        minutes = load_day_minutes(conn, series, day, open_local=settings.open_time, flat_local=settings.flat_time)
        if minutes is None:
            continue
        open_ts, flat_ts = day_bounds_utc(day, settings.open_time, settings.flat_time)
        rows = conn.execute(
            """
            select m.published_at, c.username, e.extraction_json
            from messages m join channels c on c.id = m.channel_id join extractions e on e.message_id = m.id
            where e.status = 'ok' and c.active and m.published_at >= %s and m.published_at <= %s
            """,
            (open_ts, flat_ts),
        ).fetchall()
        for row in rows:
            if username and row["username"] != username:
                continue
            ej = row["extraction_json"] or {}
            direction = ej.get("direction")
            if direction not in ("up", "down"):
                continue
            local_hour = row["published_at"].astimezone(open_ts.tzinfo).hour
            if hour_bucket and not (hour_bucket[0] <= local_hour < hour_bucket[1]):
                continue
            from .costs import cost_model

            cm = cost_model(settings, "coin" if ej.get("quoted_asset") in
                            {"emami_coin", "whole_coin", "half_coin", "quarter_coin"} else "gold")
            gross = _forward(minutes, row["published_at"], horizon, flat_ts)
            if gross is None:
                continue
            out.append(EventReturn(day=day, channel_id=0, username=row["username"], direction=direction,
                                   kind=ej.get("kind") or "opinion", horizon=horizon,
                                   gross_bps=signed(gross, direction),
                                   net_bps=signed(gross, direction) - cm.signal_cost_bps,
                                   cost_bps=cm.signal_cost_bps, measure=series.measure, asset="gold",
                                   message_id=0, published_at=row["published_at"]))
    return out


def _forward(minutes, published, horizon: str, flat_ts):
    from .stats.series import forward_return_bps

    if horizon == "eod":
        delta = max(1, int((flat_ts - published).total_seconds() / 60))
        return forward_return_bps(minutes, published, delta, flat_ts=flat_ts)
    return forward_return_bps(minutes, published, HORIZON_MINUTES[horizon], flat_ts=flat_ts)


def test_channel_lead(conn, settings, days, text) -> dict[str, Any]:
    usernames = _channel_mentions(text, conn) or [r["username"] for r in conn.execute(
        "select username from channels where active limit 1").fetchall()]
    results = []
    for username in usernames[:3]:
        events = _events_for(conn, settings, days, username=username, horizon="30m")
        if not events:
            results.append({"channel": username, "n_events": 0, "verdict": "inconclusive", "reason": "no events in window"})
            continue
        samples = [(e.day, e.gross_bps) for e in events]
        mean, lo, hi = block_bootstrap_ci(samples)
        results.append({
            "channel": username,
            "n_events": len(events),
            "gross_30m_bps": round(mean, 3),
            "ci95": [round(lo, 3), round(hi, 3)],
            "p_value": round(t_p_value(samples), 5),
            "sign_consistency": round(sign_agreement(samples), 3),
            "verdict": _verdict(lo, hi, mean),
        })
    return {"method": "channel_lead_30m_bootstrap", "results": results,
            "threshold": "95% block-bootstrap CI of signed gross 30m lift excludes zero"}


def test_cost_breakeven(conn, settings, days, text) -> dict[str, Any]:
    events = _events_for(conn, settings, days, horizon="30m")
    if not events:
        return {"method": "cost_breakeven", "results": [], "verdict": "inconclusive"}
    per_direction: dict[str, list[tuple[date, float]]] = {}
    for event in events:
        per_direction.setdefault(event.direction, []).append((event.day, event.gross_bps))
    out = []
    for direction, samples in per_direction.items():
        mean, lo, hi = block_bootstrap_ci(samples)
        out.append({
            "direction": direction,
            "n_events": len(samples),
            "gross_bps": round(mean, 3),
            "ci95": [round(lo, 3), round(hi, 3)],
            "cost_gold_one_sided_bps": settings.spread_bps_gold + settings.slippage_bps_gold,
            "net_bps": round(mean - (settings.spread_bps_gold + settings.slippage_bps_gold), 3),
            "breaks_even": bool(lo > (settings.spread_bps_gold + settings.slippage_bps_gold)),
            "verdict": "confirmed" if lo > (settings.spread_bps_gold + settings.slippage_bps_gold) else "rejected"
            if hi < (settings.spread_bps_gold + settings.slippage_bps_gold) else "inconclusive",
        })
    return {"method": "cost_breakeven_gross_vs_one_sided_cost",
            "results": out,
            "threshold": "CI lower bound of gross lift above one-sided gold cost (spread+slippage)"}


def test_agreement_scaling(conn, settings, days, text) -> dict[str, Any]:
    from .stats import event_study

    series_list = pick_series(conn, start=days[0], end=days[-1])
    if not series_list:
        return {"method": "agreement_scaling", "results": [], "verdict": "inconclusive"}
    series = series_list[0]
    per_k: dict[int, list[tuple[date, float]]] = {}
    for day in days:
        minutes = load_day_minutes(conn, series, day, open_local=settings.open_time, flat_local=settings.flat_time)
        if minutes is None:
            continue
        open_ts, flat_ts = day_bounds_utc(day, settings.open_time, settings.flat_time)
        rows = [dict(r) for r in conn.execute(
            """
            select m.published_at, c.username, e.extraction_json
            from messages m join channels c on c.id = m.channel_id join extractions e on e.message_id = m.id
            where e.status = 'ok' and c.active and m.published_at >= %s and m.published_at <= %s
            """,
            (open_ts, flat_ts),
        ).fetchall()]
        for event in event_study.agreement_events(settings, series, minutes, rows):
            net = event.net_bps.get("30m")
            if net is None:
                continue
            for k in range(2, 6):
                if event.k_channels >= k:
                    per_k.setdefault(k, []).append((day, net))
    out = []
    for k, samples in sorted(per_k.items()):
        mean, lo, hi = block_bootstrap_ci(samples)
        out.append({"k": k, "n_events": len(samples), "net_30m_bps": round(mean, 3),
                    "ci95": [round(lo, 3), round(hi, 3)], "verdict": _verdict(lo, hi, mean)})
    return {"method": "agreement_k_scaling_net_30m", "results": out,
            "threshold": "95% CI of NET 30m lift per agreement level k"}


def test_pumper_flag(conn, settings, days, text) -> dict[str, Any]:
    rows = conn.execute(
        """
        select c.username, cs.pumper_flag, cs.quoted_bias_pct, cs.lift_bps_30m, cs.n_events
        from channel_scores cs join channels c on c.id = cs.channel_id
        where cs.horizon = 'eod'
        order by cs.asof_date desc limit 50
        """
    ).fetchall()
    flagged = [dict(r) for r in rows if r["pumper_flag"]]
    clean = [dict(r) for r in rows if not r["pumper_flag"]]
    return {
        "method": "pumper_vs_clean_channel_lift",
        "results": {
            "flagged": [{"channel": r["username"], "bias_pct": r["quoted_bias_pct"], "lift_30m": r["lift_bps_30m"]}
                        for r in flagged],
            "clean_n": len(clean),
            "mean_lift_30m_flagged": round(float(np.mean([r["lift_bps_30m"] for r in flagged])), 3) if flagged else None,
            "mean_lift_30m_clean": round(float(np.mean([r["lift_bps_30m"] for r in clean if r["lift_bps_30m"] is not None])), 3)
            if any(r["lift_bps_30m"] is not None for r in clean) else None,
        },
        "threshold": "flagged channels' mean 30m lift below clean channels' mean",
        "verdict": "inconclusive",
    }


def test_hour_of_day(conn, settings, days, text) -> dict[str, Any]:
    buckets = {"open_0800_1100": (8, 11), "midday_1100_1600": (11, 16), "late_1600_2200": (16, 22)}
    out = []
    for name, bucket in buckets.items():
        events = _events_for(conn, settings, days, hour_bucket=bucket, horizon="30m")
        if not events:
            out.append({"bucket": name, "n_events": 0})
            continue
        samples = [(e.day, e.net_bps) for e in events]
        mean, lo, hi = block_bootstrap_ci(samples)
        out.append({"bucket": name, "n_events": len(events), "net_30m_bps": round(mean, 3),
                    "ci95": [round(lo, 3), round(hi, 3)], "verdict": _verdict(lo, hi, mean)})
    return {"method": "hour_bucket_net_30m_lift", "results": out,
            "threshold": "95% CI of NET 30m lift per Tehran session bucket"}


def test_ounce_vs_dollar(conn, settings, days, text) -> dict[str, Any]:
    """Do ounce and free-market USD morning moves explain the day's gold move?"""
    samples_usdt: list[tuple[date, float]] = []
    samples_ounce: list[tuple[date, float]] = []
    for day in days:
        open_ts, flat_ts = day_bounds_utc(day, settings.open_time, settings.flat_time)
        gold_dec = pv.decision_price(_light_ctx(conn, settings, open_ts), day, pv.DOMESTIC_GOLD, at=open_ts)
        gold_eod = pv.eod_price(_light_ctx(conn, settings, flat_ts), day, pv.DOMESTIC_GOLD, at=flat_ts)
        if not gold_dec.price or not gold_eod.price:
            continue
        day_move = float(gold_eod.price) / float(gold_dec.price) - 1.0
        usdt_morning = _morning_move(conn, pv.USDTIRT, open_ts)
        ounce_morning = _morning_move(conn, pv.XAUUSD, open_ts)
        if usdt_morning is not None:
            samples_usdt.append((day, np.sign(usdt_morning) * day_move * 1e4))
        if ounce_morning is not None:
            samples_ounce.append((day, np.sign(ounce_morning) * day_move * 1e4))
    out = {}
    for name, samples in (("usdtirt_morning_direction", samples_usdt), ("ounce_morning_direction", samples_ounce)):
        if not samples:
            continue
        mean, lo, hi = block_bootstrap_ci(samples)
        out[name] = {"n_days": len(samples), "mean_signed_day_bps": round(mean, 3),
                     "ci95": [round(lo, 3), round(hi, 3)], "verdict": _verdict(lo, hi, mean)}
    return {"method": "morning_direction_vs_day_outcome", "results": out,
            "threshold": "95% CI of the signed day move following the morning direction"}


def _morning_move(conn, candidates, open_ts) -> float | None:
    from datetime import timedelta

    for source, symbol, resolution in candidates:
        rows = conn.execute(
            """
            select ts, close from price_bars
            where source = %s and symbol = %s and resolution = %s and ts >= %s and ts <= %s
            order by ts asc
            """,
            (source, symbol, resolution, open_ts, open_ts + timedelta(hours=4)),
        ).fetchall()
        if len(rows) >= 2:
            return float(rows[-1]["close"]) / float(rows[0]["close"]) - 1.0
    return None


def _light_ctx(conn, settings, as_of):
    """A ReadContext for stats-time reads. `live` bounds only by as_of: these reads are part
    of a split-scoped run whose days already came from the frozen split."""
    from ..window import frozen_split
    from .asof import ReadContext

    return ReadContext(conn=conn, as_of=as_of, split_name="live", split=frozen_split(conn, settings), settings=settings)


REGISTRY: list[HypothesisTest] = [
    HypothesisTest("channel_leads", ("lead", "lag", "predict", "precede", "follow", "early", "late"),
                   test_channel_lead, "channel 30m lift + CI"),
    HypothesisTest("cost_breakeven", ("cost", "spread", "slippage", "net of", "fees", "breakeven"),
                   test_cost_breakeven, "gross lift vs one-sided cost"),
    HypothesisTest("agreement_scaling", ("agree", "agreement", "multiple channels", "consensus", "k channel"),
                   test_agreement_scaling, "net lift by agreement level k"),
    HypothesisTest("pumper_flag", ("pump", "hojoom", "bubble", "bias", "inflated", "premium"),
                   test_pumper_flag, "flagged vs clean channel lift"),
    HypothesisTest("hour_of_day", ("hour", "morning", "afternoon", "evening", "session", "time of day"),
                   test_hour_of_day, "net lift by session bucket"),
    HypothesisTest("ounce_vs_dollar", ("ounce", "xauusd", "dollar", "usdt", "usd/irt", "usdirt"),
                   test_ounce_vs_dollar, "morning direction vs day outcome"),
]


def match_test(text: str) -> HypothesisTest | None:
    lowered = text.lower()
    best: tuple[int, HypothesisTest] | None = None
    for test in REGISTRY:
        hits = sum(1 for kw in test.keywords if kw in lowered)
        if hits and (best is None or hits > best[0]):
            best = (hits, test)
    return best[1] if best else None


def test_hypothesis(
    conn: psycopg.Connection, settings: Settings, hypothesis_id: int, days: Sequence[date]
) -> dict[str, Any]:
    row = conn.execute("select id, text, status from hypotheses where id = %s", (hypothesis_id,)).fetchone()
    if row is None:
        raise ValueError(f"hypothesis {hypothesis_id} not found")
    test = match_test(row["text"])
    if test is None:
        conn.execute(
            "update hypotheses set status = 'proposed', test_method = %s, tested_at = now(), test_results_json = %s "
            "where id = %s",
            ("unmapped", jsonb({"note": "no codified test matches this hypothesis text; it stays 'proposed' "
                                        "until someone writes a test for it"}), hypothesis_id),
        )
        conn.commit()
        return {"hypothesis_id": hypothesis_id, "status": "proposed", "reason": "unmapped"}
    results = test.fn(conn, settings, days, row["text"])
    verdict = _overall_verdict(results)
    status = {"confirmed": "confirmed", "rejected": "rejected"}.get(verdict, "tested")
    conn.execute(
        """
        update hypotheses set status = %s, test_method = %s, test_results_json = %s, tested_at = now()
        where id = %s
        """,
        (status, test.name, jsonb(results), hypothesis_id),
    )
    repo.audit(conn, "worker:hypothesis", "tested",
               {"hypothesis_id": hypothesis_id, "test": test.name, "verdict": verdict})
    conn.commit()
    return {"hypothesis_id": hypothesis_id, "test": test.name, "status": status, "verdict": verdict, "results": results}


def _overall_verdict(results: dict[str, Any]) -> str:
    rows = results.get("results")
    verdicts: list[str] = []
    if isinstance(rows, list):
        verdicts = [r.get("verdict", "inconclusive") for r in rows if isinstance(r, dict)]
    elif isinstance(rows, dict):
        verdicts = [v.get("verdict", "inconclusive") for v in rows.values() if isinstance(v, dict)]
    if not verdicts:
        return "inconclusive"
    if all(v == "confirmed" for v in verdicts):
        return "confirmed"
    if all(v == "rejected" for v in verdicts):
        return "rejected"
    return "inconclusive"


def pending_hypotheses(conn: psycopg.Connection, limit: int = 50) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "select id, text, status, proposed_by_model from hypotheses where status = 'proposed' order by id limit %s",
            (limit,),
        ).fetchall()
    ]


def run_pending(conn: psycopg.Connection, settings: Settings, days: Sequence[date], *, limit: int = 20) -> list[dict[str, Any]]:
    out = []
    for row in pending_hypotheses(conn, limit=limit):
        try:
            out.append(test_hypothesis(conn, settings, row["id"], days))
        except Exception as exc:  # noqa: BLE001 - one bad hypothesis must not stop the batch
            log.warning("hypothesis %s could not be tested: %s", row["id"], exc)
            out.append({"hypothesis_id": row["id"], "status": "proposed", "error": str(exc)[:200]})
    return out


__all__ = ["REGISTRY", "match_test", "test_hypothesis", "pending_hypotheses", "run_pending", "HypothesisTest"]

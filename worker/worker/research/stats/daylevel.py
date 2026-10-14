"""Day-level records for calibration + baselines.

Every feature is computed from data available BEFORE the day's decision: the previous days'
labels, the 08:00→12:00 USDTIRT/ounce move of the same day (visible at the 12:00 checkpoint;
for the 08:00 checkpoint it is simply absent), and trailing means of the prior week.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

import psycopg

from ...config import Settings
from ...timez import day_bounds_utc, previous_trading_day
from .. import prices_view as pv
from .calibration import CalibrationRow
from .baselines import DayRecord, FEATURE_NAMES

log = logging.getLogger(__name__)

USDTIRT = pv.USDTIRT
XAUUSD = pv.XAUUSD
GOLD = pv.DOMESTIC_GOLD


def _price_at(conn: psycopg.Connection, candidates, when: datetime) -> float | None:
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


def _first_at_or_after(conn: psycopg.Connection, candidates, when: datetime, until: datetime) -> float | None:
    for source, symbol, resolution in candidates:
        row = conn.execute(
            """
            select close from price_bars
            where source = %s and symbol = %s and resolution = %s and ts >= %s and ts <= %s
            order by ts asc limit 1
            """,
            (source, symbol, resolution, when, until),
        ).fetchone()
        if row:
            return float(row["close"])
    return None


def build_day_records(
    conn: psycopg.Connection, settings: Settings, days: list[date], *, checkpoint: time = time(12, 0)
) -> list[DayRecord]:
    outcomes = {
        row["day_date"]: dict(row)
        for row in conn.execute(
            "select day_date, decision_price, eod_price, realized_bps, label from day_outcomes where day_date = any(%s)",
            (days,),
        ).fetchall()
    }
    jev = _jev_latest(conn, days)
    records: list[DayRecord] = []
    trailing_gold: list[float] = []
    trailing_usdt: list[float] = []
    prev_day: date | None = None
    for day in days:
        outcome = outcomes.get(day)
        if not outcome:
            continue
        open_ts, _ = day_bounds_utc(day, settings.open_time, settings.flat_time)
        midday = datetime.combine(day, checkpoint, tzinfo=open_ts.tzinfo).astimezone(open_ts.tzinfo)
        usdt_open = _first_at_or_after(conn, USDTIRT, open_ts, open_ts + timedelta(hours=3))
        usdt_mid = _price_at(conn, USDTIRT, midday)
        ounce_open = _price_at(conn, XAUUSD, open_ts)
        ounce_mid = _price_at(conn, XAUUSD, midday)
        features = {
            "usdtirt_morning_pct": _pct(usdt_mid, usdt_open),
            "ounce_morning_pct": _pct(ounce_mid, ounce_open),
            "yesterday_label_up": 1.0 if prev_day and outcomes.get(prev_day, {}).get("label") == "up" else 0.0,
            "yesterday_label_down": 1.0 if prev_day and outcomes.get(prev_day, {}).get("label") == "down" else 0.0,
            "gold_7d_mean_bps": float(sum(trailing_gold[-7:]) / len(trailing_gold[-7:])) if trailing_gold else 0.0,
            "usdtirt_7d_mean_bps": float(sum(trailing_usdt[-7:]) / len(trailing_usdt[-7:])) if trailing_usdt else 0.0,
        }
        features = {k: (0.0 if v is None else float(v)) for k, v in features.items()}
        assert set(features) == set(FEATURE_NAMES), "feature set drifted from FEATURE_NAMES"
        decision = jev.get(day, {})
        records.append(
            DayRecord(
                day=day,
                realized_label=str(outcome["label"]),
                realized_bps=float(outcome["realized_bps"]),
                features=features,
                jev_predicted=decision.get("predicted"),
                jev_probs=decision.get("probs", {}) or {},
                jev_confidence=decision.get("confidence"),
            )
        )
        trailing_gold.append(float(outcome["realized_bps"]))
        prev_day = day
    return records


def _jev_latest(conn: psycopg.Connection, days: list[date]) -> dict[date, dict]:
    rows = conn.execute(
        """
        select day_date, answers_json, checkpoint
        from jev_decisions
        where day_date = any(%s) and split in ('discovery', 'outofsample', 'demo')
        order by day_date, checkpoint
        """,
        (days,),
    ).fetchall()
    out: dict[date, dict] = {}
    for row in rows:
        answers = row["answers_json"] or {}
        direction = answers.get("day_direction") or {}
        dist = direction.get("distribution") or {}
        if not dist:
            continue
        predicted = max(dist.items(), key=lambda kv: kv[1])[0]
        out[row["day_date"]] = {
            "predicted": predicted,
            "probs": {k: float(v) for k, v in dist.items()},
            "confidence": float(direction["confidence"]) if direction.get("confidence") is not None else max(dist.values()),
            "checkpoint": row["checkpoint"],
        }
    return out


def calibration_rows(conn: psycopg.Connection, days: list[date]) -> list[CalibrationRow]:
    jev = _jev_latest(conn, days)
    outcomes = {
        row["day_date"]: row["label"]
        for row in conn.execute(
            "select day_date, label from day_outcomes where day_date = any(%s)", (days,)
        ).fetchall()
    }
    rows: list[CalibrationRow] = []
    for day, decision in sorted(jev.items()):
        if day not in outcomes:
            continue
        rows.append(
            CalibrationRow(
                day=day.isoformat(),
                predicted=decision["predicted"],
                probs=decision["probs"],
                actual=str(outcomes[day]),
                confidence=decision.get("confidence"),
            )
        )
    return rows


def _pct(now: float | None, then: float | None) -> float | None:
    if now is None or not then:
        return None
    return (now / then - 1.0) * 100.0


__all__ = ["build_day_records", "calibration_rows"]

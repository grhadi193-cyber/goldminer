"""Replay / backtest core (7.1) — the SAME code path the live paper monitor uses.

For each trading day D and each configured checkpoint T:
    1. decision price      = first price at/after 08:00 local (per series)
    2. state               = built from rows with event time <= T  (anti-leakage, T1–T4)
    3. Jev                 = one request for the whole question tree (cached by request hash)
    4. deterministic gates = spread / confidence / caps / session (R6)
    5. paper trade         = entry at ask, exit at bid, stop loss, forced flat 22:00 (R5)
    6. records             = jev_decisions, backtest_trades, day_outcomes

Nothing in this module mints numbers for a report by hand: everything lands in Postgres.
Determinism: identical inputs -> identical `state_hash` (T4) because the state is built only
from bounded, ordered rows and every number is rounded once, in the state builder.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

import psycopg

from ..config import Settings
from ..db import repo
from ..jsonutil import canonical_hash, jsonb
from ..timez import day_bounds_utc, local_ts, previous_trading_day
from . import prices_view as pv
from .asof import ReadContext
from .costs import cost_model
from .gates import GateVerdict, evaluate_gates
from .jev import JevClient
from .question_tree import build_questions
from .state import build_state

log = logging.getLogger(__name__)

CHECKPOINT_KEYS = {time(8, 0): "0800", time(12, 0): "1200", time(16, 0): "1600", time(22, 0): "2200"}


def checkpoint_key(t: time) -> str:
    return CHECKPOINT_KEYS.get(t, t.strftime("%H%M"))


@dataclass
class TradeResult:
    direction: str
    entry_ts: datetime
    exit_ts: datetime
    entry_price: float
    exit_price: float
    size_g: float
    gross_bps: float
    cost_bps: float
    net_bps: float
    exit_reason: str
    pnl_toman: float
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "entry_ts": self.entry_ts.isoformat(),
            "exit_ts": self.exit_ts.isoformat(),
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "size_g": self.size_g,
            "gross_bps": round(self.gross_bps, 4),
            "cost_bps": round(self.cost_bps, 4),
            "net_bps": round(self.net_bps, 4),
            "exit_reason": self.exit_reason,
            "pnl_toman": round(self.pnl_toman, 2),
            "detail": self.detail,
        }


@dataclass
class DayReplay:
    day: date
    checkpoint: time
    decision_ts: datetime
    state: dict[str, Any]
    state_hash: str
    decision_id: int | None
    jev: dict[str, Any]
    verdict: GateVerdict
    trade: TradeResult | None
    day_prices: dict[str, Any]
    outcome: dict[str, Any] | None = None


def _mark_info(point) -> dict:
    return {"source": point.source, "resolution": point.resolution, "ts": str(point.ts), "method": point.method}


def simulate_paper_trade(
    *,
    settings: Settings,
    entry_ts: datetime,
    entry_mid: float,
    exit_ts: datetime,
    exit_mid: float,
    session_low: float | None,
    session_high: float | None,
    asset: str = "gold",
    size_g: float | None = None,
    day_pnl_bps: float = 0.0,
) -> TradeResult:
    """Long-only paper round trip with explicit costs and deterministic exits."""
    cm = cost_model(settings, asset)
    size = float(size_g if size_g is not None else settings.position_size_g)
    entry_price = cm.entry_price(entry_mid)      # we buy at the ask
    stop_threshold = entry_price * (1.0 - settings.stop_loss_bps / 1e4)
    # Remaining risk budget for this position: the daily cap minus what the day already lost.
    # `day_pnl_bps` is negative when the day is down: the remaining budget is the cap minus
    # the loss magnitude already realised today.
    cap_room_bps = max(0.0, abs(settings.daily_loss_cap_bps) - max(0.0, -day_pnl_bps))
    cap_threshold = entry_price * (1.0 - (cap_room_bps + cm.round_trip_cost_bps) / 1e4)

    # On the way down the HIGHER threshold is touched first, so it is the one that binds.
    if stop_threshold >= cap_threshold:
        binding, binding_reason = stop_threshold, "stop"
    else:
        binding, binding_reason = cap_threshold, "cap"

    exit_level: float                            # frictionless MID level the position exits at
    exit_reason: str
    realised_exit_ts = exit_ts
    if session_low is not None and session_low <= binding:
        exit_level = binding
        exit_reason = binding_reason
    else:
        exit_level = exit_mid                    # forced flat at 22:00 (or the checkpoint exit)
        exit_reason = "flat_2200"
    exit_price = cm.exit_price(exit_level)

    # CONVENTION (T8, do not change without updating tests/fixtures/costs_golden.json):
    #   gross_bps = frictionless MID-to-MID move over the holding window
    #   cost_bps  = round trip = spread + 2*slippage
    #   net_bps   = gross_bps - cost_bps
    # `entry_price`/`exit_price` are ask/bid FILL prices and therefore already contain the
    # spread; computing gross from them and then subtracting cost_bps would double count it.
    gross_bps = (exit_level / entry_mid - 1.0) * 1e4
    cost_bps = cm.round_trip_cost_bps
    net_bps = gross_bps - cost_bps
    pnl_toman = size * entry_price * (net_bps / 1e4)
    return TradeResult(
        direction="long",
        entry_ts=entry_ts,
        exit_ts=realised_exit_ts,
        entry_price=entry_price,
        exit_price=exit_price,
        size_g=size,
        gross_bps=gross_bps,
        cost_bps=cost_bps,
        net_bps=net_bps,
        exit_reason=exit_reason,
        pnl_toman=pnl_toman,
        detail={
            "entry_mid": entry_mid,
            "exit_mid": exit_mid,
            "exit_level": exit_level,
            "gross_basis": "mid_to_mid",
            "cost_basis": "spread_plus_2x_slippage",
            "stop_price": stop_threshold,
            "cap_price": cap_threshold,
            "binding_threshold": binding,
            "cap_room_bps": cap_room_bps,
            "session_low": session_low,
            "session_high": session_high,
            "day_pnl_bps_before": day_pnl_bps,
            "spread_bps": cm.spread_bps,
            "slippage_bps": cm.slippage_bps,
        },
    )


class ReplayEngine:
    """Replays one split day by day, writing every artefact to Postgres."""

    def __init__(
        self,
        conn: psycopg.Connection,
        settings: Settings,
        *,
        split_name: str,
        jev: JevClient,
        persist: bool = True,
        exit_at: time | None = None,
    ) -> None:
        self.conn = conn
        self.settings = settings
        self.split_name = split_name
        self.jev = jev
        self.persist = persist
        self.exit_at = exit_at  # for --what-if style runs; default = forced flat 22:00
        self.questions = build_questions()
        self.run_id: int | None = None
        self.trades: list[TradeResult] = []

    # ---------------------------------------------------------------- run bookkeeping

    def open_run(self, config: dict[str, Any], *, split: str | None = None) -> int:
        row = self.conn.execute(
            "insert into backtest_runs (config_json, split, status) values (%s, %s, 'running') returning run_id",
            (jsonb(config), split or self.split_name),
        ).fetchone()
        self.conn.commit()
        self.run_id = int(row["run_id"])
        return self.run_id

    def close_run(self, status: str = "ok", notes: str | None = None) -> None:
        if self.run_id is None:
            return
        self.conn.execute(
            "update backtest_runs set status = %s, finished_at = now(), notes = %s where run_id = %s",
            (status, notes, self.run_id),
        )
        self.conn.commit()

    # ---------------------------------------------------------------- one checkpoint

    async def replay_checkpoint(
        self, day: date, checkpoint: time, *, day_pnl_bps: float = 0.0, persist_decision: bool = True
    ) -> DayReplay:
        open_ts, flat_ts = day_bounds_utc(day, self.settings.open_time, self.settings.flat_time)
        decision_ts = local_ts(day, checkpoint)
        ctx = ReadContext.build(self.conn, self.settings, as_of=decision_ts, split_name=self.split_name)
        ctx.assert_day_allowed(day)

        state = build_state(ctx, day, checkpoint_local=checkpoint, settings=self.settings)
        state_hash = canonical_hash(state)
        questions = self.questions

        response = await self.jev.ask(state, questions)
        answers = response.as_dict()["answers"]
        day_dir = response.answer("day_direction")
        action = response.answer("final_action")
        strength = response.answer("signal_strength")
        pumping = response.answer("coordinated_pumping")
        contradiction = response.answer("credible_contrading_news")

        confidence = None
        if day_dir and day_dir.confidence is not None:
            confidence = float(day_dir.confidence)
        if confidence is None and action and action.confidence is not None:
            confidence = float(action.confidence)

        spread_bps = float(state["market"]["gold_spread_bps"])
        verdict = evaluate_gates(
            settings=self.settings,
            now_local=local_ts(day, checkpoint).astimezone(__import__("zoneinfo").ZoneInfo(self.settings.app_timezone)).time(),
            spread_bps=spread_bps,
            jev_action=str(action.value).lower() if action and action.value is not None else None,
            jev_confidence=confidence,
            day_direction=str(day_dir.top_option()).lower() if day_dir else None,
            pumping_probability=_prob(pumping),
            contradiction_probability=_prob(contradiction),
            day_pnl_bps=day_pnl_bps,
        )
        _ = strength

        decision_id = None
        if persist_decision and self.persist:
            decision_id = self._store_decision(
                day=day, checkpoint=checkpoint, decision_ts=decision_ts, state=state, state_hash=state_hash,
                answers=answers, model=response.model, cache_key=response.cache_key,
                verdict=verdict.as_dict(), confidence=confidence, day_pnl_bps=day_pnl_bps,
            )

        trade: TradeResult | None = None
        if verdict.entered:
            entry = pv.decision_price(ctx, day, pv.DOMESTIC_GOLD, at=decision_ts)
            exit_at = self.exit_at or self.settings.flat_time
            exit_ts = local_ts(day, exit_at)
            # The exit price may only be read from data visible at THAT time — so use a fresh
            # context bounded by the exit instant (this is still historical, still leak-free).
            exit_ctx = ReadContext.build(self.conn, self.settings, as_of=max(exit_ts, decision_ts),
                                         split_name=self.split_name) if self.split_name != "live" else ctx
            exit_point = pv.eod_price(exit_ctx, day, pv.DOMESTIC_GOLD, at=exit_ts)
            low, high, _ = pv.session_low_high(exit_ctx, pv.DOMESTIC_GOLD, start=decision_ts, end=exit_ts)
            if entry.price and exit_point.price:
                trade = simulate_paper_trade(
                    settings=self.settings,
                    entry_ts=decision_ts,
                    entry_mid=float(entry.price),
                    exit_ts=exit_ts,
                    exit_mid=float(exit_point.price),
                    session_low=low,
                    session_high=high,
                    day_pnl_bps=day_pnl_bps,
                )
                # Provenance of the marks: which series marked entry and exit. When both legs
                # read the SAME bar (possible when a daily series is the only gold source) the
                # mid move is exactly 0 by construction and the row is tagged so the report can
                # say so instead of pretending the trade was measured.
                trade.detail["entry_mark"] = _mark_info(entry)
                trade.detail["exit_mark"] = _mark_info(exit_point)
                trade.detail["same_bar_mark"] = bool(entry.ts == exit_point.ts)
                self.trades.append(trade)
                if self.persist:
                    self._store_trade(day=day, decision_id=decision_id, trade=trade)

        prices = {
            "gold_decision": pv.decision_price(ctx, day, pv.DOMESTIC_GOLD, at=open_ts).as_dict(),
            "gold_open": pv.decision_price(ctx, day, pv.DOMESTIC_GOLD, at=open_ts).as_dict(),
        }
        return DayReplay(
            day=day,
            checkpoint=checkpoint,
            decision_ts=decision_ts,
            state=state,
            state_hash=state_hash,
            decision_id=decision_id,
            jev={"model": response.model, "answers": answers, "from_cache": response.from_cache},
            verdict=verdict,
            trade=trade,
            day_prices=prices,
        )

    # ---------------------------------------------------------------- one day

    async def replay_day(self, day: date, *, persist_outcome: bool = True) -> list[DayReplay]:
        results: list[DayReplay] = []
        day_pnl_bps = 0.0
        for checkpoint in self.settings.checkpoint_times:
            result = await self.replay_checkpoint(day, checkpoint, day_pnl_bps=day_pnl_bps)
            results.append(result)
            if result.trade:
                day_pnl_bps += result.trade.net_bps
        outcome = self.record_day_outcome(day) if persist_outcome and self.persist else None
        for r in results:
            r.outcome = outcome
        return results

    # ---------------------------------------------------------------- persistence

    def _store_decision(
        self,
        *,
        day: date,
        checkpoint: time,
        decision_ts: datetime,
        state: dict[str, Any],
        state_hash: str,
        answers: dict[str, Any],
        model: str,
        cache_key: str,
        verdict: dict[str, Any] | None = None,
        confidence: float | None = None,
        day_pnl_bps: float | None = None,
    ) -> int:
        row = self.conn.execute(
            """
            insert into jev_decisions
                (decision_ts, day_date, checkpoint, state_json, questions_json, answers_json,
                 model, cache_key, split, state_hash, verdict_json, confidence, day_pnl_bps, computed_at)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            on conflict (day_date, checkpoint, split, state_hash) do update
                set answers_json = excluded.answers_json,
                    model = excluded.model,
                    cache_key = excluded.cache_key,
                    verdict_json = excluded.verdict_json,
                    confidence = excluded.confidence,
                    day_pnl_bps = excluded.day_pnl_bps,
                    computed_at = now()
            returning decision_id
            """,
            (
                decision_ts, day, checkpoint_key(checkpoint), jsonb(state), jsonb(self.questions),
                jsonb(answers), model, cache_key, self.split_name, state_hash,
                jsonb(verdict) if verdict is not None else None, confidence, day_pnl_bps,
            ),
        ).fetchone()
        self.conn.commit()
        return int(row["decision_id"])

    def _store_trade(self, *, day: date, decision_id: int | None, trade: TradeResult) -> None:
        if self.run_id is None:
            return
        self.conn.execute(
            """
            insert into backtest_trades
                (run_id, decision_id, day_date, direction, entry_ts, exit_ts, entry_price, exit_price,
                 size_g, gross_bps, cost_bps, net_bps, exit_reason, pnl_toman, detail_json)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                self.run_id, decision_id, day, trade.direction, trade.entry_ts, trade.exit_ts,
                trade.entry_price, trade.exit_price, trade.size_g, trade.gross_bps, trade.cost_bps,
                trade.net_bps, trade.exit_reason, trade.pnl_toman, jsonb(trade.detail),
            ),
        )
        self.conn.commit()

    def record_day_outcome(self, day: date) -> dict[str, Any] | None:
        """Day label from decision price -> EOD price, using ONLY data visible by 22:00."""
        open_ts, flat_ts = day_bounds_utc(day, self.settings.open_time, self.settings.flat_time)
        ctx = ReadContext.build(self.conn, self.settings, as_of=flat_ts, split_name=self.split_name)
        dp = pv.decision_price(ctx, day, pv.DOMESTIC_GOLD, at=open_ts)
        ep = pv.eod_price(ctx, day, pv.DOMESTIC_GOLD, at=flat_ts)
        if not dp.price or not ep.price:
            log.warning("day %s: cannot compute outcome (decision=%s eod=%s)", day, dp.method, ep.method)
            return None
        realized_bps = (float(ep.price) / float(dp.price) - 1.0) * 1e4
        label = "up" if realized_bps > self.settings.flat_band_bps else (
            "down" if realized_bps < -self.settings.flat_band_bps else "flat"
        )
        repo.upsert_day_outcome(
            self.conn, day_date=day, decision_price=float(dp.price), eod_price=float(ep.price),
            realized_bps=realized_bps, label=label,
        )
        self.conn.commit()
        return {
            "day": day.isoformat(),
            "decision_price": float(dp.price),
            "eod_price": float(ep.price),
            "realized_bps": round(realized_bps, 3),
            "label": label,
            "decision_source": dp.as_dict(),
            "eod_source": ep.as_dict(),
        }

    # ---------------------------------------------------------------- range driver

    async def run_days(self, days: list[date], *, progress: bool = True) -> dict[str, Any]:
        summary = {"days": 0, "decisions": 0, "trades": 0, "entered": 0, "net_bps_total": 0.0}
        for day in days:
            results = await self.replay_day(day)
            summary["days"] += 1
            summary["decisions"] += len(results)
            for r in results:
                if r.trade:
                    summary["trades"] += 1
                    summary["net_bps_total"] += r.trade.net_bps
            # daily loss cap: once the day is down more than the cap, later checkpoints of the
            # same day may not open new positions (forced close still honoured via exit_reason)
            if progress and summary["days"] % 10 == 0:
                log.info("replayed %d days (%d trades)", summary["days"], summary["trades"])
        return summary


def _prob(answer: Any) -> float | None:
    """Probability of "yes" for a noul answer (tolerant to shape)."""
    if answer is None:
        return None
    if isinstance(answer.value, (int, float)) and answer.type == "noul":
        return float(answer.value)
    for key in ("probability", "prob", "yes", "confidence"):
        if key in (answer.raw or {}):
            try:
                return float(answer.raw[key])
            except (TypeError, ValueError):
                continue
    return None


def run_replay_sync(engine: ReplayEngine, days: list[date], **kwargs: Any) -> dict[str, Any]:
    return asyncio.run(engine.run_days(days, **kwargs))


__all__ = [
    "ReplayEngine",
    "DayReplay",
    "TradeResult",
    "simulate_paper_trade",
    "checkpoint_key",
    "run_replay_sync",
]

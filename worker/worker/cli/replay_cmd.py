"""`python -m worker replay day|range` — the replay engine from the command line."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date, time

from .. import window
from ..config import get_settings
from ..db import conn as dbconn
from ..db import repo
from ..research.jev import get_client
from ..research.replay import ReplayEngine


def _engine(split_name: str, persist: bool = True) -> tuple[ReplayEngine, object]:
    settings = get_settings()
    conn = dbconn.sync_conn()
    connection = conn.__enter__()
    jev = get_client(settings, connection)
    warning = jev.verify()
    if warning:
        print(f"WARNING: {warning}\n")
    engine = ReplayEngine(connection, settings, split_name=split_name, jev=jev, persist=persist)
    return engine, conn


def day(args: argparse.Namespace) -> int:
    settings = get_settings()
    day_date = date.fromisoformat(args.date)
    checkpoints = [time.fromisoformat(args.checkpoint)] if args.checkpoint else settings.checkpoint_times
    engine, holder = _engine(args.split, persist=not args.dry_run)
    try:
        engine.open_run(
            {
                "mode": "replay_day",
                "day": day_date.isoformat(),
                "checkpoints": [c.strftime("%H:%M") for c in checkpoints],
                "split": args.split,
                "costs": settings.cost_bps,
                "gates": {
                    "jev_min_confidence": settings.jev_min_confidence,
                    "max_spread_bps_tradeable": settings.max_spread_bps_tradeable,
                    "daily_loss_cap_bps": settings.daily_loss_cap_bps,
                    "stop_loss_bps": settings.stop_loss_bps,
                },
                "jev": {"model": settings.jev_model, "host": settings.jev_host, "mock": settings.gm_demo_mode},
            },
        )
        results = asyncio.run(engine.replay_day(day_date)) if not checkpoints else asyncio.run(
            _run_selected(engine, day_date, checkpoints)
        )
        for r in results:
            print(f"\n=== {r.day} {r.checkpoint.strftime('%H:%M')} Tehran ({r.decision_ts.isoformat()} UTC) ===")
            print(f"state_hash: {r.state_hash[:16]}…  jev_model: {r.jev['model']}"
                  f"{' (cache hit)' if r.jev['from_cache'] else ''}")
            for qid, ans in r.jev["answers"].items():
                dist = ans.get("distribution") or {}
                dist_txt = " ".join(f"{k}={v}" for k, v in dist.items()) if dist else ""
                print(f"  {qid:26s} {str(ans.get('value')):>12s}  conf={ans.get('confidence')}  {dist_txt}")
            print(f"  GATE: {r.verdict.action} — {r.verdict.reason}")
            for check in r.verdict.checks:
                print(f"      [{'ok ' if check.passed else 'BLK'}] {check.name:18s} {check.detail}")
            if r.trade:
                t = r.trade
                print(f"  TRADE long {t.size_g:.0f}g  entry {t.entry_price:,.0f} -> exit {t.exit_price:,.0f} "
                      f"({t.exit_reason})  gross {t.gross_bps:.1f} - cost {t.cost_bps:.1f} = net {t.net_bps:.1f} bps")
            if r.jev.get("from_cache"):
                pass
        if results and results[-1].outcome:
            print(f"\nday outcome: {json.dumps(results[-1].outcome, indent=2, ensure_ascii=False, default=str)}")
        engine.close_run("ok", notes=f"replay day {day_date}")
        print(f"\njev stats: {json.dumps(engine.jev.stats(), indent=2)}")
    finally:
        holder.__exit__(None, None, None)
    return 0


async def _run_selected(engine: ReplayEngine, day_date: date, checkpoints: list[time]) -> list:
    results = []
    pnl = 0.0
    for checkpoint in checkpoints:
        r = await engine.replay_checkpoint(day_date, checkpoint, day_pnl_bps=pnl)
        if r.trade:
            pnl += r.trade.net_bps
        results.append(r)
    outcome = engine.record_day_outcome(day_date)
    for r in results:
        r.outcome = outcome
    return results


def range_(args: argparse.Namespace) -> int:
    settings = get_settings()
    conn = dbconn.sync_conn()
    connection = conn.__enter__()
    try:
        split = window.frozen_split(connection, settings)
        days = split.days(args.split)
        if args.limit:
            days = days[: args.limit]
        jev = get_client(settings, connection)
        warning = jev.verify()
        if warning:
            print(f"WARNING: {warning}\n")
        engine = ReplayEngine(connection, settings, split_name=args.split, jev=jev, persist=True)
        engine.open_run(
            {
                "mode": "replay_range",
                "split": args.split,
                "days": [d.isoformat() for d in days],
                "costs": settings.cost_bps,
                "checkpoints": [c.strftime("%H:%M") for c in settings.checkpoint_times],
                "jev": {"model": settings.jev_model, "mock": settings.gm_demo_mode},
            }
        )
        summary = asyncio.run(engine.run_days(days))
        engine.close_run("ok", notes=f"replay range {args.split}")
        repo.audit(connection, "worker:replay", "range", {"split": args.split, **summary})
        connection.commit()
        print(json.dumps({**summary, "jev": jev.stats()}, indent=2, default=str))
    finally:
        conn.__exit__(None, None, None)
    return 0


__all__ = ["day", "range_"]

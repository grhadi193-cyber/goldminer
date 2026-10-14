"""Backtest runner (M8/M10) — discovery and out-of-sample, one code path.

Sequence for a split:
  1. iterate its trading days through `ReplayEngine` (calls Jev, applies gates, simulates trades)
  2. compute channel statistics on THAT SPLIT's days only (FDR family, bootstrap CIs)
  3. compute calibration + baselines on the same days
  4. persist a `backtest_runs` row and an artifact JSON under `reports/`
  5. build the Markdown report

The discovery/OOS boundary is frozen in `run_config` the first time this runs (R4).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import psycopg

from .. import window
from ..config import REPO_ROOT, Settings
from ..db import repo
from ..jsonutil import jsonb
from .jev import JevClient
from .replay import ReplayEngine
from .stats.baselines import evaluate_all
from .stats.calibration import build_report as build_calibration
from .stats.daylevel import build_day_records, calibration_rows
from .stats.scoring import run_scoring

log = logging.getLogger(__name__)

REPORTS = REPO_ROOT / "reports"


@dataclass
class BacktestResult:
    run_id: int
    split: str
    days: int
    decisions: int
    trades: int
    net_bps_total: float
    artifact_path: str
    scoring: dict[str, Any]
    baselines: list[dict[str, Any]]
    calibration: dict[str, Any]
    equity: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "split": self.split,
            "days": self.days,
            "decisions": self.decisions,
            "trades": self.trades,
            "net_bps_total": self.net_bps_total,
            "artifact_path": self.artifact_path,
            "scoring": self.scoring,
            "baselines": self.baselines,
            "calibration": self.calibration,
            "equity": self.equity,
        }


def split_days(conn: psycopg.Connection, settings: Settings, split_name: str) -> list[date]:
    """Trading days of a split. Delegates to `Split.days()` so the boundary exists in ONE place:
    discovery is `[start_date, discovery_end)` and out-of-sample starts exactly at discovery_end."""
    split = window.frozen_split(conn, settings)
    if split_name not in ("discovery", "outofsample"):
        raise ValueError(f"unknown split {split_name!r}")
    return split.days(split_name)


def _equity_curve(conn: psycopg.Connection, run_id: int, *, start_equity: float = 10_000_000_000.0) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        select day_date, sum(net_bps) as net_bps, count(*) as trades,
               count(*) filter (where detail_json->>'same_bar_mark' = 'true') as unmeasured
        from backtest_trades where run_id = %s group by 1 order by 1
        """,
        (run_id,),
    ).fetchall()
    equity = start_equity
    out: list[dict[str, Any]] = []
    peak = equity
    for row in rows:
        net = float(row["net_bps"] or 0.0)
        equity *= (1.0 + net / 1e4)
        peak = max(peak, equity)
        out.append(
            {
                "day": row["day_date"].isoformat(),
                "net_bps": round(net, 4),
                "trades": int(row["trades"]),
                "unmeasured_marks": int(row["unmeasured"] or 0),
                "equity": round(equity, 2),
                "drawdown": round((equity / peak - 1.0) * 100.0, 4),
            }
        )
    return out


def run_backtest(
    conn: psycopg.Connection,
    settings: Settings,
    *,
    split_name: str,
    jev: JevClient,
    label: str | None = None,
    days: list[date] | None = None,
    persist: bool = True,
) -> BacktestResult:
    window.frozen_split(conn, settings)   # freeze/read the split before touching any day (R4)
    days = days if days is not None else split_days(conn, settings, split_name)
    if not days:
        raise ValueError(f"split {split_name!r} has no trading days in this configuration")

    engine = ReplayEngine(conn, settings, split_name=split_name, jev=jev, persist=True)
    config = {
        "mode": f"backtest_{split_name}",
        "label": label,
        "days": len(days),
        "first_day": days[0].isoformat(),
        "last_day": days[-1].isoformat(),
        "checkpoints": [t.strftime("%H:%M") for t in settings.checkpoint_times],
        "costs": settings.cost_bps,
        "gates": {
            "jev_min_confidence": settings.jev_min_confidence,
            "max_spread_bps_tradeable": settings.max_spread_bps_tradeable,
            "daily_loss_cap_bps": settings.daily_loss_cap_bps,
            "stop_loss_bps": settings.stop_loss_bps,
            "position_size_g": settings.position_size_g,
        },
        "flat_band_bps": settings.flat_band_bps,
        "jev": {"model": settings.jev_model, "host": settings.jev_host, "mock": settings.gm_demo_mode or not settings.jev_api_key},
        "frozen_split": window.frozen_split(conn, settings).as_dict(),
        "started_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    run_id = engine.open_run(config, split=split_name)
    try:
        summary = asyncio.run(engine.run_days(days))
        scoring_artifact = run_scoring(conn, settings, split_name=split_name, days=days, persist=True)
        records = build_day_records(conn, settings, days)
        baselines = [s.as_dict() for s in evaluate_all(records, settings=settings)]
        calibration = build_calibration(calibration_rows(conn, days)).as_dict()
        equity = _equity_curve(conn, run_id)
        engine.close_run("ok", notes=label or f"backtest {split_name}")
    except Exception as exc:  # noqa: BLE001 - mark the run failed, then re-raise
        engine.close_run("failed", notes=f"{type(exc).__name__}: {exc}")
        raise

    artifact = {
        "run_id": run_id,
        "config": config,
        "summary": summary,
        "scoring": scoring_artifact.as_dict(),
        "baselines": baselines,
        "calibration": calibration,
        "equity": equity,
        "jev_stats": jev.stats(),
        "n_day_records": len(records),
    }
    path = REPORTS / f"backtest_{split_name}.json"
    if persist:
        REPORTS.mkdir(exist_ok=True)
        path.write_text(json.dumps(artifact, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
        repo.audit(conn, "worker:backtest", "run_finished", {
            "run_id": run_id, "split": split_name, **summary, "artifact": str(path.relative_to(REPO_ROOT)),
        })
        conn.commit()
    return BacktestResult(
        run_id=run_id,
        split=split_name,
        days=summary["days"],
        decisions=summary["decisions"],
        trades=summary["trades"],
        net_bps_total=summary["net_bps_total"],
        artifact_path=str(path),
        scoring=scoring_artifact.as_dict(),
        baselines=baselines,
        calibration=calibration,
        equity=equity,
    )


def load_artifact(split_name: str) -> dict[str, Any] | None:
    path = REPORTS / f"backtest_{split_name}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def recent_runs(conn: psycopg.Connection, limit: int = 20) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            """
            select run_id, split, status, started_at, finished_at,
                   (config_json->>'label') as label,
                   (select count(*) from backtest_trades t where t.run_id = r.run_id) as trades
            from backtest_runs r order by run_id desc limit %s
            """,
            (limit,),
        ).fetchall()
    ]


def write_run_notes(conn: psycopg.Connection, run_id: int, notes: str) -> None:
    conn.execute("update backtest_runs set notes = %s where run_id = %s", (notes, run_id))
    conn.execute("insert into audit_log (actor, event, detail_json) values (%s, %s, %s)",
                 ("worker:backtest", "run_note", jsonb({"run_id": run_id, "notes": notes})))
    conn.commit()


__all__ = ["BacktestResult", "run_backtest", "split_days", "load_artifact", "recent_runs", "write_run_notes"]

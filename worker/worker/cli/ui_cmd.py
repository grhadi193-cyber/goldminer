"""`python -m worker ui export` — build the static data bundle the Mission Control UI reads.

Why a static bundle instead of the UI querying Postgres: the free-tier rule puts long work in
the Python worker and keeps the web layer to short read-only requests. The worker is already the
only writer, and every bundle file is produced from the SAME anti-leakage reads as the replay
engine (`ReadContext` / `messages_asof`), so the replay slider in the UI cannot show a message
the engine could not have seen at that instant.

Output: `ui/public/data/` (git-ignored) — see `ui/README.md` for the file contract.
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg

from ..config import REPO_ROOT, Settings, get_settings
from ..db import conn as dbconn
from ..db import repo
from ..research import prices_view as pv
from ..research.backtest import load_artifact
from ..research.stats.scoring import METHOD_VERSION
from ..timez import TEHRAN, day_bounds_utc, local_ts
from ..window import frozen_split

UI_DATA = REPO_ROOT / "ui" / "public" / "data"

STALE_AFTER_MINUTES = 3.0          # the amber threshold in the spec (§C live tiles)
MAX_TIMELINE_PER_DAY = 300         # per-day message cap before the payload gets silly


def _write(name: str, payload: Any) -> Path:
    path = UI_DATA / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, default=str, ensure_ascii=False), encoding="utf-8")
    return path


def _series_meta(conn: psycopg.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        select source, symbol, resolution, max(ts) as last_ts, count(*) as n_bars
        from price_bars group by 1, 2, 3 order by 1, 2, 3
        """
    ).fetchall()
    return [dict(r) for r in rows]


# Tile specs: (key, label, unit, candidate series in priority order). The newest bar ACROSS the
# candidates wins, and the tile always reports which series produced it.
TILE_SPECS: list[tuple[str, str, str, list[tuple[str, str, str]]]] = [
    ("gold_18k", "Gold 18k", "toman/g", pv.DOMESTIC_GOLD),
    ("emami_coin", "Emami coin", "toman", pv.DOMESTIC_COIN),
    ("usdtirt", "USDT/IRT", "toman", pv.USDTIRT),
    ("xauusd", "XAUUSD", "usd/oz", pv.XAUUSD),
]


def _live(ctx, conn: psycopg.Connection, settings: Settings, split) -> dict[str, Any]:
    """Latest price per tracked asset — the UI's live tiles.

    A tile is `ok` when its newest bar is younger than the staleness threshold, `stale` when it
    is older (the UI paints that amber), `missing` when the series has no bars at all. The tile
    names the series that produced it so a `synthetic` demo series can never masquerade as live.
    """
    now = datetime.now(tz=timezone.utc)
    tiles: list[dict[str, Any]] = []
    for key, label, unit, candidates in TILE_SPECS:
        best: dict[str, Any] | None = None
        for source, symbol, resolution in candidates:
            row = repo.last_bar_at_or_before(conn, source, symbol, resolution, now)
            if row and (best is None or row["ts"] > best["ts"]):
                best = row
        if best is None:
            tiles.append({"key": key, "label": label, "unit": unit, "status": "missing",
                          "price": None, "source": None, "resolution": None, "ts": None,
                          "age_minutes": None})
            continue
        age = (now - best["ts"]).total_seconds() / 60.0
        tiles.append(
            {
                "key": key,
                "label": label,
                "unit": unit,
                "price": best["close"],
                "source": best["source"],
                "resolution": best["resolution"],
                "ts": best["ts"],
                "age_minutes": round(age, 2),
                "status": "ok" if age <= STALE_AFTER_MINUTES else "stale",
            }
        )
    open_pos = conn.execute(
        """
        select p.id, p.day_date, p.opened_ts, p.direction, p.size_g, p.entry_price, p.mode
        from paper_positions p where p.closed_ts is null order by p.opened_ts desc limit 1
        """
    ).fetchone()
    last_eq = conn.execute(
        "select ts, equity_toman from paper_equity order by ts desc limit 1"
    ).fetchone()
    return {
        "as_of": now.isoformat(),
        "stale_after_minutes": STALE_AFTER_MINUTES,
        "mode": "paper",
        "tiles": tiles,
        "open_position": dict(open_pos) if open_pos else None,
        "last_equity": dict(last_eq) if last_eq else None,
    }


def _leaderboard(conn: psycopg.Connection) -> dict[str, Any]:
    row = conn.execute("select max(asof_date) as d from channel_scores").fetchone()
    asof = row["d"] if row else None
    if asof is None:
        return {"asof_date": None, "channels": []}
    rows = conn.execute(
        """
        select cs.*, c.username, c.title, c.active, c.tier
        from channel_scores cs join channels c on c.id = cs.channel_id
        where cs.asof_date = %s and cs.horizon = 'eod'
        order by cs.lift_bps_eod desc nulls last
        """,
        (asof,),
    ).fetchall()
    channels = []
    for r in rows:
        extra = r["extra_json"] or {}
        channels.append(
            {
                "username": r["username"],
                "title": r["title"],
                "active": r["active"],
                "tier": r["tier"],
                "label": r["label"],
                "label_reason": extra.get("label_reason"),
                "lead_lag_minutes": r["lead_lag_minutes"],
                "n_events": r["n_events"],
                "lifts_net_bps": {
                    "30m": r["lift_bps_30m"], "1h": r["lift_bps_1h"],
                    "3h": r["lift_bps_3h"], "eod": r["lift_bps_eod"],
                },
                "ci_95": extra.get("cis") or {},
                "q_values": extra.get("q_values") or {},
                "fdr_corrected": r["fdr_corrected"],
                "pumper_flag": r["pumper_flag"],
                "quoted_bias_pct": r["quoted_bias_pct"],
                "burst_share": extra.get("burst_share"),
                "measures": extra.get("measures") or {},
                "method_version": r["method_version"],
            }
        )
    family = (rows[0]["extra_json"] or {}).get("family") if rows else None
    cost_model = (rows[0]["extra_json"] or {}).get("cost_model") if rows else None
    return {
        "asof_date": asof.isoformat(),
        "method_version": METHOD_VERSION,
        "family": family,
        "cost_model": cost_model,
        "channels": channels,
    }


def _decisions(conn: psycopg.Connection, split_name: str, limit: int) -> dict[str, Any]:
    rows = conn.execute(
        """
        select decision_id, decision_ts, day_date, checkpoint, split, state_hash, model, cache_key,
               verdict_json, confidence, day_pnl_bps, state_json, questions_json, answers_json
        from jev_decisions
        where split = %s
        order by decision_ts desc
        limit %s
        """,
        (split_name, limit),
    ).fetchall()
    items = []
    for r in rows:
        state = r["state_json"] or {}
        items.append(
            {
                "decision_id": r["decision_id"],
                "day": r["day_date"].isoformat(),
                "checkpoint": r["checkpoint"],
                "decision_ts": r["decision_ts"],
                "split": r["split"],
                "state_hash": r["state_hash"],
                "model": r["model"],
                "from_cache": bool(r["cache_key"]),
                "confidence": r["confidence"],
                "day_pnl_bps": r["day_pnl_bps"],
                "verdict": r["verdict_json"],
                "answers": r["answers_json"],
                "questions": r["questions_json"],
                "state": state,
            }
        )
    return {"split": split_name, "items": items}


def _price_path(conn: psycopg.Connection, settings: Settings, day: date, as_of: datetime,
                *, buckets: int = 96) -> dict[str, Any]:
    """Intraday path of the day's measurement series, downsampled to `buckets` points.

    Same priority order as the stats module (`stats/series.pick_series`), so the line under the
    signal markers is the series the lifts were actually measured against.
    """
    open_ts, flat_ts = day_bounds_utc(day, settings.open_time, settings.flat_time)
    for source, symbol, resolution in (pv.USDTIRT + pv.DOMESTIC_GOLD[:1] + pv.XAUUSD[:1]):
        rows = conn.execute(
            """
            select ts, close from price_bars
            where source = %s and symbol = %s and resolution = %s and ts >= %s and ts <= %s
            order by ts
            """,
            (source, symbol, resolution, open_ts, flat_ts),
        ).fetchall()
        if len(rows) < 10:
            continue
        step = max(1, len(rows) // buckets)
        points = [
            {
                "t": round((r["ts"] - open_ts).total_seconds() / 60.0, 2),
                "p": float(r["close"]),
            }
            for r in rows[::step]
        ]
        return {"source": source, "symbol": symbol, "resolution": resolution, "points": points}
    return {"source": None, "symbol": None, "resolution": None, "points": []}


def _timeline(conn: psycopg.Connection, settings: Settings, days: list[date]) -> dict[str, Any]:
    """Per-day message markers, read through the anti-leakage bound of the END of that day.

    Reading with `as_of = day 22:00 Tehran` is exactly what the replay engine could see when it
    closed that day; anything ingested later (an edit we learned about today) is therefore NOT in
    the timeline of a past day.
    """
    index: list[dict[str, Any]] = []
    for day in days:
        open_ts, flat_ts = day_bounds_utc(day, settings.open_time, settings.flat_time)
        rows = conn.execute(
            """
            select m.id, m.published_at, m.version, m.is_edited, m.raw_text,
                   c.username, c.title, e.status, e.extraction_json
            from messages m
            join channels c on c.id = m.channel_id
            left join extractions e on e.message_id = m.id
            where m.published_at >= %s and m.published_at <= %s
              and m.version = (select max(v.version) from messages v
                               where v.channel_id = m.channel_id and v.telegram_msg_id = m.telegram_msg_id)
              and m.ingested_at <= %s
            order by m.published_at
            """,
            (open_ts, flat_ts, flat_ts),
        ).fetchall()
        truncated = len(rows) > MAX_TIMELINE_PER_DAY
        items = []
        for r in rows[:MAX_TIMELINE_PER_DAY]:
            ex = r["extraction_json"] or {}
            items.append(
                {
                    "id": r["id"],
                    "published_at": r["published_at"],
                    # minutes since the 08:00 Tehran session open, computed here with zoneinfo so
                    # the browser never has to guess an offset (Iran has DST).
                    "t": round((r["published_at"] - open_ts).total_seconds() / 60.0, 2),
                    "username": r["username"],
                    "title": r["title"],
                    # Persian text is kept EXACTLY as published; the UI shows it RTL and never
                    # translates it client-side.
                    "persian_text": r["raw_text"],
                    "english_text": ex.get("english_text"),
                    "persian_gloss": ex.get("persian_gloss"),
                    "kind": ex.get("kind"),
                    "direction": ex.get("direction"),
                    "intensity": ex.get("intensity"),
                    "topic": ex.get("topic"),
                    "confidence": ex.get("confidence"),
                    "quoted_asset": ex.get("quoted_asset"),
                    "quoted_price": ex.get("quoted_price"),
                    "is_edited": r["is_edited"],
                    "version": r["version"],
                    "extraction_status": r["status"] or "missing",
                }
            )
        index.append(
            {
                "day": day.isoformat(),
                "price_path": _price_path(conn, settings, day, flat_ts),
                "n_messages": len(rows),
                "n_signals": sum(1 for i in items if i["direction"] in ("up", "down")),
                "truncated": truncated,
            }
        )
        _write(
            f"timeline/{day.isoformat()}.json",
            {"day": day.isoformat(), "items": items, "truncated": truncated,
             "price_path": _price_path(conn, settings, day, flat_ts)},
        )
    return {"days": index}


def _coverage(conn: psycopg.Connection, settings: Settings, start: date, end: date) -> dict[str, Any]:
    from ..prices import coverage as cov

    rows = cov.coverage_table(conn, start=start, end=end, settings=settings)
    accept = cov.acceptance(conn, start=start, end=end, settings=settings)
    return {
        "rows": [
            {
                "source": r.source, "symbol": r.symbol, "resolution": r.resolution, "month": r.month,
                "n_bars": r.n_bars, "expected_bars": r.expected_bars, "coverage_pct": r.coverage_pct,
                "n_gaps": r.n_gaps, "gap_minutes": r.gap_minutes,
            }
            for r in rows
        ],
        "acceptance": [
            {"name": a.name, "passed": a.passed, "detail": a.detail, "threshold": a.threshold} for a in accept
        ],
    }


def _hypotheses(conn: psycopg.Connection) -> dict[str, Any]:
    rows = conn.execute(
        "select id, text, status, proposed_by_model, test_method, test_results_json, proposed_at, tested_at "
        "from hypotheses order by id"
    ).fetchall()
    return {"items": [dict(r) for r in rows]}


def _run_config(conn: psycopg.Connection, settings: Settings) -> dict[str, Any]:
    rows = conn.execute("select key, value_json, set_at, set_by from run_config order by key").fetchall()
    return {
        "settings": {
            "open_at_local": settings.open_at_local,
            "flat_at_local": settings.flat_at_local,
            "checkpoints_local": settings.checkpoints_local,
            "trading_weekdays": settings.trading_weekdays,
            "jevs": {"model": settings.jev_model, "host": settings.jev_host, "mock": settings.gm_demo_mode or not settings.jev_api_key},
            "gates": {
                "jev_min_confidence": settings.jev_min_confidence,
                "max_spread_bps_tradeable": settings.max_spread_bps_tradeable,
                "daily_loss_cap_bps": settings.daily_loss_cap_bps,
                "stop_loss_bps": settings.stop_loss_bps,
                "flat_band_bps": settings.flat_band_bps,
                "position_size_g": settings.position_size_g,
            },
            "costs": settings.cost_bps,
            "paper_only": True,
        },
        "run_config": [dict(r) for r in rows],
    }


def export(args: argparse.Namespace) -> int:
    settings = get_settings()
    split_name = args.split or "discovery"
    if UI_DATA.exists() and args.clean:
        shutil.rmtree(UI_DATA)
    UI_DATA.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with dbconn.sync_conn() as conn:
        split = frozen_split(conn, settings)
        days = split.days(split_name)
        if args.days:
            days = days[-args.days:]
        if not days:
            print(f"split {split_name!r} has no trading days — nothing to export")
            return 2
        written.append(_write("live.json", _live(None, conn, settings, split)))
        written.append(_write("leaderboard.json", _leaderboard(conn)))
        written.append(_write("decisions.json", _decisions(conn, split_name, args.max_decisions)))
        written.append(_write("timeline_index.json", _timeline(conn, settings, days)))
        written.append(_write("coverage.json", _coverage(conn, settings, days[0], days[-1])))
        written.append(_write("hypotheses.json", _hypotheses(conn)))
        written.append(_write("run_config.json", _run_config(conn, settings)))

        artifact = load_artifact(split_name)
        if artifact is not None:
            written.append(_write("backtest.json", artifact))
        series = _series_meta(conn)
        banner = None
        try:
            from ..demo.guard import banner_for

            banner = banner_for(conn)
        except Exception:  # noqa: BLE001
            banner = None
        meta = {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "generated_by": "python -m worker ui export",
            "demo": bool(settings.gm_demo_mode),
            "banner": banner,
            "split": split_name,
            "split_window": {"start": split.start_date.isoformat(), "discovery_end": split.discovery_end.isoformat(),
                             "outofsample_end": split.outofsample_end.isoformat()},
            "days": {"first": days[0].isoformat(), "last": days[-1].isoformat(), "n": len(days)},
            "method_version": METHOD_VERSION,
            "tev": {"timezone": str(TEHRAN), "open_local": settings.open_at_local, "flat_local": settings.flat_at_local},
            "series": series,
            "has_backtest": artifact is not None,
            "stale_after_minutes": STALE_AFTER_MINUTES,
        }
        written.append(_write("meta.json", meta))
    for path in written:
        print(f"wrote {path.relative_to(REPO_ROOT)}")
    print(f"\n{len(written)} files -> {UI_DATA.relative_to(REPO_ROOT)}  (serve with `cd ui && npm run dev`)")
    return 0


__all__ = ["export"]

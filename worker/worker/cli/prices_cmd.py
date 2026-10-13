"""`python -m worker prices ...` — price ingestion, derivation, coverage verification (M1/M4)."""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from .. import window
from ..config import REPO_ROOT, get_settings
from ..db import conn as dbconn
from ..db import repo
from ..prices import coverage as cov
from ..prices.derive import derive_resolution
from ..prices.live import log_once
from ..prices.nobitex import NobitexProvider
from ..prices.symbols import SERIES
from ..prices.tgju import SLUGS, TgjuProvider
from ..prices.tsetmc import TsetmcProvider
from ..prices.xauusd import provider_for
from ..timez import local_ts, today_tehran
from ..prices.base import bars_to_rows

log = logging.getLogger(__name__)


def _range(args: argparse.Namespace) -> tuple[date, date]:
    settings = get_settings()
    split = window.compute_split(settings)
    end = today_tehran()
    start = split.history_start
    if getattr(args, "start", None):
        start = date.fromisoformat(args.start)
    if getattr(args, "end", None):
        end = date.fromisoformat(args.end)
    if getattr(args, "tail_days", None):
        start = max(start, end - timedelta(days=args.tail_days))
    return start, end


def _start_ts(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------- nobitex

def nobitex(args: argparse.Namespace) -> int:
    start, end = _range(args)
    settings = get_settings()
    with dbconn.sync_conn() as conn:
        key = f"ingest_progress:nobitex:USDTIRT:1m"
        resume = repo.get_run_config(conn, key) or {}
        if resume.get("last_ts"):
            resumed = datetime.fromisoformat(resume["last_ts"]).date() - timedelta(days=1)
            start = max(start, resumed)
        log.info("nobitex USDTIRT 1m backfill %s .. %s", start, end)

        provider = NobitexProvider(settings.nobitex_base, max_req_per_min=settings.nobitex_max_req_per_min)
        total = 0
        cursor = resume
        for day in window.compute_split(settings).days("discovery") + window.compute_split(settings).days("outofsample"):
            if not (start <= day <= end):
                continue
            open_ts, flat_ts = local_ts(day, settings.open_time), local_ts(day, settings.flat_time)
            bars, failures = asyncio.run(provider.fetch_range_chunked(open_ts, flat_ts))
            rows = bars_to_rows("nobitex", "USDTIRT", "1m", bars)
            written = repo.upsert_price_bars(conn, rows)
            for f_start, f_end, reason in failures:
                repo.record_gap(conn, "nobitex", "USDTIRT", f_start, f_end, reason)
            total += written
            cursor = {"last_ts": flat_ts.isoformat(), "last_day": day.isoformat(), "written": total}
            repo.set_run_config(conn, key, cursor, set_by="worker:prices")
            conn.commit()
            log.info("nobitex %s: %d bars (cumulative %d)", day, written, total)
        repo.audit(conn, "worker:prices", "nobitex_backfill", {"start": start.isoformat(), "end": end.isoformat(), "bars": total})
        print(f"nobitex USDTIRT 1m: {total} bars upserted for {start}..{end}")
    return 0


# ---------------------------------------------------------------------------- xauusd

def xauusd(args: argparse.Namespace) -> int:
    start, end = _range(args)
    settings = get_settings()
    with dbconn.sync_conn() as conn:
        for resolution, symbol in (("1h", "XAUUSD"), ("1d", "XAUUSD")):
            provider = provider_for(settings.xauusd_provider, symbol=symbol, resolution=resolution)
            try:
                bars = asyncio.run(provider.fetch(_start_ts(start), _start_ts(end + timedelta(days=1))))  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                repo.record_gap(conn, "yfinance", symbol, _start_ts(start), _start_ts(end + timedelta(days=1)),
                                f"provider_failed:{type(exc).__name__}")
                print(f"XAUUSD {resolution}: FAILED ({exc}) — recorded as a gap")
                conn.commit()
                continue
            written = repo.upsert_price_bars(conn, bars_to_rows("yfinance", symbol, resolution, bars))
            print(f"XAUUSD {resolution}: {written} bars ({start}..{end})")
            if resolution == "1h":
                series = SERIES["xauusd_1h"]
                gaps = cov.record_missing_bars(conn, series, start=start, end=end, settings=settings)
                if gaps:
                    print(f"  recorded {gaps} thin/empty sessions in price_gaps")
        repo.audit(conn, "worker:prices", "xauusd_backfill", {"start": start.isoformat(), "end": end.isoformat()})
    return 0


# ---------------------------------------------------------------------------- tgju

def tgju(args: argparse.Namespace) -> int:
    start, end = _range(args)
    with dbconn.sync_conn() as conn:
        provider = TgjuProvider()
        for slug, symbol in SLUGS.items():
            if symbol == "xauusd_ounce_usd":
                continue  # the ounce comes from the XAUUSD provider, not TGJU's page
            try:
                bars = asyncio.run(provider.fetch(slug, start, end))
            except Exception as exc:  # noqa: BLE001
                repo.record_gap(conn, "tgju", symbol, local_ts(start, time(0, 0)), local_ts(end, time(23, 59)),
                                f"provider_failed:{type(exc).__name__}")
                print(f"tgju {slug} ({symbol}): FAILED ({exc}) — recorded as a gap")
                conn.commit()
                continue
            written = repo.upsert_price_bars(conn, bars_to_rows("tgju", symbol, "1d", bars))
            series = next((s for s in SERIES.values() if s.source == "tgju" and s.symbol == symbol), None)
            gaps = cov.record_missing_bars(conn, series, start=start, end=end) if series else 0
            print(f"tgju {slug} ({symbol}): {written} daily bars, {gaps} missing trading days")
        repo.audit(conn, "worker:prices", "tgju_backfill", {"start": start.isoformat(), "end": end.isoformat()})
    return 0


# ---------------------------------------------------------------------------- tsetmc

def tsetmc(args: argparse.Namespace) -> int:
    settings = get_settings()
    with dbconn.sync_conn() as conn:
        stored = repo.get_run_config(conn, "tsetmc_symbols") or {}
        symbols = list(stored.get("symbols") or settings.tsetmc_symbol_list)
        if not symbols:
            print("no TSETMC symbols configured (TSETMC_SYMBOLS or run_config['tsetmc_symbols'])")
            return 1
        provider = TsetmcProvider()
        for symbol in symbols:
            try:
                ins_code = asyncio.run(provider.fetch_ins_code(symbol))
            except Exception as exc:  # noqa: BLE001
                print(f"tsetmc {symbol}: search failed ({exc})")
                continue
            if not ins_code:
                print(f"tsetmc {symbol}: no insCode found")
                continue
            try:
                daily = asyncio.run(provider.fetch_daily(ins_code))
                intraday = asyncio.run(provider.fetch_intraday(ins_code))
            except Exception as exc:  # noqa: BLE001
                print(f"tsetmc {symbol}: fetch failed ({exc})")
                continue
            n1 = repo.upsert_price_bars(conn, bars_to_rows("tsetmc", symbol, "1d", daily))
            n2 = repo.upsert_price_bars(conn, bars_to_rows("tsetmc", symbol, "1h", intraday)) if intraday else 0
            print(f"tsetmc {symbol} (insCode {ins_code}): {n1} daily, {n2} intraday bars")
            repo.set_run_config(conn, f"tsetmc_ins_code:{symbol}", {"ins_code": ins_code}, set_by="worker:prices")
        repo.audit(conn, "worker:prices", "tsetmc_backfill", {"symbols": symbols})
    return 0


# ---------------------------------------------------------------------------- derive/verify/gaps

def derive(args: argparse.Namespace) -> int:
    with dbconn.sync_conn() as conn:
        out = derive_resolution(conn, source="nobitex", symbol="USDTIRT", base_resolution="1m", targets=("5m", "1h"))
        print(f"derived from nobitex 1m: {out}")
        out2 = derive_resolution(conn, source="synthetic", symbol="gold_18k_toman_per_gram", base_resolution="1m", targets=("5m", "1h"))
        if any(out2.values()):
            print(f"derived from synthetic 1m: {out2}")
    return 0


def verify(args: argparse.Namespace) -> int:
    start, end = _range(args)
    settings = get_settings()
    with dbconn.sync_conn() as conn:
        rows = cov.coverage_table(conn, start=start, end=end, settings=settings)
        accept = cov.acceptance(conn, start=start, end=end, settings=settings)
        markdown = cov.coverage_markdown(rows, accept, window=f"{start} .. {end}")
        out_path = REPO_ROOT / "docs" / "coverage_report.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        from ..demo.guard import banner_for

        banner = banner_for(conn)
        out_path.write_text((banner + "\n\n" if banner else "") + markdown + "\n", encoding="utf-8")
        print(markdown)
        print(f"\nwritten: {out_path.relative_to(REPO_ROOT)}")
        for series in SERIES.values():
            if series.resolution == "1d":
                cov.record_missing_bars(conn, series, start=start, end=end, settings=settings)
        conn.commit()
        repo.audit(conn, "worker:prices", "verify_sources", {"rows": len(rows), "fail": [a.name for a in accept if not a.passed]})
    failed = [a for a in accept if not a.passed]
    if failed:
        print(f"\nCOVERAGE ACCEPTANCE FAILED for: {', '.join(a.name for a in failed)}")
        return 2
    print("\ncoverage acceptance: PASS")
    return 0


def gaps(args: argparse.Namespace) -> int:
    with dbconn.sync_conn() as conn:
        rows = conn.execute(
            """
            select source, symbol, count(*) as n_gaps, min(from_ts) as first_gap, max(to_ts) as last_gap,
                   sum(extract(epoch from (to_ts - from_ts))/3600.0) as hours
            from price_gaps group by 1,2 order by 1,2
            """
        ).fetchall()
    if not rows:
        print("no gaps recorded — suspicious: verify coverage before trusting any report (R8)")
        return 0
    print(f"{'source':12s} {'symbol':28s} {'gaps':>6s} {'hours':>8s}  first_gap .. last_gap")
    for r in rows:
        print(f"{r['source']:12s} {r['symbol']:28s} {r['n_gaps']:6d} {float(r['hours'] or 0):8.1f}  "
              f"{r['first_gap']} .. {r['last_gap']}")
    return 0


def live(args: argparse.Namespace) -> int:
    settings = get_settings()
    with dbconn.sync_conn() as conn:
        n = asyncio.run(log_once(settings, conn))
        print(f"live logger wrote {n} bar(s)")
    return 0


__all__ = ["nobitex", "xauusd", "tgju", "tsetmc", "derive", "verify", "gaps", "live"]


def _unused(path: Path) -> None:  # pragma: no cover
    _ = path

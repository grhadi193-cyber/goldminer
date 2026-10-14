#!/usr/bin/env python
"""M1 — verify every price source: probe it live, then print a coverage table.

Usage
-----
    python scripts/verify_sources.py                 # coverage table + acceptance checks + write docs
    python scripts/verify_sources.py --probe         # also hit each upstream API live
    python scripts/verify_sources.py --start 2026-03-14 --end 2026-10-13

Exit codes
----------
    0  all acceptance checks pass
    2  a coverage acceptance check failed (R8: never patch data to fit assumptions)
    3  live probes failed AND coverage cannot be verified either

Probes are deliberately small (one symbol, a couple of hours) so running this on a free tier
costs nothing and stays polite. Anything a probe learns that contradicts docs/source_notes.md
must be recorded there before adapting the ingestion module.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))

from worker import window  # noqa: E402
from worker.config import get_settings  # noqa: E402
from worker.db import conn as dbconn  # noqa: E402
from worker.db import repo  # noqa: E402
from worker.prices import coverage as cov  # noqa: E402
from worker.prices.live import poll_nobitex, poll_tgju  # noqa: E402
from worker.prices.nobitex import NobitexProvider  # noqa: E402
from worker.prices.symbols import SERIES  # noqa: E402
from worker.timez import local_ts, today_tehran  # noqa: E402


async def probe_nobitex(settings) -> tuple[bool, str]:
    provider = NobitexProvider(settings.nobitex_base, max_req_per_min=settings.nobitex_max_req_per_min)
    end = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(hours=3)
    try:
        bars = await provider.fetch(start, end)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:160]}"
    return bool(bars), f"{len(bars)} 1m bars in the last 3h (endpoint {settings.nobitex_base}/market/udf/history)"


async def probe_tgju(settings) -> tuple[bool, str]:
    import httpx

    async with httpx.AsyncClient(timeout=15.0) as client:
        quotes = await poll_tgju(client, "https://api.tgju.org")
    if not quotes:
        return False, "no quotes returned (endpoint shape may have drifted — see docs/source_notes.md)"
    detail = ", ".join(f"{q.symbol}={q.price:,.0f}" for q in quotes)
    return True, detail


async def probe_xauusd(settings) -> tuple[bool, str]:
    from worker.prices.xauusd import provider_for

    provider = provider_for(settings.xauusd_provider, symbol="XAUUSD", resolution="1h")
    end = datetime.now(tz=timezone.utc)
    start = end - timedelta(days=5)
    try:
        bars = await provider.fetch(start, end)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:160]}"
    return bool(bars), f"{len(bars)} hourly bars in the last 5 days"


async def probe_nobitex_stats(settings) -> tuple[bool, str]:
    import httpx

    async with httpx.AsyncClient(timeout=15.0) as client:
        quotes = await poll_nobitex(client, settings.nobitex_base)
    return bool(quotes), (f"{quotes[0].symbol}={quotes[0].price:,.0f} toman" if quotes else "no stats payload")


PROBES = [
    ("nobitex UDF history (USDTIRT 1m)", probe_nobitex),
    ("nobitex market stats", probe_nobitex_stats),
    ("tgju summary-table-data", probe_tgju),
    ("xauusd provider hourly", probe_xauusd),
]


async def run_probes(settings) -> bool:
    print("Live probes")
    print("-----------")
    ok_all = True
    for name, fn in PROBES:
        t0 = time.monotonic()
        try:
            ok, detail = await fn(settings)
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"probe crashed: {type(exc).__name__}: {exc}"
        dt = (time.monotonic() - t0) * 1000
        print(f"[{'ok  ' if ok else 'FAIL'}] {name:34s} {dt:7.0f} ms  {detail}")
        ok_all &= ok
    print()
    if not ok_all:
        print(
            "Some probes failed. If the payload shape differs from the documented one, record it in\n"
            "docs/source_notes.md and adapt the provider — do NOT patch data (R13.3).\n"
        )
    return ok_all


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--probe", action="store_true", help="hit upstream APIs live")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--no-write", action="store_true", help="do not write docs/coverage_report.md")
    args = parser.parse_args(argv)

    settings = get_settings()
    split = window.compute_split(settings)
    start = date.fromisoformat(args.start) if args.start else split.history_start
    end = date.fromisoformat(args.end) if args.end else today_tehran()

    probes_ok = True
    if args.probe:
        try:
            probes_ok = asyncio.run(run_probes(settings))
        except Exception as exc:  # noqa: BLE001 - network may be entirely unavailable
            print(f"live probes could not run: {type(exc).__name__}: {exc}\n")
            probes_ok = False

    with dbconn.sync_conn() as conn:
        rows = cov.coverage_table(conn, start=start, end=end, settings=settings)
        accept = cov.acceptance(conn, start=start, end=end, settings=settings)
        markdown = cov.coverage_markdown(rows, accept, window=f"{start} .. {end}")
        if not args.no_write:
            out = Path(__file__).resolve().parents[1] / "docs" / "coverage_report.md"
            from worker.demo.guard import banner_for

            banner = banner_for(conn)
            out.write_text((banner + "\n\n" if banner else "") + markdown + "\n", encoding="utf-8")
            print(f"written: {out}\n")
        for series in SERIES.values():
            if series.resolution == "1d":
                cov.record_missing_bars(conn, series, start=start, end=end, settings=settings)
        conn.commit()
        repo.audit(conn, "scripts:verify_sources", "probe", {
            "probes_ok": probes_ok,
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "failed_checks": [a.name for a in accept if not a.passed],
        })

    print(markdown)
    failed = [a for a in accept if not a.passed]
    if failed:
        print(f"\nCOVERAGE ACCEPTANCE FAILED: {', '.join(a.name for a in failed)}")
        print("Ask the operator to accept the gaps or fix ingestion (M1 decision point).")
        return 2
    print("\ncoverage acceptance: PASS")
    return 0 if probes_ok or not args.probe else 3


if __name__ == "__main__":
    raise SystemExit(main())

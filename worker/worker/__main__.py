"""`python -m worker <command>` — every entry point is idempotent and resumable.

Design notes (R9 / R13.4):
  * commands are chunked; Ctrl-C loses at most one chunk
  * progress checkpoints live in `run_config` so reruns resume
  * subcommand imports are lazy so a missing optional dependency (telethon, yfinance)
    never breaks unrelated commands
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any, Callable

from .log import setup_logging

USAGE = """Mishkah worker — Phase 1 (research + paper only)

command groups:
  db        up | down | status
  jev       smoke | decide --date YYYY-MM-DD [--checkpoint 0800] | tree
  channels  sync | list
  ingest    telegram [--since] [--channels a,b] [--limit]
  prices    nobitex | xauusd | tgju | tsetmc | derive | verify | gaps
  extract   run [--limit N] [--batch N] | stats
  replay    day --date YYYY-MM-DD | range --split discovery|outofsample
  stats     event-study | leadlag | calibration | baselines | all
  backtest  run --split discovery|outofsample [--label L]
  paper     monitor | what-if --date YYYY-MM-DD
  report    build --split discovery|outofsample
  server    run [--host 0.0.0.0] [--port 8787]
  ui        export [--split discovery] [--days 45] [--clean]
  hypothesis test --id N | test-all | list
  server    run
  demo      seed | reset | status     (synthetic data — never research results)
  selftest
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m worker", description=USAGE,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="group", required=True)

    # db -------------------------------------------------------------------------------
    db = sub.add_parser("db", help="schema migrations")
    db_sub = db.add_subparsers(dest="action", required=True)
    up = db_sub.add_parser("up")
    up.add_argument("--until", default=None)
    down = db_sub.add_parser("down")
    down.add_argument("--steps", type=int, default=1)
    db_sub.add_parser("status")

    # jev ------------------------------------------------------------------------------
    jev = sub.add_parser("jev", help="System One client")
    jev_sub = jev.add_subparsers(dest="action", required=True)
    jev_sub.add_parser("smoke")
    jd = jev_sub.add_parser("decide")
    jd.add_argument("--date", required=True)
    jd.add_argument("--checkpoint", default="0800")
    jev_sub.add_parser("tree")

    # channels -------------------------------------------------------------------------
    ch = sub.add_parser("channels", help="channel registry")
    ch_sub = ch.add_subparsers(dest="action", required=True)
    ch_sub.add_parser("sync")
    ch_sub.add_parser("list")

    # ingest ---------------------------------------------------------------------------
    ing = sub.add_parser("ingest", help="telegram ingestion (READ-ONLY)")
    ing_sub = ing.add_subparsers(dest="action", required=True)
    tg = ing_sub.add_parser("telegram")
    tg.add_argument("--since", default=None)
    tg.add_argument("--channels", default=None)
    tg.add_argument("--limit", type=int, default=None)
    tg.add_argument("--once", action="store_true", help="single pass instead of polling")

    # prices ---------------------------------------------------------------------------
    pr = sub.add_parser("prices", help="price ingestion")
    pr_sub = pr.add_subparsers(dest="action", required=True)
    for name in ("nobitex", "xauusd", "tgju", "tsetmc", "derive", "live"):
        p = pr_sub.add_parser(name)
        p.add_argument("--start", default=None)
        p.add_argument("--end", default=None)
        p.add_argument("--tail-days", type=int, default=None)
    pr_sub.add_parser("verify")
    pr_sub.add_parser("gaps")

    # extract --------------------------------------------------------------------------
    ex = sub.add_parser("extract", help="LLM translation/extraction")
    ex_sub = ex.add_subparsers(dest="action", required=True)
    exr = ex_sub.add_parser("run")
    exr.add_argument("--limit", type=int, default=None)
    exr.add_argument("--batch", type=int, default=200)
    exr.add_argument("--only-missing", action="store_true", default=True)
    ex_sub.add_parser("stats")
    ex_sub.add_parser("selftest")

    # replay ---------------------------------------------------------------------------
    rp = sub.add_parser("replay", help="replay engine")
    rp_sub = rp.add_subparsers(dest="action", required=True)
    rd = rp_sub.add_parser("day")
    rd.add_argument("--date", required=True)
    rd.add_argument("--checkpoint", default=None)
    rd.add_argument("--split", default="discovery")
    rd.add_argument("--dry-run", action="store_true")
    rr = rp_sub.add_parser("range")
    rr.add_argument("--split", default="discovery")
    rr.add_argument("--limit", type=int, default=None)

    # stats ----------------------------------------------------------------------------
    st = sub.add_parser("stats", help="statistics module")
    st_sub = st.add_subparsers(dest="action", required=True)
    for name in ("event-study", "leadlag", "calibration", "baselines", "costs", "all"):
        s = st_sub.add_parser(name)
        s.add_argument("--split", default="discovery")

    # backtest -------------------------------------------------------------------------
    bt = sub.add_parser("backtest", help="backtest runner")
    bt_sub = bt.add_subparsers(dest="action", required=True)
    btr = bt_sub.add_parser("run")
    btr.add_argument("--split", default="discovery", required=False)
    btr.add_argument("--label", default=None)

    # paper ----------------------------------------------------------------------------
    pp = sub.add_parser("paper", help="paper monitor (live mode, no real orders)")
    pp_sub = pp.add_subparsers(dest="action", required=True)
    pm = pp_sub.add_parser("monitor")
    pm.add_argument("--once", action="store_true")
    pm.add_argument("--no-ingest", action="store_true")
    pw = pp_sub.add_parser("what-if")
    pw.add_argument("--date", required=True)

    # report ---------------------------------------------------------------------------
    rp2 = sub.add_parser("report", help="report generator")
    rp2_sub = rp2.add_subparsers(dest="action", required=True)
    rb = rp2_sub.add_parser("build")
    rb.add_argument("--split", default="discovery")
    rb.add_argument("--out", default=None)
    rb.add_argument("--no-narrative", action="store_true")

    # hypothesis -----------------------------------------------------------------------
    hy = sub.add_parser("hypothesis", help="hypothesis registry")
    hy_sub = hy.add_subparsers(dest="action", required=True)
    hyt = hy_sub.add_parser("test")
    hyt.add_argument("--id", type=int, required=True)
    hy_sub.add_parser("list")
    hya = hy_sub.add_parser("test-all")
    hya.add_argument("--limit", type=int, default=20)

    # ui (static bundle for the Mission Control front end) ------------------------------
    uc = sub.add_parser("ui", help="export the static data bundle the UI reads")
    uc_sub = uc.add_subparsers(dest="action", required=True)
    ue = uc_sub.add_parser("export")
    ue.add_argument("--split", default="discovery")
    ue.add_argument("--days", type=int, default=45, help="timeline days to export (last N)")
    ue.add_argument("--max-decisions", type=int, default=60, dest="max_decisions")
    ue.add_argument("--clean", action="store_true", help="wipe ui/public/data first")

    # server ---------------------------------------------------------------------------
    sv = sub.add_parser("server", help="worker HTTP surface (bundle reads, panel API, webhook)")
    sv_sub = sv.add_subparsers(dest="action", required=True)
    svr = sv_sub.add_parser("run")
    svr.add_argument("--host", default=None)
    svr.add_argument("--port", type=int, default=None)

    # demo (synthetic dataset for offline pipeline validation) --------------------------
    dm = sub.add_parser("demo", help="synthetic demo dataset (never research results)")
    dm_sub = dm.add_subparsers(dest="action", required=True)
    dms = dm_sub.add_parser("seed")
    dms.add_argument("--days", type=int, default=None)
    dms.add_argument("--message-days", type=int, default=None, dest="message_days")
    dms.add_argument("--seed", type=int, default=20260401, dest="seed_value")
    dms.add_argument("--skip-extraction", action="store_true")
    dm_sub.add_parser("reset")
    dm_sub.add_parser("status")

    sub.add_parser("selftest", help="environment + connectivity self check")
    return parser


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = _dispatch(args)
    return handler(args)


def _dispatch(args: argparse.Namespace) -> Callable[[argparse.Namespace], int]:
    group, action = args.group, getattr(args, "action", None)

    if group == "db":
        from .cli import db_cmd

        return getattr(db_cmd, action)
    if group == "jev":
        from .cli import jev_cmd

        return getattr(jev_cmd, action)
    if group == "channels":
        from .cli import channels_cmd

        return getattr(channels_cmd, action)
    if group == "ingest":
        from .cli import ingest_cmd

        return ingest_cmd.telegram
    if group == "prices":
        from .cli import prices_cmd

        return getattr(prices_cmd, action)
    if group == "extract":
        from .cli import extract_cmd

        return getattr(extract_cmd, action)
    if group == "replay":
        from .cli import replay_cmd

        return {"day": replay_cmd.day, "range": replay_cmd.range_}[action]
    if group == "stats":
        from .cli import stats_cmd

        return {
            "event-study": stats_cmd.event_study,
            "leadlag": stats_cmd.leadlag,
            "calibration": stats_cmd.calibration,
            "baselines": stats_cmd.baselines,
            "costs": stats_cmd.costs,
            "all": stats_cmd.all_,
        }[action]
    if group == "backtest":
        from .cli import backtest_cmd

        return backtest_cmd.run
    if group == "paper":
        from .cli import paper_cmd

        return getattr(paper_cmd, action.replace("-", "_"))
    if group == "ui":
        from .cli import ui_cmd

        return ui_cmd.export

    if group == "report":
        from .cli import report_cmd

        return report_cmd.build
    if group == "hypothesis":
        from .cli import hypothesis_cmd

        return {"list": hypothesis_cmd.list_hypotheses, "test": hypothesis_cmd.test,
                "test-all": hypothesis_cmd.test_all}[action]
    if group == "server":
        from .cli import server_cmd

        return server_cmd.run
    if group == "demo":
        from .cli import demo_cmd

        return {"seed": demo_cmd.seed_cmd, "reset": demo_cmd.reset_cmd, "status": demo_cmd.status}[action]
    if group == "selftest":
        from .cli import selftest_cmd

        return selftest_cmd.run
    parser_exit(_unknown(parser, args))
    return lambda _a: 2


def _unknown(parser: argparse.ArgumentParser, args: Any) -> int:
    parser.print_help()
    return 2


def parser_exit(code: int) -> None:  # pragma: no cover - defensive
    raise SystemExit(code)


if __name__ == "__main__":
    sys.exit(main())


def run_async(coro: Any) -> Any:  # small helper used by async CLI commands
    return asyncio.run(coro)

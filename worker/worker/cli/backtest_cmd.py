"""`python -m worker backtest run` — discovery or out-of-sample (M8/M10).

The out-of-sample split is ONE-SHOT: this command warns loudly and records in the audit log
what it is about to do, because after an OOS run the numbers stop being untouched.
"""

from __future__ import annotations

import argparse
import json

from .. import window
from ..config import get_settings
from ..db import conn as dbconn
from ..db import repo
from ..research.backtest import run_backtest
from ..research.jev import get_client


def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    split_name = args.split or "discovery"
    with dbconn.sync_conn() as conn:
        split = window.frozen_split(conn, settings)
        jev = get_client(settings, conn)
        warning = jev.verify()
        if warning:
            print(f"WARNING: {warning}\n")
        if split_name == "outofsample":
            print("=" * 78)
            print("OUT-OF-SAMPLE RUN — this window is meant to be touched exactly ONCE.")
            print(f"  discovery:     {split.start_date} .. {split.discovery_end} (frozen)")
            print(f"  out-of-sample: {split.discovery_end} .. {split.outofsample_end} (frozen)")
            print("  Every subsequent number from this window must be reported as 'second look'.")
            print("=" * 78)
            repo.audit(conn, "worker:backtest", "outofsample_run_started", split.as_dict())
            conn.commit()
        if not settings.jev_api_key and not settings.gm_demo_mode:
            print("NOTE: JEV_API_KEY is empty — the run will use the mock transport and its answers are NOT Jev's.\n")
        result = run_backtest(conn, settings, split_name=split_name, jev=jev, label=args.label)
        print(json.dumps(
            {
                "run_id": result.run_id,
                "split": result.split,
                "days": result.days,
                "decisions": result.decisions,
                "trades": result.trades,
                "net_bps_total": round(result.net_bps_total, 2),
                "artifact": result.artifact_path,
                "family": result.scoring.get("family"),
                "channels": [
                    {"username": c["username"], "label": c["label"], "net_eod": c["lifts_net_bps"].get("eod")}
                    for c in result.scoring.get("channels", [])
                ],
                "jev": jev.stats(),
            },
            indent=2,
            default=str,
        ))
    return 0


__all__ = ["run"]

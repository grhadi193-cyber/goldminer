"""`python -m worker report build` — Markdown + JSON report (§7.4)."""

from __future__ import annotations

import argparse
import asyncio

from ..config import REPO_ROOT, get_settings
from ..db import conn as dbconn
from ..research.backtest import load_artifact
from ..research.explainer import explain
from ..research.hypothesis import run_pending
from ..research.report import write_report
from ..window import frozen_split


def build(args: argparse.Namespace) -> int:
    settings = get_settings()
    split_name = args.split or "discovery"
    artifact = load_artifact(split_name)
    if artifact is None:
        print(f"no artifact for split {split_name!r} — run `python -m worker backtest run --split {split_name}` first")
        return 2
    with dbconn.sync_conn() as conn:
        days = frozen_split(conn, settings).days(split_name)
        tested = run_pending(conn, settings, days, limit=20)
        narrative, note = "", ""
        if args.no_narrative:
            note = "narrative disabled with --no-narrative"
        else:
            result = asyncio.run(explain(conn, settings, artifact, run_id=artifact.get("run_id")))
            narrative = result.narrative
            note = result.reason if result.skipped else f"written by {result.model}"
            if result.hypotheses:
                note += f"; {len(result.hypotheses)} new hypothesis(es) queued for coded testing"
        paths = write_report(conn, settings, artifact, split=split_name, narrative=narrative, narrative_note=note)
        print(f"markdown: {paths.markdown.relative_to(REPO_ROOT)}")
        print(f"json:     {paths.json.relative_to(REPO_ROOT)}")
        if tested:
            print(f"hypotheses tested this round: {len(tested)}")
            for row in tested:
                print(f"  #{row['hypothesis_id']}: {row.get('status')} ({row.get('test', 'unmapped')})")
    return 0


__all__ = ["build"]

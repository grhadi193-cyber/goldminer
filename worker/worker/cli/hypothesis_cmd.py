"""`python -m worker hypothesis list|test` — the coded hypothesis registry (R1d)."""

from __future__ import annotations

import argparse
import json

from ..config import get_settings
from ..db import conn as dbconn
from ..research.hypothesis import match_test, run_pending, test_hypothesis
from ..window import frozen_split


def list_hypotheses(args: argparse.Namespace) -> int:
    with dbconn.sync_conn() as conn:
        rows = conn.execute(
            """
            select h.id, h.status, h.test_method, h.proposed_by_model, h.proposed_at, h.text
            from hypotheses h order by h.id
            """
        ).fetchall()
    if not rows:
        print("no hypotheses yet — run `python -m worker report build` with EXPLAINER_MODEL set, "
              "or insert rows into `hypotheses` by hand")
        return 0
    for row in rows:
        test = match_test(row["text"])
        print(f"#{row['id']:3d} [{row['status']:9s}] {row['text'][:110]}")
        print(f"      proposed by {row['proposed_by_model'] or '—'} · test: {row['test_method'] or (test.name if test else 'unmapped')}")
    return 0


def test(args: argparse.Namespace) -> int:
    settings = get_settings()
    with dbconn.sync_conn() as conn:
        days = frozen_split(conn, settings).days("discovery")
        result = test_hypothesis(conn, settings, args.id, days)
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


def test_all(args: argparse.Namespace) -> int:
    settings = get_settings()
    with dbconn.sync_conn() as conn:
        days = frozen_split(conn, settings).days("discovery")
        rows = run_pending(conn, settings, days, limit=getattr(args, "limit", 20))
        print(json.dumps(rows, indent=2, ensure_ascii=False, default=str))
    return 0


__all__ = ["list_hypotheses", "test", "test_all"]

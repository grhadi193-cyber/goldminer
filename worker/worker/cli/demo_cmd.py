"""`python -m worker demo seed|reset` — synthetic dataset for offline pipeline validation."""

from __future__ import annotations

import argparse

from ..config import get_settings
from ..db import conn as dbconn
from ..demo import guard
from ..demo.seed import reset, seed


def seed_cmd(args: argparse.Namespace) -> int:
    settings = get_settings()
    with dbconn.sync_conn() as conn:
        out = seed(
            settings,
            conn,
            days=getattr(args, "days", None),
            seed_value=getattr(args, "seed_value", 20260401),
            with_extraction=not getattr(args, "skip_extraction", False),
            message_days=getattr(args, "message_days", None),
        )
        print("demo seed complete:")
        for key, value in out.items():
            print(f"  {key}: {value}")
        print()
        print(guard.BANNER)
    return 0


def reset_cmd(args: argparse.Namespace) -> int:
    with dbconn.sync_conn() as conn:
        counts = reset(conn)
        print("demo reset:")
        for key, value in counts.items():
            print(f"  deleted {value} row(s) from {key}")
    return 0


def status(args: argparse.Namespace) -> int:
    with dbconn.sync_conn() as conn:
        for key, value in guard.counts(conn).items():
            print(f"{key}: {value}")
        print("demo database" if guard.is_demo_run(conn) else "not a pure-demo database")
    return 0


__all__ = ["seed_cmd", "reset_cmd", "status"]

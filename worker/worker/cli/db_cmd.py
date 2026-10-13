"""`python -m worker db ...` — schema migrations."""

from __future__ import annotations

import argparse

from ..db import migrate


def up(args: argparse.Namespace) -> int:
    applied = migrate.up(until=getattr(args, "until", None))
    if applied:
        print("applied: " + ", ".join(applied))
    else:
        print("nothing to apply (schema is current)")
    return 0


def down(args: argparse.Namespace) -> int:
    rolled = migrate.down(steps=args.steps)
    if rolled:
        print("rolled back: " + ", ".join(rolled))
    else:
        print("nothing to roll back")
    return 0


def status(args: argparse.Namespace) -> int:
    for name, state in migrate.status():
        print(f"{state:8s} {name}")
    return 0

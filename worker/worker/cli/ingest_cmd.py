"""`python -m worker ingest telegram` — READ-ONLY backfill/poll (M2)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import datetime, timezone

from .. import window
from ..config import get_settings
from ..db import conn as dbconn
from ..db import repo
from ..telegram.client import TelethonReadOnlyClient
from ..telegram.ingest import poll_titles_and_messages, run_ingest

log = logging.getLogger(__name__)


def telegram(args: argparse.Namespace) -> int:
    settings = get_settings()
    split = window.compute_split(settings)
    since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc) if args.since else None
    if since is None:
        from ..timez import local_ts

        since = local_ts(split.history_start, settings.open_time)

    channels = [c.strip() for c in args.channels.split(",")] if args.channels else None
    with dbconn.sync_conn() as conn:
        client = asyncio.run(TelethonReadOnlyClient.connect(settings))
        try:
            if args.once:
                stats = asyncio.run(run_ingest(conn, settings, client, channels=channels, since=since, limit=args.limit))
                print(json.dumps(stats, indent=2))
            else:
                first = asyncio.run(run_ingest(conn, settings, client, channels=channels, since=since, limit=args.limit))
                print(json.dumps(first, indent=2))
                print("\nentering live polling loop (Ctrl-C to stop; reruns resume from checkpoints)")
                asyncio.run(poll_titles_and_messages(conn, settings, client))
        finally:
            asyncio.run(client.close())
    _ = repo
    return 0


__all__ = ["telegram"]

"""`python -m worker channels sync|list` — keep the `channels` table in sync with config/UI."""

from __future__ import annotations

import argparse

from ..config import get_settings
from ..db import conn as dbconn
from ..db import repo


def sync(args: argparse.Namespace) -> int:
    """Env channels are ADDED if missing; nothing is ever deactivated automatically (the UI
    owns the `active` flag, and silently dropping a channel would change a leaderboard)."""
    settings = get_settings()
    usernames = settings.telegram_channel_list
    if not usernames:
        print("TELEGRAM_CHANNELS is empty — nothing to sync (add channels in the UI or .env)")
        return 1
    with dbconn.sync_conn() as conn:
        existing = repo.channel_id_map(conn)
        new = [u for u in usernames if u not in existing]
        repo.upsert_channels(conn, [{"username": u} for u in usernames])
        repo.audit(conn, "worker:channels", "sync", {"new": new, "total": len(usernames)})
        print(f"channels: {len(existing)} existing, {len(new)} added {new if new else ''}")
    return 0


def list_channels(args: argparse.Namespace) -> int:
    with dbconn.sync_conn() as conn:
        rows = repo.get_channels(conn)
    if not rows:
        print("no channels registered yet")
        return 0
    print(f"{'id':>5s}  {'username':28s} {'active':6s} {'tier':16s} title")
    for r in rows:
        print(f"{r['id']:5d}  {'@' + r['username']:28s} {str(r['active']):6s} {r['tier']:16s} {r['title'] or ''}")
    return 0


__all__ = ["sync", "list_channels"]

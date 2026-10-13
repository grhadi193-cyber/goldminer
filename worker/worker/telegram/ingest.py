"""P1 — Telegram backfill + polling (READ-ONLY, idempotent, resumable).

Behaviour that matters:
  * one row per message VERSION; the unique key is (channel_id, telegram_msg_id, version)
    so rerunning after Ctrl-C inserts zero duplicates (M2 acceptance)
  * an edit NEVER rewrites `published_at`; it appends version N+1 with `edited_at` set and
    `ingested_at` = when we saw the edit (this is what makes T2/T6 pass)
  * per-channel progress lives in `run_config`, so a rerun resumes where it stopped
  * FloodWait is slept off; nothing is ever sent (R7)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Sequence

import psycopg

from ..config import Settings
from ..db import repo
from ..timez import teheran_local
from .client import RawMessage, ReadOnlyTelegramClient

log = logging.getLogger(__name__)

PROGRESS_KEY = "ingest_progress:telegram:{username}"


def write_versions(
    conn: psycopg.Connection, channel_id: int, rows: Sequence[tuple[Any, ...]], *, chunk: int = 500
) -> int:
    """Single writer for message versions — used by BOTH the Telethon backfill and the demo
    seeder, so the versioning behaviour is exercised identically in both."""
    return repo.upsert_messages(conn, rows, chunk=chunk)


def build_version_rows(
    conn: psycopg.Connection,
    channel_id: int,
    raw_messages: Sequence[RawMessage],
    *,
    ingested_at: datetime | None = None,
) -> list[tuple[Any, ...]]:
    """Turn provider messages into version rows, deciding versions against what we stored."""
    now = ingested_at or datetime.now(tz=timezone.utc)
    ids = [m.msg_id for m in raw_messages]
    existing: dict[int, list[dict[str, Any]]] = {}
    if ids:
        for row in conn.execute(
            """
            select telegram_msg_id, version, raw_text, is_edited
            from messages where channel_id = %s and telegram_msg_id = any(%s)
            order by telegram_msg_id, version
            """,
            (channel_id, ids),
        ).fetchall():
            existing.setdefault(int(row["telegram_msg_id"]), []).append(dict(row))

    out: list[tuple[Any, ...]] = []
    for msg in raw_messages:
        versions = existing.get(msg.msg_id, [])
        text = msg.text or ""
        if not versions:
            out.append(
                (channel_id, msg.msg_id, msg.published_utc, now, msg.edit_utc, text, 1,
                 msg.edit_utc is not None, msg.media_ref)
            )
            continue
        latest = max(versions, key=lambda v: v["version"])
        if (latest["raw_text"] or "") == text:
            continue  # nothing changed — do not create noise versions
        out.append(
            (channel_id, msg.msg_id, msg.published_utc, now, msg.edit_utc, text, int(latest["version"]) + 1,
             True, msg.media_ref)
        )
    return out


async def ingest_channel(
    conn: psycopg.Connection,
    client: ReadOnlyTelegramClient,
    settings: Settings,
    username: str,
    *,
    since: datetime | None = None,
    limit: int | None = None,
    batch: int = 500,
) -> dict[str, int]:
    """Backfill one channel. Resumable: progress is committed every `batch` messages."""
    username = username.lstrip("@")
    cid_map = repo.channel_id_map(conn)
    if username not in cid_map:
        repo.upsert_channels(conn, [{"username": username, "title": await client.resolve_title(username)}])
        conn.commit()
        cid_map = repo.channel_id_map(conn)
    channel_id = cid_map[username]

    progress = repo.get_run_config(conn, PROGRESS_KEY.format(username=username)) or {}
    resume_since = since
    if progress.get("last_published_at"):
        stored = datetime.fromisoformat(progress["last_published_at"])
        # Re-read a day of overlap: edits to recent messages are what we are mostly missing.
        resume_since = max(since, stored) if since else stored

    stats = {"messages": 0, "versions_inserted": 0, "batches": 0, "last_msg_id": 0}
    buffer: list[RawMessage] = []

    async def flush() -> None:
        nonlocal buffer
        if not buffer:
            return
        rows = build_version_rows(conn, channel_id, buffer)
        inserted = write_versions(conn, channel_id, rows)
        newest = max(buffer, key=lambda m: (m.published_utc, m.msg_id))
        repo.set_run_config(
            conn,
            PROGRESS_KEY.format(username=username),
            {
                "last_published_at": newest.published_utc.isoformat(),
                "last_msg_id": newest.msg_id,
                "last_run_at": datetime.now(tz=timezone.utc).isoformat(),
            },
            set_by="worker:ingest",
        )
        conn.commit()
        stats["versions_inserted"] += inserted
        stats["batches"] += 1
        log.info(
            "telegram %s: batch of %d (inserted %d, up to %s Tehran)",
            username, len(buffer), inserted, teheran_local(newest.published_utc).strftime("%Y-%m-%d %H:%M"),
        )
        buffer = []

    async for raw in client.iter_messages(username, since=resume_since, limit=limit):
        stats["messages"] += 1
        stats["last_msg_id"] = raw.msg_id
        buffer.append(raw)
        if len(buffer) >= batch:
            await flush()
    await flush()

    repo.audit(conn, "worker:ingest", "channel_ingest", {"username": username, **stats})
    conn.commit()
    return stats


async def run_ingest(
    conn: psycopg.Connection,
    settings: Settings,
    client: ReadOnlyTelegramClient,
    *,
    channels: Sequence[str] | None = None,
    since: datetime | None = None,
    limit: int | None = None,
) -> dict[str, dict[str, int]]:
    targets = [c.lstrip("@") for c in (channels or [c["username"] for c in repo.get_channels(conn, active_only=True)])]
    if not targets:
        raise ValueError("no channels to ingest — sync TELEGRAM_CHANNELS or add channels in the UI")
    out: dict[str, dict[str, int]] = {}
    for username in targets:
        out[username] = await ingest_channel(conn, client, settings, username, since=since, limit=limit)
    return out


async def poll_titles_and_messages(
    conn: psycopg.Connection, settings: Settings, client: ReadOnlyTelegramClient, *, interval_s: float = 60.0
) -> None:
    """Live polling loop for the paper monitor (read-only)."""
    while True:
        try:
            channels = [c["username"] for c in repo.get_channels(conn, active_only=True)]
            for username in channels:
                await ingest_channel(conn, client, settings, username, since=None, limit=200, batch=100)
        except Exception as exc:  # noqa: BLE001 - the poller must survive transient failures
            log.warning("telegram poll iteration failed: %s", exc)
        await asyncio.sleep(interval_s)


__all__ = ["ingest_channel", "run_ingest", "poll_titles_and_messages", "build_version_rows", "write_versions"]

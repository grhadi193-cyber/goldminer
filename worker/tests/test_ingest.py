"""M2 — Telegram ingestion: idempotency, versioning, read-only behaviour."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from worker.config import Settings
from worker.telegram.client import FakeTelegramClient, RawMessage
from worker.telegram.ingest import build_version_rows, ingest_channel, run_ingest

T0 = datetime(2026, 5, 6, 6, 0, tzinfo=timezone.utc)


def _settings(**over) -> Settings:
    base = {"gm_demo_mode": 0, "start_date": date(2026, 4, 1)}
    base.update(over)
    return Settings(**base)


def _client(messages: list[RawMessage], username: str = "chan_a") -> FakeTelegramClient:
    return FakeTelegramClient({username: messages})


async def test_ingest_then_rerun_is_idempotent(conn):
    settings = _settings()
    messages = [RawMessage(msg_id=i, published_at=T0 + timedelta(minutes=i), text=f"m{i}") for i in range(1, 21)]
    client = _client(messages)

    first = await ingest_channel(conn, client, settings, "chan_a", batch=7)
    assert first["versions_inserted"] == 20
    second = await ingest_channel(conn, client, settings, "chan_a", batch=7)
    assert second["versions_inserted"] == 0
    total = conn.execute("select count(*) as n from messages").fetchone()["n"]
    assert total == 20


async def test_edits_create_a_new_version_without_moving_event_time(conn):
    settings = _settings()
    original = RawMessage(msg_id=1, published_at=T0, text="آبشده بالا رفت")
    client = _client([original])
    await ingest_channel(conn, client, settings, "chan_a")

    edited = RawMessage(msg_id=1, published_at=T0, text="آبشده بالا رفت — ویرایش", edit_date=T0 + timedelta(minutes=30))
    await ingest_channel(conn, _client([edited]), settings, "chan_a")

    rows = conn.execute("select version, published_at, raw_text, is_edited from messages order by version").fetchall()
    assert [r["version"] for r in rows] == [1, 2]
    assert rows[0]["published_at"] == rows[1]["published_at"] == T0
    assert rows[0]["is_edited"] is False and rows[1]["is_edited"] is True
    assert rows[0]["raw_text"] != rows[1]["raw_text"]


async def test_identical_reingest_does_not_create_a_noise_version(conn):
    settings = _settings()
    msg = RawMessage(msg_id=5, published_at=T0, text="same text", edit_date=T0 + timedelta(minutes=5))
    await ingest_channel(conn, _client([msg]), settings, "chan_a")
    await ingest_channel(conn, _client([msg]), settings, "chan_a")
    assert conn.execute("select count(*) as n from messages").fetchone()["n"] == 1


async def test_progress_checkpoint_is_stored_per_channel(conn):
    settings = _settings()
    messages = [RawMessage(msg_id=i, published_at=T0 + timedelta(minutes=i), text="x") for i in range(1, 6)]
    await ingest_channel(conn, _client(messages), settings, "chan_a", batch=2)
    progress = conn.execute("select value_json from run_config where key = 'ingest_progress:telegram:chan_a'").fetchone()
    assert progress is not None
    assert progress["value_json"]["last_msg_id"] == 5


async def test_resume_uses_the_checkpoint(conn):
    settings = _settings()
    messages = [RawMessage(msg_id=i, published_at=T0 + timedelta(minutes=i), text="x") for i in range(1, 6)]
    client = _client(messages)
    await ingest_channel(conn, client, settings, "chan_a", batch=10)
    # a second pass starts from the checkpoint; the overlap re-read inserts nothing new
    stats = await ingest_channel(conn, client, settings, "chan_a", batch=10)
    assert stats["versions_inserted"] == 0


async def test_multiple_channels_and_titles(conn):
    settings = _settings()
    client = FakeTelegramClient(
        {
            "chan_a": [RawMessage(msg_id=1, published_at=T0, text="a")],
            "chan_b": [RawMessage(msg_id=1, published_at=T0, text="b")],
        }
    )
    stats = await run_ingest(conn, settings, client, channels=["chan_a", "chan_b"])
    assert set(stats) == {"chan_a", "chan_b"}
    rows = conn.execute("select username, title from channels order by username").fetchall()
    assert [r["username"] for r in rows] == ["chan_a", "chan_b"]
    assert rows[0]["title"] == "fake chan_a"


async def test_build_version_rows_skips_unchanged(conn):
    from worker.db import repo

    repo.upsert_channels(conn, [{"username": "chan_a"}])
    cid = repo.channel_id_map(conn)["chan_a"]
    conn.execute(
        "insert into messages (channel_id, telegram_msg_id, published_at, ingested_at, raw_text, version) "
        "values (%s, 7, %s, %s, 'unchanged', 1)",
        (cid, T0, T0),
    )
    rows = build_version_rows(conn, cid, [RawMessage(msg_id=7, published_at=T0, text="unchanged")])
    assert rows == []
    rows2 = build_version_rows(conn, cid, [RawMessage(msg_id=7, published_at=T0, text="changed")])
    assert len(rows2) == 1 and rows2[0][6] == 2


def test_no_send_methods_on_the_read_only_clients():
    """Belt and braces next to the source grep in test_rules.py."""
    from worker.telegram.client import FakeTelegramClient, TelethonReadOnlyClient

    for cls in (FakeTelegramClient, TelethonReadOnlyClient):
        for name in dir(cls):
            assert not name.startswith("send"), f"{cls.__name__}.{name} looks like a write API"
            assert not name.startswith("forward"), f"{cls.__name__}.{name} looks like a write API"
            assert name not in ("edit_message", "delete_messages"), f"{cls.__name__}.{name} writes"


@pytest.mark.parametrize("count", [1, 3])
async def test_small_channels_work(conn, count):
    settings = _settings()
    messages = [RawMessage(msg_id=i, published_at=T0 + timedelta(minutes=i), text="x") for i in range(count)]
    stats = await ingest_channel(conn, _client(messages), settings, "chan_a")
    assert stats["versions_inserted"] == count

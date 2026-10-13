"""T1–T6 — the anti-leakage contract (R3/R4). The build FAILS if any of these fail."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import psycopg
import pytest

from worker.config import Settings
from worker.errors import AntiLeakageError
from worker.research.asof import ReadContext, split_bounds
from worker.research.state import build_state
from worker.timez import TEHRAN, day_bounds_utc, local_ts
from worker.window import frozen_split

from .conftest import add_channel, add_extraction, add_message, add_price

DAY = date(2026, 5, 6)  # in the discovery window of START_DATE=2026-04-01


def _ctx(conn, settings: Settings, as_of: datetime, split: str = "discovery") -> ReadContext:
    return ReadContext.build(conn, settings, as_of=as_of, split_name=split)


def _checkpoint(settings: Settings) -> datetime:
    return local_ts(DAY, settings.open_time)


def test_t1_message_published_one_second_later_is_invisible(conn, settings: Settings):
    """T1: a message published at T+1s must not appear in the state built at T."""
    channel = add_channel(conn)
    t = _checkpoint(settings)
    late = add_message(conn, channel, published_at=t + timedelta(seconds=1), text="LATE", telegram_msg_id=2)
    add_extraction(conn, late, english="LEAKED-SENTINEL late message", direction="up")
    early = add_message(conn, channel, published_at=t - timedelta(minutes=1), text="EARLY", telegram_msg_id=1)
    add_extraction(conn, early, english="VISIBLE early message", direction="up")

    ctx = _ctx(conn, settings, t)
    state = build_state(ctx, DAY, checkpoint_local=settings.open_time)
    snippets = " ".join(state["signals"]["recent_signal_snippets"])
    assert "VISIBLE" in snippets
    assert "LEAKED-SENTINEL" not in snippets
    assert state["signals"]["n_messages_last_3h"] == 1


def test_t2_edited_version_is_not_visible_before_its_ingest(conn, settings: Settings):
    """T2: an edit made after T keeps the old version visible at T."""
    channel = add_channel(conn)
    t = _checkpoint(settings)
    add_message(conn, channel, published_at=t - timedelta(minutes=30), text="ORIGINAL", version=1, telegram_msg_id=7)
    add_message(
        conn, channel, published_at=t - timedelta(minutes=30), ingested_at=t + timedelta(minutes=5),
        text="EDITED", version=2, telegram_msg_id=7, is_edited=True,
    )
    rows = _ctx(conn, settings, t).messages()
    assert len(rows) == 1
    assert rows[0]["raw_text"] == "ORIGINAL"
    assert rows[0]["version"] == 1

    # ...and once the edit has been ingested, the new version wins
    later = _ctx(conn, settings, t + timedelta(minutes=10)).messages()
    assert later[0]["raw_text"] == "EDITED"
    assert later[0]["version"] == 2


def test_t3_price_bar_boundary(conn, settings: Settings):
    """T3: a bar stamped T+1m is invisible at T; a bar at T-1m is visible."""
    t = _checkpoint(settings)
    add_price(conn, "USDTIRT", t - timedelta(minutes=1), 118_000.0)
    add_price(conn, "USDTIRT", t + timedelta(minutes=1), 120_000.0)
    ctx = _ctx(conn, settings, t)
    bar = ctx.last_bar("USDTIRT", "1m", source="nobitex")
    assert bar is not None and bar["close"] == 118_000.0

    # a bar stamped exactly at T IS visible (it is the decision bar itself)
    add_price(conn, "USDTIRT", t, 119_000.0)
    assert ctx.last_bar("USDTIRT", "1m", source="nobitex")["close"] == 119_000.0

    with pytest.raises(AntiLeakageError):
        ctx.bars_between("USDTIRT", "1m", t - timedelta(hours=1), t + timedelta(minutes=1))


def test_t4_state_snapshot_is_deterministic_after_future_rows_arrive(conn, settings: Settings):
    """T4: replaying a past decision twice gives identical state hashes, even after new rows."""
    from worker.jsonutil import canonical_hash

    channel = add_channel(conn)
    t = _checkpoint(settings)
    open_ts, _ = day_bounds_utc(DAY, settings.open_time, settings.flat_time)
    add_price(conn, "USDTIRT", open_ts, 118_000.0)
    add_price(conn, "USDTIRT", t - timedelta(minutes=30), 118_400.0)
    msg = add_message(conn, channel, published_at=t - timedelta(minutes=20), text="x")
    add_extraction(conn, msg, english="gold bid up", direction="up", intensity=4)

    ctx = _ctx(conn, settings, t)
    first = canonical_hash(build_state(ctx, DAY, checkpoint_local=settings.open_time))

    # future rows arrive (after the decision instant)
    late_msg = add_message(conn, channel, published_at=t + timedelta(minutes=10), text="y", telegram_msg_id=99)
    add_extraction(conn, late_msg, english="FUTURE text", direction="down", intensity=5)
    add_price(conn, "USDTIRT", t + timedelta(minutes=10), 130_000.0)
    add_price(conn, "USDTIRT", t + timedelta(days=1), 150_000.0)

    second = canonical_hash(build_state(_ctx(conn, settings, t), DAY, checkpoint_local=settings.open_time))
    assert first == second


def test_t5_discovery_run_cannot_read_out_of_sample(conn, settings: Settings):
    """T5: split enforcement happens in the read layer, not by convention."""
    split = frozen_split(conn, settings)
    oos_day = split.discovery_end
    with pytest.raises(AntiLeakageError):
        _ctx(conn, settings, local_ts(oos_day, settings.open_time), split="discovery")

    ctx = _ctx(conn, settings, local_ts(split.start_date, settings.open_time), split="discovery")
    with pytest.raises(AntiLeakageError):
        ctx.assert_day_allowed(oos_day)

    # and the out-of-sample context cannot reach back into discovery
    with pytest.raises(AntiLeakageError):
        _ctx(conn, settings, local_ts(split.start_date, settings.open_time), split="outofsample")


def test_t6_forwarded_message_keeps_original_publish_date(conn, settings: Settings):
    """T6: a forward ingested much later still carries its original event time."""
    channel = add_channel(conn)
    t = _checkpoint(settings)
    original_publish = t - timedelta(days=3)
    forwarded = add_message(conn, channel, published_at=original_publish, ingested_at=datetime.now(tz=timezone.utc),
                            text="FORWARDED-ORIGINAL-DATE", telegram_msg_id=42)
    add_extraction(conn, forwarded, english="forwarded old news", direction="up")

    # visible at a decision time after the original publish date, even though we only saw it now
    rows = _ctx(conn, settings, t).messages()
    assert [r["message_id"] for r in rows] == [forwarded]
    # but NOT visible before its original publish date
    earlier = _ctx(conn, settings, original_publish - timedelta(hours=1)).messages()
    assert earlier == []


def test_read_context_rejects_naive_timestamps(conn, settings: Settings):
    with pytest.raises(AntiLeakageError):
        _ctx(conn, settings, _checkpoint(settings))._check(datetime(2026, 5, 6, 4, 30), "test")


def test_split_bounds_cover_the_intended_days(conn, settings: Settings):
    split = frozen_split(conn, settings)
    start, end = split_bounds(split, "discovery", settings)
    assert start == local_ts(split.start_date, settings.open_time)
    _, disco_end = day_bounds_utc(split.discovery_end - timedelta(days=1), settings.open_time, settings.flat_time)
    assert end == disco_end
    oos_start, oos_end = split_bounds(split, "outofsample", settings)
    assert oos_start == local_ts(split.discovery_end, settings.open_time)
    assert oos_end > oos_start
    _ = psycopg, TEHRAN

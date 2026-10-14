"""Repository helpers: idempotent upserts + time-bounded reads.

Every read that a replay/decision could consume takes an explicit `as_of` (UTC) and filters
on the EVENT time column (`published_at` / `ts`), never on `ingested_at`, except for the one
deliberate exception documented in `messages_asof`: an edited message version only becomes
visible once the edit itself was ingested.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime
from typing import Any

import psycopg

from ..jsonutil import jsonb, safe_json


def audit(conn: psycopg.Connection | psycopg.AsyncConnection, actor: str, event: str, detail: Any = None) -> None:
    conn.execute(
        "insert into audit_log (actor, event, detail_json) values (%s, %s, %s)",
        (actor, event, jsonb(detail) if detail is not None else None),
    )


# --------------------------------------------------------------------------- config

def get_run_config(conn: psycopg.Connection, key: str) -> Any | None:
    row = conn.execute("select value_json from run_config where key = %s", (key,)).fetchone()
    return row["value_json"] if row else None


def set_run_config(
    conn: psycopg.Connection, key: str, value: Any, *, set_by: str = "system", freeze: bool = False
) -> None:
    """Write a config row. `freeze=True` refuses to overwrite an existing value (used for the
    one-shot out-of-sample split definition, R4)."""
    if freeze:
        existing = get_run_config(conn, key)
        if existing is not None and existing != value:
            raise ValueError(f"run_config[{key!r}] is frozen at {existing!r}; refusing to overwrite with {value!r}")
    conn.execute(
        """
        insert into run_config (key, value_json, set_at, set_by) values (%s, %s, now(), %s)
        on conflict (key) do update set value_json = excluded.value_json, set_at = now(), set_by = excluded.set_by
        """,
        (key, jsonb(value), set_by),
    )


# --------------------------------------------------------------------------- channels

def upsert_channels(conn: psycopg.Connection, channels: Iterable[dict[str, Any]]) -> int:
    n = 0
    for ch in channels:
        conn.execute(
            """
            insert into channels (username, title, active, notes)
            values (%s, %s, %s, %s)
            on conflict (username) do update
              set title = coalesce(excluded.title, channels.title),
                  notes = coalesce(excluded.notes, channels.notes)
            """,
            (ch["username"].lstrip("@"), ch.get("title"), ch.get("active", True), ch.get("notes")),
        )
        n += 1
    return n


def get_channels(conn: psycopg.Connection, *, active_only: bool = False) -> list[dict[str, Any]]:
    sql = "select * from channels"
    if active_only:
        sql += " where active"
    sql += " order by username"
    return list(conn.execute(sql).fetchall())


def channel_id_map(conn: psycopg.Connection) -> dict[str, int]:
    return {r["username"]: r["id"] for r in conn.execute("select id, username from channels").fetchall()}


# --------------------------------------------------------------------------- messages

_MESSAGE_UPSERT = """
insert into messages
    (channel_id, telegram_msg_id, published_at, ingested_at, edited_at, raw_text, version, is_edited, media_ref)
values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
on conflict (channel_id, telegram_msg_id, version) do nothing
returning id
"""


def upsert_messages(conn: psycopg.Connection, rows: Sequence[tuple[Any, ...]], *, chunk: int = 500) -> int:
    """Insert message versions, ignoring duplicates. Returns the number of NEW rows.

    Idempotent by construction: the unique key is (channel_id, telegram_msg_id, version) and
    the conflict action is DO NOTHING, so a rerun after Ctrl-C inserts zero duplicates (M2).
    """
    inserted = 0
    cur = conn.cursor()
    for start in range(0, len(rows), chunk):
        batch = rows[start : start + chunk]
        cur.executemany(_MESSAGE_UPSERT, batch, returning=True)
        while True:
            try:
                inserted += len(cur.fetchall())
            except psycopg.ProgrammingError:
                pass  # this batch item conflicted and returned no rows
            if not cur.nextset():
                break
    return inserted


def message_count_by_channel_month(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return list(
        conn.execute(
            """
            select c.username,
                   date_trunc('month', m.published_at at time zone 'UTC')::date as month,
                   count(*) as n_messages,
                   count(*) filter (where m.is_edited) as n_edited
            from messages m join channels c on c.id = m.channel_id
            group by 1, 2
            order by 1, 2
            """
        ).fetchall()
    )


def asof_messages(
    conn: psycopg.Connection,
    as_of: datetime,
    *,
    channel_ids: Sequence[int] | None = None,
    since: datetime | None = None,
    with_extractions: bool = True,
) -> list[dict[str, Any]]:
    """Messages visible at `as_of` (R3) — see db/migrations/0003_asof.sql for the rule."""
    sql = [
        "select v.id as message_id, v.channel_id, c.username, v.telegram_msg_id, v.published_at,",
        "       v.raw_text, v.version",
    ]
    if with_extractions:
        sql.append("       , e.extraction_json, e.status as extraction_status, e.model as extraction_model")
    sql.append("from messages_asof(%s) v join channels c on c.id = v.channel_id")
    if with_extractions:
        sql.append("left join extractions e on e.message_id = v.id")
    params: list[Any] = [as_of]
    if channel_ids:
        sql.append("where v.channel_id = any(%s)")
        params.append(list(channel_ids))
    else:
        sql.append("where true")
    if since is not None:
        sql.append("and v.published_at >= %s")
        params.append(since)
    sql.append("order by v.published_at")
    return list(conn.execute("\n".join(sql), params).fetchall())


# --------------------------------------------------------------------------- prices

_PRICE_UPSERT = """
insert into price_bars (source, symbol, ts, resolution, open, high, low, close, volume)
values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
on conflict (source, symbol, resolution, ts) do update
   set open = excluded.open, high = excluded.high, low = excluded.low,
       close = excluded.close, volume = excluded.volume
"""


def upsert_price_bars(conn: psycopg.Connection, rows: Sequence[tuple[Any, ...]], *, chunk: int = 2000) -> int:
    """Idempotent price upsert (same key as the unique index). Returns rows written."""
    cur = conn.cursor()
    written = 0
    for start in range(0, len(rows), chunk):
        batch = rows[start : start + chunk]
        cur.executemany(_PRICE_UPSERT, batch)
        written += len(batch)
    return written


def record_gap(
    conn: psycopg.Connection, source: str, symbol: str, from_ts: datetime, to_ts: datetime, reason: str
) -> None:
    conn.execute(
        """
        insert into price_gaps (source, symbol, from_ts, to_ts, reason)
        values (%s, %s, %s, %s, %s)
        on conflict (source, symbol, from_ts, to_ts, reason) do nothing
        """,
        (source, symbol, from_ts, to_ts, reason),
    )


def last_bar_at_or_before(
    conn: psycopg.Connection | psycopg.AsyncConnection,
    source: str | None,
    symbol: str,
    resolution: str,
    as_of: datetime,
) -> dict[str, Any] | None:
    """Last bar with ts <= as_of. STRICT bound: a bar stamped exactly at `as_of` counts
    (it is the decision bar) but anything later is invisible (T3)."""
    sql = """
        select ts, open, high, low, close, volume, source, resolution
        from price_bars
        where symbol = %s and resolution = %s and ts <= %s
    """
    params: list[Any] = [symbol, resolution, as_of]
    if source:
        sql += " and source = %s"
        params.append(source)
    sql += " order by ts desc limit 1"
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def first_bar_at_or_after(
    conn: psycopg.Connection | psycopg.AsyncConnection,
    source: str | None,
    symbol: str,
    resolution: str,
    from_ts: datetime,
    *,
    within_minutes: int | None = None,
    until: datetime | None = None,
) -> dict[str, Any] | None:
    sql = """
        select ts, open, high, low, close, volume, source, resolution
        from price_bars
        where symbol = %s and resolution = %s and ts >= %s
    """
    params: list[Any] = [symbol, resolution, from_ts]
    if source:
        sql += " and source = %s"
        params.append(source)
    if until is not None:
        sql += " and ts <= %s"
        params.append(until)
    sql += " order by ts asc limit 1"
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def bars_between(
    conn: psycopg.Connection | psycopg.AsyncConnection,
    symbol: str,
    resolution: str,
    start: datetime,
    end: datetime,
    *,
    source: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    sql = """
        select ts, open, high, low, close, volume, source
        from price_bars
        where symbol = %s and resolution = %s and ts >= %s and ts <= %s
    """
    params: list[Any] = [symbol, resolution, start, end]
    if source:
        sql += " and source = %s"
        params.append(source)
    sql += " order by ts asc"
    if limit:
        sql += f" limit {int(limit)}"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def bar_coverage(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("select * from price_coverage_monthly order by source, symbol, resolution, month").fetchall()]


def distinct_series(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            """
            select source, symbol, resolution, count(*) as n_bars, min(ts) as first_ts, max(ts) as last_ts
            from price_bars group by 1,2,3 order by 1,2,3
            """
        ).fetchall()
    ]


# --------------------------------------------------------------------------- outcomes

def upsert_day_outcome(
    conn: psycopg.Connection,
    *,
    day_date: date,
    decision_price: float,
    eod_price: float,
    realized_bps: float,
    label: str,
    series: str = "gold_18k_toman_per_gram",
) -> None:
    conn.execute(
        """
        insert into day_outcomes (day_date, decision_price, eod_price, realized_bps, label, series)
        values (%s, %s, %s, %s, %s, %s)
        on conflict (day_date) do update
          set decision_price = excluded.decision_price,
              eod_price = excluded.eod_price,
              realized_bps = excluded.realized_bps,
              label = excluded.label,
              series = excluded.series,
              computed_at = now()
        """,
        (day_date, decision_price, eod_price, realized_bps, label, series),
    )


def day_outcomes_range(conn: psycopg.Connection, start: date, end: date) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "select * from day_outcomes where day_date >= %s and day_date <= %s order by day_date",
            (start, end),
        ).fetchall()
    ]

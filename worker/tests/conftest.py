"""Test fixtures.

A real PostgreSQL is used on purpose: the anti-leakage rule (R3) lives in SQL
(`messages_asof`), so testing it against sqlite would test nothing.

Resolution order for the database:
  1. `PGTEST_URI` environment variable (CI service container or a local server)
  2. `pgserver` (bundled Postgres binaries) started in a temp directory
  3. skip the postgres-marked tests with a clear message

Every test gets a freshly truncated schema; migrations run once per session.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time as _time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import psycopg
import psycopg.types.json
import pytest
from psycopg.rows import dict_row

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_SRC = REPO_ROOT / "worker"

from worker.config import Settings  # noqa: E402
from worker.db import migrate  # noqa: E402

TABLES = [
    "audit_log", "run_config", "hypotheses", "paper_equity", "paper_orders", "paper_positions",
    "day_outcomes", "backtest_trades", "backtest_runs", "jev_decisions", "jev_cache",
    "channel_scores", "llm_cache", "price_gaps", "price_bars", "extractions", "messages", "channels",
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class PostgresHarness:
    def __init__(self) -> None:
        self.uri = os.environ.get("PGTEST_URI", "")
        self.process: subprocess.Popen | None = None
        self.datadir = Path("/tmp/goldminer-test-pg")

    def start(self) -> str:
        if self.uri:
            return self.uri
        import pgserver

        bin_dir = Path(pgserver.__file__).parent / "pginstall" / "bin"
        if not (bin_dir / "initdb").exists():  # pragma: no cover - environment dependent
            raise RuntimeError("pgserver binaries missing")
        if not (self.datadir / "PG_VERSION").exists():
            subprocess.run(
                [str(bin_dir / "initdb"), "-D", str(self.datadir), "-U", "postgres", "--auth=trust", "-E", "UTF8"],
                check=True, capture_output=True,
            )
        port = _free_port()
        self.process = subprocess.Popen(
            [str(bin_dir / "postgres"), "-D", str(self.datadir), "-p", str(port), "-k", "/tmp",
             "-c", "listen_addresses=127.0.0.1", "-c", "fsync=off", "-c", "synchronous_commit=off"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = _time.time() + 30
        while _time.time() < deadline:
            try:
                with psycopg.connect(f"postgresql://postgres@127.0.0.1:{port}/postgres", connect_timeout=2):
                    break
            except psycopg.OperationalError:
                _time.sleep(0.2)
        else:  # pragma: no cover
            raise RuntimeError("test postgres did not start")
        self.uri = f"postgresql://postgres@127.0.0.1:{port}/postgres"
        return self.uri

    def stop(self) -> None:
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                self.process.kill()


@pytest.fixture(scope="session")
def harness() -> PostgresHarness:
    h = PostgresHarness()
    h.start()
    migrate.up(h.uri)
    yield h
    h.stop()


@pytest.fixture(scope="session")
def pg_uri(harness: PostgresHarness) -> str:
    return harness.uri


@pytest.fixture()
def conn(pg_uri: str):
    connection = psycopg.connect(pg_uri, autocommit=True, row_factory=dict_row)
    connection.execute("set session time zone 'UTC'")
    for table in TABLES:
        connection.execute(f"truncate table {table} restart identity cascade")
    yield connection
    connection.close()


@pytest.fixture()
def settings(pg_uri: str) -> Settings:
    return Settings(
        database_url=pg_uri,
        gm_demo_mode=1,
        start_date=date(2026, 4, 1),
        discovery_months=4,
        history_months=6,
        jev_base_url="https://api.typesafe.ai",
        jev_allowed_hosts="api.typesafe.ai",
        jev_api_key="",
    )


# ------------------------------------------------------------------ small builders

def add_channel(conn, username: str = "chan_a", *, active: bool = True) -> int:
    row = conn.execute(
        "insert into channels (username, title, active) values (%s, %s, %s) returning id",
        (username, username, active),
    ).fetchone()
    return int(row["id"])


def add_message(
    conn,
    channel_id: int,
    *,
    published_at: datetime,
    ingested_at: datetime | None = None,
    text: str = "آبشده ۱۸ عیار بالا رفت",
    version: int = 1,
    telegram_msg_id: int = 1,
    is_edited: bool = False,
) -> int:
    row = conn.execute(
        """
        insert into messages (channel_id, telegram_msg_id, published_at, ingested_at, raw_text, version, is_edited)
        values (%s, %s, %s, %s, %s, %s, %s) returning id
        """,
        (channel_id, telegram_msg_id, published_at, ingested_at or published_at, text, version, is_edited),
    ).fetchone()
    return int(row["id"])


def add_extraction(
    conn,
    message_id: int,
    *,
    english: str,
    direction: str = "up",
    intensity: int = 3,
    kind: str = "price_quote",
    confidence: float = 0.7,
    quoted_price: float | None = None,
    quoted_asset: str | None = None,
    model: str = "test-model",
    status: str = "ok",
) -> None:
    payload = {
        "topic": "test",
        "direction": direction,
        "kind": kind,
        "intensity": intensity,
        "quoted_price": quoted_price,
        "quoted_asset": quoted_asset,
        "confidence": confidence,
        "english_text": english,
        "persian_gloss": None,
    }
    conn.execute(
        """
        insert into extractions (message_id, model, extraction_json, status, attempts, cache_key)
        values (%s, %s, %s, %s, 1, %s)
        """,
        (message_id, model, psycopg.types.json.Jsonb(payload) if status == "ok" else None, status,
         f"test-{message_id}"),
    )


def add_price(
    conn,
    symbol: str,
    ts: datetime,
    close: float,
    *,
    source: str = "nobitex",
    resolution: str = "1m",
    high: float | None = None,
    low: float | None = None,
) -> None:
    conn.execute(
        """
        insert into price_bars (source, symbol, ts, resolution, open, high, low, close, volume)
        values (%s, %s, %s, %s, %s, %s, %s, %s, null)
        on conflict (source, symbol, resolution, ts) do update set close = excluded.close
        """,
        (source, symbol, ts, resolution, close, high if high is not None else close,
         low if low is not None else close, close),
    )


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)  # type: ignore[arg-type]


def minutes(n: int) -> timedelta:
    return timedelta(minutes=n)

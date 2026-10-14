"""Connection helpers. Sqlite-free: Postgres only (psycopg3)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

import psycopg
from psycopg.rows import dict_row

from ..config import get_settings


@contextmanager
def sync_conn(url: str | None = None, *, autocommit: bool = False) -> Iterator[psycopg.Connection]:
    settings = get_settings()
    conn = psycopg.connect(url or settings.db_url, autocommit=autocommit, row_factory=dict_row)
    try:
        yield conn
        if not autocommit:
            conn.commit()
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


@asynccontextmanager
async def async_conn(url: str | None = None, *, autocommit: bool = False) -> AsyncIterator[psycopg.AsyncConnection]:
    settings = get_settings()
    conn = await psycopg.AsyncConnection.connect(
        url or settings.db_url, autocommit=autocommit, row_factory=dict_row
    )
    try:
        yield conn
        if not autocommit:
            await conn.commit()
    except Exception:
        if not autocommit:
            await conn.rollback()
        raise
    finally:
        await conn.close()


def ping(url: str | None = None) -> str:
    with sync_conn(url) as conn:
        row = conn.execute("select version()").fetchone()
    return str(row["version"]) if row else "unknown"

"""Tiny forward-only migration runner (deliberately boring — no alembic).

Each migration file has two required sections:

    -- migrate:up
    ...statements...
    -- migrate:down
    ...statements...

`up` applies every not-yet-applied file in version order inside its own transaction and
records it in `schema_migrations`. `down --steps N` rolls back the last N. Ctrl-C at any
point leaves the database in a consistent state (one file = one transaction).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from ..config import REPO_ROOT, get_settings

MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"

LEDGER_DDL = """
create table if not exists schema_migrations (
    version     text primary key,
    name        text not null,
    checksum    text not null,
    applied_at  timestamptz not null default now()
);
"""


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    path: Path
    up: str
    down: str
    checksum: str


def _parse(path: Path) -> Migration:
    text = path.read_text(encoding="utf-8")
    if "-- migrate:up" not in text or "-- migrate:down" not in text:
        raise ValueError(f"{path} must contain '-- migrate:up' and '-- migrate:down' markers")
    up_part = text.split("-- migrate:up", 1)[1].split("-- migrate:down", 1)[0]
    down_part = text.split("-- migrate:down", 1)[1]
    version = path.name.split("_", 1)[0]
    return Migration(
        version=version,
        name=path.name,
        path=path,
        up=up_part.strip(),
        down=down_part.strip(),
        checksum=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def discover(migrations_dir: Path | None = None) -> list[Migration]:
    directory = migrations_dir or MIGRATIONS_DIR
    files = sorted(directory.glob("*.sql"))
    return [_parse(p) for p in files]


def _applied(conn: psycopg.Connection) -> dict[str, str]:
    conn.execute(LEDGER_DDL)
    conn.commit()
    rows = conn.execute("select version, checksum from schema_migrations").fetchall()
    return {r["version"]: r["checksum"] for r in rows}


def up(url: str | None = None, *, until: str | None = None, migrations_dir: Path | None = None) -> list[str]:
    settings = get_settings()
    applied_now: list[str] = []
    with psycopg.connect(url or settings.migration_db_url, autocommit=True, row_factory=dict_row) as conn:
        applied = _applied(conn)
        for mig in discover(migrations_dir):
            if mig.version in applied:
                if applied[mig.version] != mig.checksum:
                    raise RuntimeError(
                        f"Migration {mig.name} was already applied but its contents changed. "
                        "Migrations are forward-only: add a new file instead of editing history."
                    )
                continue
            if until and mig.version > until:
                break
            try:
                with conn.transaction():
                    conn.execute(mig.up)
                    conn.execute(
                        "insert into schema_migrations (version, name, checksum) values (%s, %s, %s)",
                        (mig.version, mig.name, mig.checksum),
                    )
            except psycopg.Error as exc:  # pragma: no cover - operational
                raise RuntimeError(f"Migration {mig.name} failed: {exc}") from exc
            applied_now.append(mig.name)
    return applied_now


def down(url: str | None = None, *, steps: int = 1, migrations_dir: Path | None = None) -> list[str]:
    settings = get_settings()
    rolled: list[str] = []
    with psycopg.connect(url or settings.migration_db_url, autocommit=True, row_factory=dict_row) as conn:
        applied = _applied(conn)
        done = [m for m in discover(migrations_dir) if m.version in applied]
        for mig in reversed(done[-steps:] if steps > 0 else []):
            with conn.transaction():
                conn.execute(mig.down)
                conn.execute("delete from schema_migrations where version = %s", (mig.version,))
            rolled.append(mig.name)
    return rolled


def status(url: str | None = None, *, migrations_dir: Path | None = None) -> list[tuple[str, str]]:
    settings = get_settings()
    with psycopg.connect(url or settings.migration_db_url, autocommit=True, row_factory=dict_row) as conn:
        applied = _applied(conn)
    return [(m.name, "applied" if m.version in applied else "pending") for m in discover(migrations_dir)]

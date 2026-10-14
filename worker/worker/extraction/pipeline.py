"""Extraction pipeline: chunked, resumable, cached, never retried more than twice (R1a/R9)."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import psycopg

from ..config import Settings
from ..db import repo
from ..jsonutil import jsonb, safe_json, sha256_hex
from .provider import ExtractionProvider, ExtractionResult, cache_key_for, get_provider
from .schema import ExtractionRecord

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 2  # initial + one stricter retry (R1a)


@dataclass
class ExtractionStats:
    processed: int = 0
    ok: int = 0
    failed: int = 0
    cache_hits: int = 0
    empty_text: int = 0
    model_counts: Counter[str] = field(default_factory=Counter)
    failure_reasons: Counter[str] = field(default_factory=Counter)

    @property
    def success_rate(self) -> float:
        denom = self.ok + self.failed
        return (self.ok / denom) if denom else 0.0

    @property
    def cache_hit_rate(self) -> float:
        return (self.cache_hits / self.processed) if self.processed else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "processed": self.processed,
            "ok": self.ok,
            "failed": self.failed,
            "cache_hits": self.cache_hits,
            "empty_text": self.empty_text,
            "success_rate": round(self.success_rate, 4),
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "models": dict(self.model_counts),
            "failure_reasons": dict(self.failure_reasons),
        }


def pending_messages(
    conn: psycopg.Connection, *, limit: int | None = None, retry_failed: bool = False
) -> list[dict[str, Any]]:
    """Messages with no extraction row (or, optionally, failed ones under the attempt cap)."""
    sql = """
        select m.id as message_id, m.raw_text, m.published_at, c.username, e.status, e.attempts
        from messages m
        join channels c on c.id = m.channel_id
        left join extractions e on e.message_id = m.id
        where m.raw_text is not null and length(btrim(m.raw_text)) > 0
    """
    if retry_failed:
        sql += f" and (e.message_id is null or (e.status = 'failed' and coalesce(e.attempts, 0) < {MAX_ATTEMPTS}))"
    else:
        sql += " and e.message_id is null"
    sql += " order by m.published_at asc"
    if limit:
        sql += f" limit {int(limit)}"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def _cache_get(conn: psycopg.Connection, cache_key: str) -> dict[str, Any] | None:
    row = conn.execute(
        "select cache_key, model, status, response_json, raw_response, reason from llm_cache where cache_key = %s",
        (cache_key,),
    ).fetchone()
    return dict(row) if row else None


def _cache_put(conn: psycopg.Connection, key: str, model: str, result: ExtractionResult) -> None:
    conn.execute(
        """
        insert into llm_cache (cache_key, model, status, response_json, raw_response, reason)
        values (%s, %s, %s, %s, %s, %s)
        on conflict (cache_key) do nothing
        """,
        (
            key,
            model,
            result.status,
            jsonb(result.record.model_dump()) if result.record else None,
            result.raw[:20000] if result.raw else None,
            result.reason,
        ),
    )


def _cache_bump(conn: psycopg.Connection, key: str) -> None:
    conn.execute("update llm_cache set hits = hits + 1 where cache_key = %s", (key,))


def _write_extraction(
    conn: psycopg.Connection,
    *,
    message_id: int,
    model: str,
    record: ExtractionRecord | None,
    status: str,
    reason: str | None,
    cache_key: str,
    attempts: int,
) -> None:
    conn.execute(
        """
        insert into extractions (message_id, model, extraction_json, status, status_reason, attempts, cache_key, extracted_at, computed_at)
        values (%s, %s, %s, %s, %s, %s, %s, now(), now())
        on conflict (message_id) do update
          set model = excluded.model,
              extraction_json = excluded.extraction_json,
              status = excluded.status,
              status_reason = excluded.status_reason,
              attempts = excluded.attempts,
              cache_key = excluded.cache_key,
              extracted_at = now(),
              computed_at = now()
        """,
        (
            message_id,
            model,
            jsonb(record.model_dump()) if record else None,
            status,
            reason,
            attempts,
            cache_key,
        ),
    )


async def run_extraction(
    conn: psycopg.Connection,
    settings: Settings,
    *,
    limit: int | None = None,
    batch: int = 200,
    provider: ExtractionProvider | None = None,
    retry_failed: bool = False,
    commit_every: int = 50,
    progress: bool = True,
) -> ExtractionStats:
    """Process the backlog in chunks. Ctrl-C loses at most `commit_every` messages."""
    stats = ExtractionStats()
    prov = provider or get_provider(settings)
    models = settings.extraction_model_list or ["mock-extractor-v1"]
    semaphore = asyncio.Semaphore(max(1, settings.jev_max_concurrency + 1))

    todo = pending_messages(conn, limit=limit, retry_failed=retry_failed)
    log.info("extraction backlog: %d message(s)", len(todo))
    for start in range(0, len(todo), batch):
        chunk = todo[start : start + batch]

        async def handle(row: dict[str, Any]) -> tuple[dict[str, Any], ExtractionResult, bool]:
            text = (row["raw_text"] or "").strip()
            if not text:
                return row, ExtractionResult(record=None, model="", raw="", status="failed", reason="empty_text"), False
            # 1) cache lookup by (text, model) for EVERY candidate model
            for model in models:
                key = cache_key_for(text, model)
                hit = _cache_get(conn, key)
                if hit and hit["status"] == "ok" and hit["response_json"]:
                    _cache_bump(conn, key)
                    record = ExtractionRecord.model_validate(hit["response_json"])
                    return row, ExtractionResult(
                        record=record, model=hit["model"], raw="", status="ok", cache_key=key, attempts=0
                    ), True
            # 2) real call
            async with semaphore:
                result = await prov.extract(
                    row["raw_text"], channel=row["username"], published_at=row["published_at"].isoformat()
                )
            return row, result, False

        results = await asyncio.gather(*(handle(row) for row in chunk))
        for row, result, cached in results:
            stats.processed += 1
            if cached:
                stats.cache_hits += 1
            if result.status == "ok" and result.record is not None:
                stats.ok += 1
                stats.model_counts[result.model] += 1
                if not cached:
                    _cache_put(conn, result.cache_key, result.model, result)
            else:
                stats.failed += 1
                if result.reason == "empty_text":
                    stats.empty_text += 1
                stats.failure_reasons[(result.reason or "unknown").split(":")[0]] += 1
            _write_extraction(
                conn,
                message_id=row["message_id"],
                model=result.model or "none",
                record=result.record,
                status=result.status,
                reason=result.reason,
                cache_key=result.cache_key or sha256_hex(row["raw_text"] or ""),
                attempts=result.attempts,
            )
        conn.commit()
        if progress:
            log.info(
                "extraction %d/%d processed (ok=%d failed=%d cache_hits=%d)",
                min(start + batch, len(todo)), len(todo), stats.ok, stats.failed, stats.cache_hits,
            )
    repo.audit(conn, "worker:extract", "extraction_run", stats.as_dict())
    conn.commit()
    return stats


def extraction_summary(conn: psycopg.Connection) -> dict[str, Any]:
    """`python -m worker extract stats` — what is done, what failed, cache hit rate."""
    row = conn.execute(
        """
        select count(*) filter (where e.status = 'ok') as ok,
               count(*) filter (where e.status = 'failed') as failed,
               (select count(*) from messages m2 where m2.raw_text is not null and length(btrim(m2.raw_text)) > 0) as total_messages,
               count(*) filter (where e.status = 'failed' and coalesce(e.attempts,0) >= %s) as failed_final
        from extractions e
        """,
        (MAX_ATTEMPTS,),
    ).fetchone()
    models = conn.execute("select model, count(*) as n from extractions group by 1 order by 2 desc").fetchall()
    cache = conn.execute("select coalesce(sum(hits),0) as hits, count(*) as entries from llm_cache").fetchone()
    per_channel = conn.execute(
        """
        select c.username,
               count(*) as n,
               count(*) filter (where e.status = 'ok') as ok,
               count(*) filter (where e.status = 'failed') as failed
        from messages m
        join channels c on c.id = m.channel_id
        left join extractions e on e.message_id = m.id
        group by 1 order by 1
        """
    ).fetchall()
    total = int(row["total_messages"] or 0)
    ok = int(row["ok"] or 0)
    return {
        "total_messages_with_text": total,
        "extractions_ok": ok,
        "extractions_failed": int(row["failed"] or 0),
        "failed_after_retries": int(row["failed_final"] or 0),
        "success_rate": round(ok / total, 4) if total else 0.0,
        "models": {r["model"]: int(r["n"]) for r in models},
        "llm_cache": {"entries": int(cache["entries"] or 0), "hits": int(cache["hits"] or 0)},
        "per_channel": [dict(r) for r in per_channel],
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }


__all__ = ["ExtractionStats", "run_extraction", "pending_messages", "extraction_summary", "MAX_ATTEMPTS"]

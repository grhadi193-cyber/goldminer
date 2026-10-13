"""`python -m worker extract run|stats|selftest` (M3)."""

from __future__ import annotations

import argparse
import asyncio
import json

from ..config import get_settings
from ..db import conn as dbconn
from ..extraction.pipeline import extraction_summary, run_extraction
from ..extraction.provider import MockProvider, get_provider
from ..extraction.schema import ExtractionValidationError, parse_extraction


def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    with dbconn.sync_conn() as conn:
        provider = get_provider(settings)
        stats = asyncio.run(
            run_extraction(
                conn,
                settings,
                limit=args.limit,
                batch=args.batch,
                provider=provider,
                retry_failed=True,
            )
        )
        print(json.dumps(stats.as_dict(), indent=2, ensure_ascii=False))
    return 0 if stats.failed == 0 or stats.ok > 0 else 1


def stats(args: argparse.Namespace) -> int:
    with dbconn.sync_conn() as conn:
        print(json.dumps(extraction_summary(conn), indent=2, ensure_ascii=False))
    return 0


def selftest(args: argparse.Namespace) -> int:
    """Offline check of the schema validator: right shape accepted, wrong shapes rejected."""
    good = '{"topic":"coin premium","direction":"up","kind":"price_quote","intensity":4,' \
           '"quoted_price":152000000,"quoted_asset":"emami_coin","confidence":0.8,' \
           '"english_text":"Emami coin quoted at 152,000,000 toman","persian_gloss":null}'
    bad_cases = {
        "direction enum": good.replace('"up"', '"higher"'),
        "intensity range": good.replace('"intensity":4', '"intensity":9'),
        "missing key": good.replace('"persian_gloss":null', '"gloss":null'),
        "not json": "I think the market will go up.",
    }
    parse_extraction(good)
    print("[ok  ] valid record accepted")
    failures = 0
    for name, payload in bad_cases.items():
        try:
            parse_extraction(payload)
        except ExtractionValidationError:
            print(f"[ok  ] rejected: {name}")
        else:
            print(f"[FAIL] accepted invalid payload: {name}")
            failures += 1
    provider = MockProvider()
    result = asyncio.run(provider.extract("آبشده ۱۸ عیار الان ۹,۴۵۰,۰۰۰ تومان معامله می‌شود", channel="demo"))
    print(f"[ok  ] mock provider: {result.record.model_dump_json() if result.record else result.reason}")
    return 1 if failures else 0


__all__ = ["run", "stats", "selftest"]

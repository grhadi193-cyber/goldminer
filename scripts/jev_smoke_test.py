#!/usr/bin/env python
"""Jev smoke test — must pass before ANY real Jev call is trusted (R2, checkpoint b).

What it does, in order:
  1. prints the resolved provider, endpoint and model (never the key), and runs the host
     allowlist check — an unlisted host fails LOUDLY here, before anything is sent
  2. prints the third-party warning when the host is not api.typesafe.ai
  3. sends the FIXED five-question tree over a synthetic state (the same tree the replay uses)
  4. prints the raw response payload, so the operator can confirm the field names the parser
     tolerates, and then the parsed answers
  5. writes docs/jev_smoke_test_output.md for the record

Usage:
  python scripts/jev_smoke_test.py              # real call (needs JEV_API_KEY)
  python scripts/jev_smoke_test.py --mock       # mock transport, no network, no key
  python scripts/jev_smoke_test.py --provider opencode   # override just for this run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "worker"))

from worker.config import JEV_PROVIDER_PRESETS, OFFICIAL_JEV_HOST, get_settings, reset_settings_cache  # noqa: E402
from worker.errors import ConfigError, JevHostNotAllowed, JevRequestError, JevUnavailable  # noqa: E402
from worker.jsonutil import canonical_json  # noqa: E402
from worker.research.jev import JevClient, MockTransport  # noqa: E402
from worker.research.question_tree import build_questions  # noqa: E402

STATE = {
    "day": {"date": "2026-05-06", "session": "08:00-22:00 Tehran"},
    "market": {
        "gold_18k_toman_per_gram": 9_450_000,
        "usdtirt_toman": 121_500,
        "xauusd_usd_oz": 3_310.5,
        "gold_spread_bps": 30,
    },
    "history": {"gold_eod_vs_08_bps_yesterday": 42.0, "flat_band_bps": 15.0},
    "signals": {
        "n_messages_last_3h": 11,
        "n_extracted_last_3h": 11,
        "n_channels_active": 4,
        "mean_intensity": 2.7,
        "kind_counts": {"news": 3, "rumor": 2, "opinion": 4, "price_quote": 2},
        "bullish_minus_bearish_count": {"last_30m": 1, "last_1h": 3, "last_3h": 4},
        "recent_signal_snippets": [
            "[hojoom_tracker @ 11:44Z] Report about abshodeh leaning UP; quoted price 10,477,000 toman",
            "[coin_watch @ 11:52Z] Emami coin trading 10 bps above the bazaar mid",
        ],
    },
    "note": "SMOKE TEST STATE — synthetic, not a market snapshot",
}


def _settings_with_overrides(args: argparse.Namespace):
    if args.provider:
        os.environ["JEV_PROVIDER"] = args.provider
    reset_settings_cache()
    return get_settings()


async def main() -> int:
    parser = argparse.ArgumentParser(description="Jev smoke test")
    parser.add_argument("--mock", action="store_true", help="use the mock transport (no key, no network)")
    parser.add_argument("--provider", choices=sorted(JEV_PROVIDER_PRESETS), default=None)
    parser.add_argument("--model", default=None, help="override JEV_MODEL for this run")
    parser.add_argument("--out", default=str(REPO_ROOT / "docs" / "jev_smoke_test_output.md"))
    args = parser.parse_args()

    settings = _settings_with_overrides(args)
    model = args.model or settings.jev_model_resolved
    lines: list[str] = []

    def say(text: str = "") -> None:
        print(text)
        lines.append(text)

    say(f"# Jev smoke test — {datetime.now(tz=timezone.utc).isoformat(timespec='seconds')}")
    say()
    say(f"- provider: `{settings.jev_provider_resolved}`")
    say(f"- endpoint: `{settings.jev_endpoint}`")
    say(f"- model: `{model}`")
    say(f"- api key: {'set' if settings.jev_api_key else 'EMPTY'}")
    say(f"- allowlist: {settings.allowed_jev_hosts}")
    say(f"- transport: {'mock' if args.mock else 'http'}")

    try:
        settings.verify_jev_host()
    except (ConfigError, JevHostNotAllowed) as exc:
        say()
        say(f"**REFUSED before sending anything:** {exc}")
        Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
        return 2

    warning = settings.jev_third_party_warning()
    if warning:
        say()
        say(f"> **THIRD PARTY WARNING** — {warning}")

    if not args.mock and not settings.jev_api_key:
        say()
        say("**No JEV_API_KEY.** Set it (or run with --mock to check the plumbing offline).")
        Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
        return 2

    client = JevClient(settings, None, mock=args.mock, transport=MockTransport() if args.mock else None)
    questions = build_questions()
    say()
    say("## Request (canonical, key stripped)")
    say()
    say("```json")
    say(canonical_json({"model": model, "state": STATE, "questions": questions})[:2000])
    say("```")

    try:
        response = await client.ask(STATE, questions)
    except (JevRequestError, JevUnavailable) as exc:
        say()
        say(f"**FAILED:** {type(exc).__name__}: {exc}")
        Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
        return 1

    say()
    say("## Raw response payload")
    say()
    say("```json")
    say(json.dumps(response.raw, indent=2, ensure_ascii=False)[:4000])
    say("```")
    say()
    say("## Parsed answers")
    say()
    say("| question | type | value | confidence | distribution |")
    say("|---|---|---|---|---|")
    for qid, answer in response.answers.items():
        dist = ", ".join(f"{k}={v}" for k, v in (answer.distribution or {}).items()) or "—"
        say(f"| {qid} | {answer.type} | {answer.value} | {answer.confidence} | {dist} |")

    problems: list[str] = []
    missing = set(questions) - set(response.answers)
    if missing:
        problems.append(f"questions without an answer: {sorted(missing)}")
    if not any(a.distribution for a in response.answers.values()):
        problems.append("no question returned a distribution — check the response field names")
    say()
    if problems:
        say("## Result: **NEEDS ATTENTION**")
        for problem in problems:
            say(f"- {problem}")
    else:
        say(f"## Result: **PASS** — model answered `{response.model}` in {response.latency_ms:.0f} ms "
            f"(cache {'hit' if response.from_cache else 'miss'})")
        say()
        say(f"Host check: `{settings.jev_host}` is {'the official TypeSafe host' if settings.jev_is_official_host else 'an allowlisted third party (see warning above)'}.")
    say()
    say("_Key material is never printed. The raw payload is printed on purpose: field names must be "
        "confirmed by a human before the pipeline trusts the parser._")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {Path(args.out).relative_to(REPO_ROOT)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

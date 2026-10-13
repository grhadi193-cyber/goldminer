"""`python -m worker selftest` — what is configured, what is missing, what is dangerous.

Never prints secret values; only presence/length and host names.
"""

from __future__ import annotations

import argparse
from datetime import datetime

from ..config import OFFICIAL_JEV_HOST, get_settings
from ..db import conn as dbconn
from ..timez import TEHRAN, UTC, day_bounds_utc, today_tehran

OK = "ok  "
WARN = "warn"
FAIL = "fail"


def _line(status: str, name: str, detail: str = "") -> None:
    print(f"[{status}] {name}{(' — ' + detail) if detail else ''}")


def run(args: argparse.Namespace | None = None) -> int:
    s = get_settings()
    problems = 0

    _line(OK, "timezone", f"Asia/Tehran offset now {datetime.now(tz=TEHRAN).utcoffset()}")
    today = today_tehran()
    start, end = day_bounds_utc(today, s.open_time, s.flat_time)
    _line(OK, "session", f"{today} 08:00-22:00 Tehran = {start.isoformat()} .. {end.isoformat()} (UTC)")

    if s.database_url or s.gm_demo_database_url:
        try:
            version = dbconn.ping()
            _line(OK, "database", version.split(" on ")[0])
        except Exception as exc:  # noqa: BLE001
            _line(FAIL, "database", f"cannot connect: {exc}")
            problems += 1
    else:
        _line(FAIL, "database", "DATABASE_URL is empty")
        problems += 1

    if s.telegram_api_id and s.telegram_api_hash and s.telegram_session:
        _line(OK, "telegram", f"{len(s.telegram_channel_list)} channel(s) configured")
    else:
        _line(WARN, "telegram", "credentials incomplete — ingestion commands will refuse to run")

    try:
        s.verify_jev_host()
    except Exception as exc:  # noqa: BLE001
        _line(FAIL, "jev allowlist", str(exc).splitlines()[0])
        problems += 1
    else:
        status = OK if s.jev_is_official_host else WARN
        _line(status, "jev host allowlisted", s.jev_host)
        warning = s.jev_third_party_warning()
        if warning:
            _line(WARN, "jev third-party", warning)
        if not s.jev_api_key:
            _line(WARN, "jev key", "JEV_API_KEY empty — real calls will fail (dry/demo runs are fine)")

    if s.openrouter_api_key:
        _line(OK, "openrouter", f"{len(s.extraction_model_list)} extraction model(s), explainer={'yes' if s.explainer_model else 'off'}")
    else:
        _line(WARN, "openrouter", "OPENROUTER_API_KEY empty — extraction disabled")

    try:
        s.assert_paper_only()
        _line(OK, "paper only", "PAPER_ONLY=true (no order path exists)")
    except Exception as exc:  # noqa: BLE001
        _line(FAIL, "paper only", str(exc).splitlines()[0])
        problems += 1

    _line(OK, "costs", f"gold {s.spread_bps_gold}/{s.slippage_bps_gold} bps spread/slippage, "
                       f"coin {s.spread_bps_coin}/{s.slippage_bps_coin}")
    _line(OK, "gates", f"min Jev confidence {s.jev_min_confidence}, max spread {s.max_spread_bps_tradeable} bps, "
                       f"stop {s.stop_loss_bps} bps, daily cap {s.daily_loss_cap_bps} bps")

    if s.start_date:
        _line(OK, "window", f"start {s.start_date}, discovery {s.discovery_months} months, "
                            f"history {s.history_months} months")
    else:
        _line(WARN, "window", "START_DATE not set")

    print()
    if problems:
        print(f"selftest: {problems} blocking problem(s)")
        return 1
    print("selftest: no blocking problems")
    return 0


__all__ = ["run", "UTC", "OFFICIAL_JEV_HOST"]

"""In-process bundle builder shared by the CLI exporter and the API server.

Both the `ui export` command and `GET /api/bundle/*` must produce the SAME bytes from the SAME
reads, so the assembly lives here once. The CLI writes the mapping to `ui/public/data`; the API
serves the requested member straight from the database (which is what makes a Render deployment
possible without shipping a static folder).

Every member is produced through the anti-leakage reads described in `cli/ui_cmd.py`.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Callable

import psycopg

from .cli import ui_cmd
from .config import Settings
from .research.backtest import load_artifact
from .research.stats.scoring import METHOD_VERSION
from .timez import TEHRAN
from .window import frozen_split


def bundle_members() -> list[str]:
    """Static member names (the timeline days are dynamic)."""
    return [
        "meta.json",
        "live.json",
        "leaderboard.json",
        "decisions.json",
        "timeline_index.json",
        "coverage.json",
        "hypotheses.json",
        "run_config.json",
        "backtest.json",
    ]


def build_bundle(
    conn: psycopg.Connection,
    settings: Settings,
    *,
    split_name: str = "discovery",
    days: int | None = 45,
    max_decisions: int = 60,
    include_backtest: bool = True,
    include_timeline: bool = True,
) -> dict[str, Any]:
    """Return `{member_name: payload}` — exactly what `ui export` writes to disk."""
    split = frozen_split(conn, settings)
    day_list: list[date] = split.days(split_name)
    if days:
        day_list = day_list[-days:]
    if not day_list:
        raise ValueError(f"split {split_name!r} has no trading days in this configuration")

    members: dict[str, Any] = {}
    emit: Callable[[str, Any], None] = lambda name, payload: members.__setitem__(name, payload)

    members["live.json"] = ui_cmd._live(None, conn, settings, split)
    members["leaderboard.json"] = ui_cmd._leaderboard(conn)
    members["decisions.json"] = ui_cmd._decisions(conn, split_name, max_decisions)
    if include_timeline:
        members["timeline_index.json"] = ui_cmd._timeline(conn, settings, day_list, emit)
    members["coverage.json"] = ui_cmd._coverage(conn, settings, day_list[0], day_list[-1])
    members["hypotheses.json"] = ui_cmd._hypotheses(conn)
    members["run_config.json"] = ui_cmd._run_config(conn, settings)

    artifact = load_artifact(split_name) if include_backtest else None
    if artifact is not None:
        members["backtest.json"] = artifact

    banner = None
    try:
        from .demo.guard import banner_for

        banner = banner_for(conn)
    except Exception:  # noqa: BLE001 - a missing banner must not break the bundle
        banner = None

    members["meta.json"] = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "generated_by": "worker.ui_bundle.build_bundle",
        "demo": bool(settings.gm_demo_mode),
        "banner": banner,
        "split": split_name,
        "split_window": {
            "start": split.start_date.isoformat(),
            "discovery_end": split.discovery_end.isoformat(),
            "outofsample_end": split.outofsample_end.isoformat(),
        },
        "days": {"first": day_list[0].isoformat(), "last": day_list[-1].isoformat(), "n": len(day_list)},
        "method_version": METHOD_VERSION,
        "tev": {"timezone": str(TEHRAN), "open_local": settings.open_at_local, "flat_local": settings.flat_at_local},
        "series": ui_cmd._series_meta(conn),
        "has_backtest": artifact is not None,
        "stale_after_minutes": ui_cmd.STALE_AFTER_MINUTES,
    }
    return members


__all__ = ["build_bundle", "bundle_members"]

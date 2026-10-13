"""Report generator (§7.4, M8).

Emits Markdown + JSON. The honesty header is copied verbatim from the prompt's §11 and is
placed at the TOP of every report, before any result, on purpose.

Every table in the Markdown carries the CLI/SQL that produced it, so a reader can re-derive
any number in the repository (M8 acceptance: "every claim traceable to a query in the repo").
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import psycopg

from ..config import REPO_ROOT, Settings
from ..db import repo
from ..prices import coverage as cov
from ..demo.guard import banner_for
from .stats.scoring import METHOD_VERSION

log = logging.getLogger(__name__)

REPORTS = REPO_ROOT / "reports"

HONESTY_SECTION = """## Honesty section — what 6 months of data can and cannot prove

- 6 months ≈ 120–130 trading days; the out-of-sample split is ~40–45 days. ANY daily accuracy
  figure on ~40 days carries a 95% CI of roughly ±15 percentage points. A headline like
  "72% directional accuracy" is not statistically distinguishable from a coin flip against
  this sample. Treat day-level numbers as directional evidence, never proof.
- Message-level event studies (thousands of events) have real statistical power. Their
  results — channel lead/lag labels, per-horizon lifts — are the phase-1 deliverable you can
  trust, WITH the caveat that domestic gold intraday history is approximated (TGJU daily +
  live logger only covers the recent period); the +30m/+1h/+3h horizons are computed against
  USDTIRT and XAUUSD, and against domestic gold only where live-minute data exists.
- Effect sizes with bootstrap CIs are mandatory; p-values without CIs must never be shown
  alone. FDR correction covers only PRE-REGISTERED hypothesis families; anything explored ad
  hoc is labelled "exploratory, uncorrected".
- Jev confidence is derived from its answer distribution, NOT guaranteed calibrated. The
  reliability curves decide whether "70% confidence" means 70% — until calibration is
  demonstrated out-of-sample, do not trade on the raw number.
- Execution costs (spread+slippage for ~1 kg) can plausibly consume the entire edge of most
  intraday signals. Net-of-costs numbers are the only numbers that matter.
- Known weaknesses: channel messages may lag the actual market move (most "tip" channels are
  followers); TGJU is unofficial and its "last price" is indicative, not a tradeable quote;
  Nobitex USDTIRT is a proxy for free-market USD, not the physical bazaar rate;
  holidays/Thursdays/Fridays distort day counts; Telegram account safety (use a dedicated
  non-personal account; do not bulk-scrape aggressively).
"""


@dataclass
class ReportPaths:
    markdown: Path
    json: Path


def trading_stats(equity: list[dict[str, Any]], *, days_per_year: int = 250) -> dict[str, Any]:
    """CAGR / Sharpe / max drawdown / expectancy from the per-day equity curve."""
    if not equity:
        return {"n_days": 0}
    values = np.array([row["equity"] for row in equity], dtype=float)
    returns = np.diff(np.concatenate([[values[0]], values])) / np.concatenate([[values[0]], values[:-1]])
    daily_bps = np.array([row["net_bps"] for row in equity], dtype=float)
    peak = np.maximum.accumulate(values)
    drawdown = (values / peak - 1.0)
    total_return = values[-1] / values[0] - 1.0
    years = len(values) / days_per_year
    cagr = (values[-1] / values[0]) ** (1 / years) - 1.0 if years > 0 else float("nan")
    sharpe = float(np.mean(daily_bps) / np.std(daily_bps) * np.sqrt(days_per_year)) if np.std(daily_bps) > 0 else 0.0
    win_rate = float(np.mean(daily_bps > 0)) if len(daily_bps) else float("nan")
    return {
        "n_days": int(len(values)),
        "total_return_pct": round(total_return * 100.0, 4),
        "cagr_pct": round(cagr * 100.0, 4),
        "sharpe": round(sharpe, 3),
        "max_drawdown_pct": round(float(drawdown.min()) * 100.0, 4),
        "expectancy_bps_per_day": round(float(np.mean(daily_bps)), 3),
        "win_rate": round(win_rate, 4),
        "mean_daily_return": round(float(np.mean(returns)), 6),
    }


def leaderboard_table(scoring: dict[str, Any]) -> str:
    lines = [
        "| channel | label | lead/lag (min) | n events | gross 30m bps | net 30m bps | net EOD bps | 95% CI (net EOD) | q | FDR-corrected | pumper | measure |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for ch in scoring.get("channels", []):
        ci = (ch.get("ci_95") or {}).get("eod") or [None, None]
        ci_txt = f"[{ci[0]}, {ci[1]}]" if ci[0] is not None else "—"
        measures = ", ".join(f"{k}:{v}" for k, v in (ch.get("measures") or {}).items()) or "—"
        q = (ch.get("q_values") or {}).get("eod")
        lines.append(
            f"| {ch['username']} | {ch['label']} | {ch.get('leads_lag_minutes') or '—'} | {ch['n_events']} | "
            f"{(ch.get('lifts_gross_bps') or {}).get('30m', '—')} | {(ch.get('lifts_net_bps') or {}).get('30m', '—')} | "
            f"{(ch.get('lifts_net_bps') or {}).get('eod', '—')} | {ci_txt} | {q if q is not None else '—'} | yes | "
            f"{'YES' if ch.get('pumper_flag') else 'no'} | {measures} |"
        )
    return "\n".join(lines)


def agreement_table(scoring: dict[str, Any]) -> str:
    rows = scoring.get("agreement") or []
    if not rows:
        return "_no agreement events in this window_"
    lines = ["| k channels | horizon | n events | net bps | 95% CI | p | sign consistency |", "|---|---|---|---|---|---|---|"]
    for row in sorted(rows, key=lambda r: (r["k"], r["horizon"])):
        lines.append(
            f"| {row['k']} | {row['horizon']} | {row['n_events']} | {row['net_bps']} | "
            f"[{row['ci_low']}, {row['ci_high']}] | {row['p_value']} | {row['sign_consistency']} |"
        )
    return "\n".join(lines)


def baselines_table(baselines: list[dict[str, Any]]) -> str:
    lines = [
        "| model | calls | coverage | directional accuracy (non-flat) | mean gross bps | mean NET bps |",
        "|---|---|---|---|---|---|",
    ]
    for row in baselines:
        acc = row["directional_accuracy"]
        lines.append(
            f"| {row['name']} | {row['n_calls']} | {row['coverage']:.3f} | "
            f"{acc if acc is not None else '—'} | {row['mean_signed_bps_gross']} | {row['mean_signed_bps_net']} |"
        )
    return "\n".join(lines)


def calibration_table(calibration: dict[str, Any]) -> str:
    lines = ["| predicted-probability bin | n | mean confidence | empirical frequency | gap |", "|---|---|---|---|---|"]
    for row in calibration.get("reliability") or []:
        if row["n"]:
            lines.append(
                f"| {row['bin_low']:.1f}-{row['bin_high']:.1f} | {row['n']} | {row['mean_confidence']} | "
                f"{row['empirical_frequency']} | {row['gap']} |"
            )
    lines.append("")
    lines.append(f"Brier score: **{calibration.get('brier')}** · ECE: **{calibration.get('ece')}** · n = {calibration.get('n')}")
    lines.append("")
    lines.append("| confidence gate | coverage | n trades | precision (up calls) | base rate (up days) |")
    lines.append("|---|---|---|---|---|")
    for row in calibration.get("sweep") or []:
        lines.append(
            f"| >= {row['threshold']:.2f} | {row['coverage']} | {row['n_trades']} | {row['precision']} | "
            f"{row['hit_rate_up_days']} |"
        )
    return "\n".join(lines)


def coverage_section(rows: list[cov.CoverageRow], accept: list[cov.Acceptance], *, window: str) -> str:
    lines = [f"Coverage window: **{window}**", "", "| source | symbol | res | month | bars | expected | coverage | gaps | gap minutes |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(
            f"| {r.source} | {r.symbol} | {r.resolution} | {r.month} | {r.n_bars} | {r.expected_bars} | "
            f"{r.coverage_pct:.1f}% | {r.n_gaps} | {r.gap_minutes:.0f} |"
        )
    lines += ["", "| acceptance check | result | detail | threshold |", "|---|---|---|---|"]
    for a in accept:
        lines.append(f"| {a.name} | {'PASS' if a.passed else 'FAIL'} | {a.detail} | {a.threshold} |")
    return "\n".join(lines)


def hypotheses_table(conn: psycopg.Connection) -> str:
    rows = conn.execute(
        "select id, text, status, proposed_by_model, test_method, test_results_json, tested_at "
        "from hypotheses order by id"
    ).fetchall()
    if not rows:
        return "_no hypotheses recorded yet (run the explainer, or insert your own)_"
    lines = ["| id | hypothesis | status | proposed by | test | verdict detail |", "|---|---|---|---|---|---|"]
    for row in rows:
        results = row["test_results_json"] or {}
        detail = ""
        if isinstance(results, dict):
            verdicts = results.get("results")
            if isinstance(verdicts, list):
                detail = "; ".join(f"{r.get('channel') or r.get('bucket') or r.get('k')}: {r.get('verdict')}"
                                   for r in verdicts if isinstance(r, dict))[:200]
            elif isinstance(verdicts, dict):
                detail = "; ".join(f"{k}: {v.get('verdict')}" for k, v in verdicts.items() if isinstance(v, dict))[:200]
            elif results.get("note"):
                detail = str(results["note"])[:200]
        lines.append(f"| {row['id']} | {row['text'][:160]} | {row['status']} | {row['proposed_by_model'] or '—'} | "
                     f"{row['test_method'] or '—'} | {detail or '—'} |")
    return "\n".join(lines)


def mark_basis_table(conn: psycopg.Connection, run_id: int | None) -> str:
    """Which series actually marked the two legs of each trade — the difference between a
    measured P&L and a P&L that is only execution cost."""
    if run_id is None:
        return "_unknown run_"
    rows = conn.execute(
        """
        select coalesce(detail_json->'entry_mark'->>'source', 'unknown') as source,
               coalesce(detail_json->'entry_mark'->>'resolution', '?') as resolution,
               count(*) as n,
               count(*) filter (where detail_json->>'same_bar_mark' = 'true') as same_bar
        from backtest_trades where run_id = %s
        group by 1, 2 order by n desc
        """,
        (run_id,),
    ).fetchall()
    if not rows:
        return "_no trades in this run_"
    lines = ["| mark source | resolution | trades | entered and exited on the SAME bar |", "|---|---|---|---|"]
    for row in rows:
        lines.append(f"| {row['source']} | {row['resolution']} | {row['n']} | {row['same_bar']} |")
    return "\n".join(lines)


def equity_section(equity: list[dict[str, Any]]) -> str:
    stats = trading_stats(equity)
    if not stats.get("n_days"):
        return "_no trades in this split — the gates blocked everything (that is a finding, not a bug)_"
    trades_total = sum(int(row["trades"]) for row in equity)
    unmeasured = sum(int(row.get("unmeasured_marks") or 0) for row in equity)
    lines = [
        f"- days traded: **{stats['n_days']}**",
        f"- total return on the paper book: **{stats['total_return_pct']}%** "
        f"(CAGR {stats['cagr_pct']}% — meaningless at this sample size, shown for completeness)",
        f"- Sharpe (annualised, 250 days): **{stats['sharpe']}**",
        f"- max drawdown: **{stats['max_drawdown_pct']}%**",
        f"- expectancy: **{stats['expectancy_bps_per_day']} bps/day**, win rate {stats['win_rate']}",
        f"- trades: **{trades_total}**, of which **{unmeasured}** entered and exited on the SAME price bar",
    ]
    if unmeasured:
        lines += [
            "",
            "> **Mechanically unmeasured P&L.** Those trades read the same bar for entry and exit, so",
            "> their mid-to-mid move is 0 by construction: the row records EXECUTION COST, not a price",
            "> outcome. This happens when the only series available for an asset is a daily bar (see the",
            "> mark-basis table below). Read the equity curve as a cost study, not as a strategy result,",
            "> until a genuinely intraday series marks both legs.",
        ]
    lines += ["", "| day | trades | net bps | equity | drawdown % |", "|---|---|---|---|---|"]
    for row in equity:
        lines.append(f"| {row['day']} | {row['trades']} | {row['net_bps']} | {row['equity']} | {row['drawdown']} |")
    return "\n".join(lines)


def traceability_section(split: str) -> str:
    return f"""Every table above is reproducible from the repository:

| table | how to regenerate |
|---|---|
| coverage, acceptance | `python -m worker prices verify` (writes `docs/coverage_report.md`) |
| leaderboard, agreement, FDR family | `python -m worker stats event-study --split {split}` → `reports/stats_{split}.json` |
| Jev vs baselines | `python -m worker stats baselines --split {split}` → `reports/baselines_{split}.json` |
| calibration curve / Brier / ECE | `python -m worker stats calibration --split {split}` → `reports/calibration_{split}.json` |
| equity curve, trades | `python -m worker backtest run --split {split}` → `backtest_runs`, `backtest_trades` |
| Jev decisions + state snapshots | `select * from jev_decisions where split = '{split}'` |
| hypotheses + coded tests | `python -m worker hypothesis list` / `hypothesis test --id N` |

Anti-leakage: every number in this report was produced through `worker.research.asof.ReadContext`,
which refuses rows with event time after the decision instant (tests T1–T6 in `worker/tests/`).
"""


def build_markdown(
    *,
    conn: psycopg.Connection,
    settings: Settings,
    artifact: dict[str, Any],
    split: str,
    narrative: str = "",
    narrative_note: str = "",
) -> str:
    scoring = artifact.get("scoring") or {}
    config = artifact.get("config") or {}
    window_desc = f"{config.get('first_day')} .. {config.get('last_day')}"
    rows = cov.coverage_table(conn, start=date.fromisoformat(config["first_day"]),
                              end=date.fromisoformat(config["last_day"]), settings=settings)
    accept = cov.acceptance(conn, start=date.fromisoformat(config["first_day"]),
                            end=date.fromisoformat(config["last_day"]), settings=settings)
    banner = banner_for(conn)
    parts: list[str] = []
    parts.append(f"# Mishkah report — `{split}` split")
    parts.append("")
    if banner:
        parts.append(banner)
        parts.append("")
    parts.append(f"_Generated {datetime.now(tz=timezone.utc).isoformat(timespec='seconds')} · "
                 f"method version `{METHOD_VERSION}` · run_id {artifact.get('run_id')}_")
    parts.append("")
    parts.append("**This report states which window its numbers came from:** "
                 f"split `{split}` over `{window_desc}` "
                 f"({scoring.get('days_covered')} days covered, {scoring.get('days_missing')} missing data).")
    parts.append("")
    parts.append(HONESTY_SECTION)
    parts.append("")
    parts.append("## 1. Coverage (what data actually exists)")
    parts.append("")
    parts.append(coverage_section(rows, accept, window=window_desc))
    parts.append("")
    parts.append("## 2. Channel leaderboard (net of costs, FDR-corrected)")
    parts.append("")
    parts.append(f"- measurement series: `{scoring.get('measurement_series')}`")
    parts.append(f"- pre-registered FDR family: {scoring.get('family')}")
    parts.append(f"- measure coverage (which series measured each event): {scoring.get('measure_coverage')}")
    parts.append("- the `measure` column of the leaderboard tells you whether a channel's numbers come from a")
    parts.append("  genuine domestic-gold intraday series or from the USDTIRT/XAUUSD proxy")
    parts.append("")
    parts.append(leaderboard_table(scoring))
    parts.append("")
    parts.append("### Agreement events (k channels agreeing within 30 minutes)")
    parts.append("")
    parts.append(agreement_table(scoring))
    parts.append("")
    parts.append("## 3. Day-level calls: Jev vs baselines")
    parts.append("")
    parts.append(baselines_table(artifact.get("baselines") or []))
    parts.append("")
    parts.append("## 4. Calibration (does Jev's confidence mean anything?)")
    parts.append("")
    parts.append(calibration_table(artifact.get("calibration") or {}))
    parts.append("")
    parts.append("## 5. Paper equity curve and trading statistics")
    parts.append("")
    parts.append(equity_section(artifact.get("equity") or []))
    parts.append("")
    parts.append("### Mark basis (which series priced entry and exit)")
    parts.append("")
    parts.append(mark_basis_table(conn, artifact.get("run_id")))
    parts.append("")
    parts.append("`gross_bps` is the frictionless MID-to-MID move of the holding window; "
                 "`cost_bps` = round trip = spread + 2×slippage; `net_bps = gross_bps − cost_bps`.")
    parts.append("")
    parts.append("## 6. Hypotheses (proposed by the explainer model, tested by code)")
    parts.append("")
    parts.append(hypotheses_table(conn))
    parts.append("")
    parts.append("## 7. Narrative")
    parts.append("")
    if narrative:
        parts.append(narrative)
    else:
        parts.append(f"_No narrative: {narrative_note or 'explainer not configured'}._")
    parts.append("")
    parts.append("## 8. Traceability")
    parts.append("")
    parts.append(traceability_section(split))
    return "\n".join(parts)


def write_report(
    conn: psycopg.Connection,
    settings: Settings,
    artifact: dict[str, Any],
    *,
    split: str,
    narrative: str = "",
    narrative_note: str = "",
) -> ReportPaths:
    REPORTS.mkdir(exist_ok=True)
    markdown = build_markdown(conn=conn, settings=settings, artifact=artifact, split=split,
                              narrative=narrative, narrative_note=narrative_note)
    md_path = REPORTS / f"report_{split}.md"
    json_path = REPORTS / f"report_{split}.json"
    md_path.write_text(markdown, encoding="utf-8")
    payload = {**artifact, "narrative": narrative, "narrative_note": narrative_note,
               "generated_at": datetime.now(tz=timezone.utc).isoformat()}
    json_path.write_text(json.dumps(payload, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    repo.audit(conn, "worker:report", "written", {"split": split, "markdown": str(md_path.name)})
    conn.commit()
    return ReportPaths(markdown=md_path, json=json_path)


__all__ = [
    "HONESTY_SECTION",
    "ReportPaths",
    "build_markdown",
    "write_report",
    "trading_stats",
    "leaderboard_table",
    "baselines_table",
    "calibration_table",
    "hypotheses_table",
    "equity_section",
    "mark_basis_table",
]

"""`python -m worker stats ...` — event study, lead/lag, calibration, baselines (M7)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .. import window
from ..config import REPO_ROOT, get_settings
from ..db import conn as dbconn
from ..research.stats.baselines import evaluate_all
from ..research.stats.calibration import build_report
from ..research.stats.daylevel import build_day_records, calibration_rows
from ..research.stats.scoring import run_scoring

REPORTS = REPO_ROOT / "reports"


def _split_days(split_name: str):
    settings = get_settings()
    conn = dbconn.sync_conn()
    connection = conn.__enter__()
    split = window.frozen_split(connection, settings)
    return connection, conn, split.days(split_name)


def _write(name: str, payload: dict) -> Path:
    REPORTS.mkdir(exist_ok=True)
    path = REPORTS / name
    path.write_text(json.dumps(payload, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    return path


def _print_channels(artifact) -> None:
    print(f"\n{'channel':22s} {'label':13s} {'lag':>5s} {'n_ev':>6s} {'gross30m':>9s} {'net30m':>8s} "
          f"{'gross@EOD':>10s} {'net@EOD':>9s} {'CI95(net)':>19s} {'q':>7s} pump")
    for ch in artifact["channels"]:
        ci = ch["ci_95"].get("eod") or [None, None]
        ci_txt = f"[{(ci[0] or 0):7.1f},{(ci[1] or 0):7.1f}]"

        def fmt(mapping: dict, key: str, width: int = 9) -> str:
            value = mapping.get(key)
            return f"{value:{width}.1f}" if value is not None else " " * (width - 1) + "-"

        q = ch["q_values"].get("eod")
        q_txt = f"{q:7.3f}" if q is not None else "      -"
        print(f"{ch['username'][:22]:22s} {ch['label']:13s} {str(ch['leads_lag_minutes'] or '-'):>5s} "
              f"{ch['n_events']:6d} {fmt(ch['lifts_gross_bps'], '30m')} {fmt(ch['lifts_net_bps'], '30m', 8)} "
              f"{fmt(ch['lifts_gross_bps'], 'eod', 10)} {fmt(ch['lifts_net_bps'], 'eod')} {ci_txt:>19s} {q_txt} "
              f"{'YES' if ch['pumper_flag'] else ''}")


def event_study(args: argparse.Namespace) -> int:
    conn, holder, days = _split_days(args.split)
    try:
        settings = get_settings()
        artifact = run_scoring(conn, settings, split_name=args.split, days=days, persist=True)
        payload = artifact.as_dict()
        path = _write(f"stats_{args.split}.json", payload)
        print(f"family: {payload['family']}")
        print(f"measurement series: {payload['measurement_series']}")
        print(f"days covered {payload['days_covered']}, missing {payload['days_missing']}, "
              f"events {sum(payload['measure_coverage'].values())}")
        _print_channels(payload)
        print(f"\nagreement events (k channels within 30m):")
        for row in payload["agreement"]:
            print(f"  k={row['k']} {row['horizon']:>4s} n={row['n_events']:5d} net={row['net_bps']:8.2f} bps "
                  f"CI[{row['ci_low']:.2f},{row['ci_high']:.2f}] p={row['p_value']:.4f}")
        print(f"\nwritten: {path.relative_to(REPO_ROOT)}")
    finally:
        holder.__exit__(None, None, None)
    return 0


def leadlag(args: argparse.Namespace) -> int:
    conn, holder, days = _split_days(args.split)
    try:
        payload = json.loads((REPORTS / f"stats_{args.split}.json").read_text(encoding="utf-8"))
        print("lead/lag labels (from the last scoring run — rerun `stats event-study` to refresh):")
        for ch in sorted(payload["channels"], key=lambda c: c["label"]):
            print(f"  {ch['username'][:24]:24s} {ch['label']:13s} peak lag {ch['leads_lag_minutes']}  "
                  f"{ch['label_reason']}")
    finally:
        holder.__exit__(None, None, None)
    return 0


def calibration(args: argparse.Namespace) -> int:
    conn, holder, days = _split_days(args.split)
    try:
        rows = calibration_rows(conn, days)
        report = build_report(rows)
        payload = report.as_dict()
        payload["split"] = args.split
        path = _write(f"calibration_{args.split}.json", payload)
        print(f"n={report.n}  Brier={report.brier:.4f}  ECE={report.ece:.4f}")
        print("\nreliability (predicted class):")
        for b in report.reliability:
            if b["n"]:
                print(f"  {b['bin_low']:.1f}-{b['bin_high']:.1f}: n={b['n']:3d} conf={b['mean_confidence']:.3f} "
                      f"emp={b['empirical_frequency']:.3f} gap={b['gap']:+.3f}")
        print("\nconfidence sweep:")
        for row in report.sweep:
            print(f"  >= {row['threshold']:.2f}: coverage={row['coverage']} n={row['n_trades']} "
                  f"precision={row['precision']} (base rate up={row['hit_rate_up_days']})")
        print(f"\nwritten: {path.relative_to(REPO_ROOT)}")
    finally:
        holder.__exit__(None, None, None)
    return 0


def baselines(args: argparse.Namespace) -> int:
    conn, holder, days = _split_days(args.split)
    try:
        settings = get_settings()
        records = build_day_records(conn, settings, days)
        scores = evaluate_all(records, settings=settings)
        payload = {"split": args.split, "n_days": len(records), "scores": [s.as_dict() for s in scores]}
        path = _write(f"baselines_{args.split}.json", payload)
        print(f"{'model':34s} {'calls':>6s} {'cover':>7s} {'dir.acc':>8s} {'gross':>9s} {'net':>9s}")
        for score in scores:
            print(f"{score.name:34s} {score.n_calls:6d} {score.coverage:7.3f} "
                  f"{(f'{score.directional_accuracy:8.3f}' if score.directional_accuracy is not None else '       -')} "
                  f"{(f'{score.mean_signed_bps_gross:9.2f}' if score.mean_signed_bps_gross is not None else '        -')} "
                  f"{(f'{score.mean_signed_bps_net:9.2f}' if score.mean_signed_bps_net is not None else '        -')}")
        print(f"\nwritten: {path.relative_to(REPO_ROOT)}")
    finally:
        holder.__exit__(None, None, None)
    return 0


def costs(args: argparse.Namespace) -> int:
    settings = get_settings()
    print("cost model (deterministic code, R5). Reported lifts follow T8:")
    print("    net_lift_bps = gross_bps - spread_bps - slippage_bps")
    for asset, cfg in settings.cost_bps.items():
        print(f"  {asset:8s} spread={cfg['spread_bps']:5.1f} slippage={cfg['slippage_bps']:5.1f} "
              f"-> one-sided {cfg['spread_bps'] + cfg['slippage_bps']:5.1f} bps, "
              f"round trip {cfg['spread_bps'] + 2 * cfg['slippage_bps']:5.1f} bps")
    return 0


def all_(args: argparse.Namespace) -> int:
    rc = event_study(args)
    rc |= calibration(args)
    rc |= baselines(args)
    return rc


__all__ = ["event_study", "leadlag", "calibration", "baselines", "costs", "all_"]


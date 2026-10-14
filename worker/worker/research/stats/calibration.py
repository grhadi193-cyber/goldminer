"""Calibration of Jev's day_direction distribution (7.3).

Answers one question honestly: "when Jev says 70%, is it right ~70% of the time?" — and does
so per split, with the raw numbers a report can quote:

  * reliability curve per class (binned predicted probability vs empirical frequency)
  * Brier score (multi-class: up/down/flat)
  * Expected Calibration Error (ECE)
  * a precision/coverage sweep as the JEV_MIN_CONFIDENCE gate moves
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np

CLASSES = ("up", "down", "flat")
DEFAULT_BINS = 10


@dataclass
class CalibrationRow:
    day: str
    predicted: str
    probs: dict[str, float]
    actual: str
    confidence: float | None


@dataclass
class CalibrationReport:
    n: int
    brier: float
    ece: float
    reliability: list[dict[str, Any]] = field(default_factory=list)
    per_class: dict[str, dict[str, float]] = field(default_factory=dict)
    sweep: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "brier": self.brier,
            "ece": self.ece,
            "reliability": self.reliability,
            "per_class": self.per_class,
            "sweep": self.sweep,
        }


def reliability_curve(
    rows: Sequence[CalibrationRow], *, bins: int = DEFAULT_BINS, class_name: str | None = None
) -> list[dict[str, Any]]:
    """Bin predicted probabilities; compare with the empirical frequency of that class.

    With `class_name=None` the curve is computed for the *predicted* class (the confidence
    curve a user reads as "how often is the top pick right when it says X").
    """
    buckets: list[dict[str, Any]] = [
        {"bin_low": i / bins, "bin_high": (i + 1) / bins, "n": 0, "sum_conf": 0.0, "sum_hit": 0.0}
        for i in range(bins)
    ]
    for row in rows:
        if class_name is None:
            conf = max(row.probs.values()) if row.probs else 0.0
            hit = 1.0 if row.predicted == row.actual else 0.0
        else:
            conf = float(row.probs.get(class_name, 0.0))
            hit = 1.0 if row.actual == class_name else 0.0
        idx = min(bins - 1, max(0, int(conf * bins)))
        buckets[idx]["n"] += 1
        buckets[idx]["sum_conf"] += conf
        buckets[idx]["sum_hit"] += hit
    out = []
    for b in buckets:
        n = b["n"]
        out.append(
            {
                "bin_low": b["bin_low"],
                "bin_high": b["bin_high"],
                "n": n,
                "mean_confidence": round(b["sum_conf"] / n, 4) if n else None,
                "empirical_frequency": round(b["sum_hit"] / n, 4) if n else None,
                "gap": round(b["sum_conf"] / n - b["sum_hit"] / n, 4) if n else None,
            }
        )
    return out


def brier_score(rows: Sequence[CalibrationRow]) -> float:
    if not rows:
        return float("nan")
    total = 0.0
    for row in rows:
        for cls in CLASSES:
            p = float(row.probs.get(cls, 0.0))
            y = 1.0 if row.actual == cls else 0.0
            total += (p - y) ** 2
    return total / len(rows)


def expected_calibration_error(rows: Sequence[CalibrationRow], *, bins: int = DEFAULT_BINS) -> float:
    curve = reliability_curve(rows, bins=bins)
    n_total = sum(b["n"] for b in curve)
    if not n_total:
        return float("nan")
    ece = 0.0
    for b in curve:
        if b["n"] and b["gap"] is not None:
            ece += (b["n"] / n_total) * abs(b["gap"])
    return ece


def confidence_sweep(
    rows: Sequence[CalibrationRow],
    *,
    thresholds: Sequence[float] = (0.0, 0.4, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.9),
) -> list[dict[str, Any]]:
    """Precision / coverage of the long call as the confidence gate moves.

    "Long call" = predicted class is `up`. Precision = share of those days that really closed
    up; coverage = share of all days still traded at that threshold.
    """
    out: list[dict[str, Any]] = []
    for threshold in thresholds:
        taken = [r for r in rows if r.predicted == "up" and r.confidence is not None and r.confidence >= threshold]
        hits = sum(1 for r in taken if r.actual == "up")
        out.append(
            {
                "threshold": threshold,
                "coverage": round(len(taken) / len(rows), 4) if rows else None,
                "n_trades": len(taken),
                "precision": round(hits / len(taken), 4) if taken else None,
                "hit_rate_up_days": round(
                    sum(1 for r in rows if r.actual == "up") / len(rows), 4
                ) if rows else None,
            }
        )
    return out


def build_report(
    rows: Sequence[CalibrationRow], *, bins: int = DEFAULT_BINS, sweep_thresholds: Sequence[float] | None = None
) -> CalibrationReport:
    per_class: dict[str, dict[str, float]] = {}
    for cls in CLASSES:
        positives = [r for r in rows if r.actual == cls]
        per_class[cls] = {
            "n_actual": len(positives),
            "mean_predicted": round(float(np.mean([r.probs.get(cls, 0.0) for r in rows])), 4) if rows else None,
            "empirical": round(len(positives) / len(rows), 4) if rows else None,
        }
    return CalibrationReport(
        n=len(rows),
        brier=brier_score(rows),
        ece=expected_calibration_error(rows, bins=bins),
        reliability=reliability_curve(rows, bins=bins),
        per_class=per_class,
        sweep=confidence_sweep(rows, thresholds=sweep_thresholds) if sweep_thresholds else confidence_sweep(rows),
    )


__all__ = [
    "CLASSES",
    "CalibrationRow",
    "CalibrationReport",
    "reliability_curve",
    "brier_score",
    "expected_calibration_error",
    "confidence_sweep",
    "build_report",
]

"""Baselines B1–B5 (7.3) — every one of them reported NEXT TO Jev, same split, same costs.

    B1  same direction as yesterday's EOD label
    B2  USDTIRT up 08:00 -> 12:00 implies gold up
    B3  always up
    B4  always flat
    B5  multinomial logistic regression on the numeric state features (plain code, no LLM)

Scoring convention (shared with Jev's row):
  * directional accuracy is computed on NON-FLAT calls, and the always-up / always-flat
    baselines are shown with their own accuracy so the comparison is not flattering by
    construction (note B3 scores ~P(up) by construction)
  * net expectancy = mean over called days of the realized day move signed by the call,
    minus the round-trip cost (long-only: a "down" call means "don't trade", not "short")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Sequence

import numpy as np

from ...config import Settings
from ..costs import cost_model

log = logging.getLogger(__name__)

CLASSES = ("up", "down", "flat")
FEATURE_NAMES = (
    "usdtirt_morning_pct",
    "ounce_morning_pct",
    "yesterday_label_up",
    "yesterday_label_down",
    "gold_7d_mean_bps",
    "usdtirt_7d_mean_bps",
)


@dataclass
class DayRecord:
    """One day of decision-relevant facts, all computed from data up to the decision."""

    day: date
    realized_label: str
    realized_bps: float
    features: dict[str, float] = field(default_factory=dict)
    jev_predicted: str | None = None
    jev_probs: dict[str, float] = field(default_factory=dict)
    jev_confidence: float | None = None


@dataclass
class BaselineScore:
    name: str
    n_calls: int
    coverage: float
    directional_accuracy: float | None
    mean_signed_bps_gross: float | None
    mean_signed_bps_net: float | None
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "n_calls": self.n_calls,
            "coverage": round(self.coverage, 4),
            "directional_accuracy": _r(self.directional_accuracy),
            "mean_signed_bps_gross": _r(self.mean_signed_bps_gross),
            "mean_signed_bps_net": _r(self.mean_signed_bps_net),
            "notes": self.notes,
        }


def _r(value: float | None) -> float | None:
    return None if value is None else round(float(value), 4)


# --------------------------------------------------------------------------- predictions

def b1_yesterday(records: Sequence[DayRecord]) -> list[str | None]:
    out: list[str | None] = []
    prev: str | None = None
    for record in records:
        out.append(prev)
        prev = record.realized_label
    return out


def b2_usdtirt_morning(records: Sequence[DayRecord], *, threshold_pct: float = 0.05) -> list[str | None]:
    out: list[str | None] = []
    for record in records:
        move = record.features.get("usdtirt_morning_pct")
        if move is None:
            out.append(None)
        elif move > threshold_pct:
            out.append("up")
        elif move < -threshold_pct:
            out.append("down")
        else:
            out.append("flat")
    return out


def b3_always_up(records: Sequence[DayRecord]) -> list[str | None]:
    return ["up"] * len(records)


def b4_always_flat(records: Sequence[DayRecord]) -> list[str | None]:
    return ["flat"] * len(records)


def _softmax(z: np.ndarray) -> np.ndarray:
    z = z - np.max(z, axis=1, keepdims=True)
    e = np.exp(z)
    return e / np.sum(e, axis=1, keepdims=True)


def _fit_logistic(
    x: np.ndarray, y: np.ndarray, *, classes: int = 3, epochs: int = 400, lr: float = 0.35, l2: float = 1e-3
) -> np.ndarray:
    """Plain multinomial logistic regression (gradient descent). Deliberately simple: the
    baseline exists to show what an unglamorous linear model can do on the same features."""
    n, d = x.shape
    w = np.zeros((d, classes))
    b = np.zeros(classes)
    for _ in range(epochs):
        probs = _softmax(x @ w + b)
        grad_w = (x.T @ (probs - _onehot(y, classes))) / n + l2 * w
        grad_b = np.mean(probs - _onehot(y, classes), axis=0)
        w -= lr * grad_w
        b -= lr * grad_b
    return np.vstack([w, b])


def _onehot(y: np.ndarray, classes: int) -> np.ndarray:
    out = np.zeros((len(y), classes))
    out[np.arange(len(y)), y] = 1.0
    return out


def b5_logistic(
    records: Sequence[DayRecord], *, min_train_days: int = 20, refit_every: int = 5
) -> list[dict[str, float] | None]:
    """Walk-forward: each day is predicted from ONLY the days before it (no look-ahead)."""
    usable = [r for r in records if all(name in r.features for name in FEATURE_NAMES)]
    out: list[dict[str, float] | None] = []
    params: np.ndarray | None = None
    train_x: list[list[float]] = []
    train_y: list[int] = []
    index_of_class = {c: i for i, c in enumerate(CLASSES)}
    for i, record in enumerate(usable):
        if len(train_x) >= min_train_days and (params is None or (i % refit_every == 0)):
            params = _fit_logistic(np.array(train_x, dtype=float), np.array(train_y, dtype=int))
        if params is None:
            out.append(None)
        else:
            x = np.array([[record.features[name] for name in FEATURE_NAMES]], dtype=float)
            probs = _softmax(np.hstack([x, np.ones((1, 1))]) @ params)[0]
            out.append({c: float(probs[idx]) for c, idx in index_of_class.items()})
        train_x.append([record.features[name] for name in FEATURE_NAMES])
        train_y.append(index_of_class[record.realized_label])
    return out


# --------------------------------------------------------------------------- scoring

def score_predictions(
    records: Sequence[DayRecord],
    predictions: Sequence[str | None],
    *,
    settings: Settings,
    name: str,
    asset: str = "gold",
) -> BaselineScore:
    """Directional accuracy + net expectancy of a set of day calls (same costs as Jev)."""
    if len(predictions) != len(records):
        raise ValueError("predictions and records must line up")
    cm = cost_model(settings, asset)
    calls = [(r, p) for r, p in zip(records, predictions, strict=True) if p is not None]
    if not calls:
        return BaselineScore(name=name, n_calls=0, coverage=0.0, directional_accuracy=None,
                             mean_signed_bps_gross=None, mean_signed_bps_net=None, notes="no calls")
    non_flat = [(r, p) for r, p in calls if p != "flat"]
    correct = sum(1 for r, p in non_flat if p == r.realized_label)
    accuracy = (correct / len(non_flat)) if non_flat else None
    gross = []
    net = []
    for record, prediction in calls:
        if prediction != "up":       # long-only: down/flat calls mean "stay flat"
            gross.append(0.0)
            net.append(0.0)
            continue
        gross.append(record.realized_bps)
        net.append(record.realized_bps - cm.round_trip_cost_bps)
    return BaselineScore(
        name=name,
        n_calls=len(calls),
        coverage=len(calls) / len(records),
        directional_accuracy=accuracy,
        mean_signed_bps_gross=float(np.mean(gross)),
        mean_signed_bps_net=float(np.mean(net)),
        notes=f"{len(non_flat)} non-flat calls, cost {cm.round_trip_cost_bps:.0f} bps per round trip",
    )


def score_jev(records: Sequence[DayRecord], *, settings: Settings, asset: str = "gold") -> BaselineScore:
    preds = [r.jev_predicted for r in records]
    score = score_predictions(records, preds, settings=settings, name="Jev (day_direction)", asset=asset)
    # probability-aware extras
    for threshold in (0.5, 0.6, 0.7):
        taken = [r for r in records if r.jev_predicted == "up" and (r.jev_confidence or 0) >= threshold]
        if taken:
            score.notes += f" | up-calls at conf>= {threshold}: n={len(taken)}"
    return score


def evaluate_all(records: Iterable[DayRecord], *, settings: Settings) -> list[BaselineScore]:
    records = list(records)
    scores = [score_jev(records, settings=settings)]
    scores.append(score_predictions(records, b1_yesterday(records), settings=settings, name="B1 yesterday's label"))
    scores.append(score_predictions(records, b2_usdtirt_morning(records), settings=settings, name="B2 USDTIRT 08-12 up"))
    scores.append(score_predictions(records, b3_always_up(records), settings=settings, name="B3 always up (no costs)"))
    scores.append(score_predictions(records, b4_always_flat(records), settings=settings, name="B4 always flat"))
    b5 = b5_logistic(records)
    b5_calls: list[str | None] = []
    for probs in b5:
        b5_calls.append(max(probs.items(), key=lambda kv: kv[1])[0] if probs else None)
    scores.append(score_predictions(records, b5_calls, settings=settings, name="B5 logistic (walk-forward)"))
    # attach probabilities for the report's reliability section
    return scores


def b5_probabilities(records: Sequence[DayRecord]) -> list[dict[str, float] | None]:
    return b5_logistic(records)


__all__ = [
    "DayRecord",
    "BaselineScore",
    "FEATURE_NAMES",
    "CLASSES",
    "b1_yesterday",
    "b2_usdtirt_morning",
    "b3_always_up",
    "b4_always_flat",
    "b5_logistic",
    "b5_probabilities",
    "score_predictions",
    "score_jev",
    "evaluate_all",
]

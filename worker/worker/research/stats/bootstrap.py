"""Day-block bootstrap + day-level t-tests.

Why block by day: intraday events are heavily overlapping (a channel posting 40 messages in
an hour produces 40 non-independent observations). Aggregating to a DAY first and then
resampling/t-testing days keeps the effective sample honest.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np


@dataclass
class DayAggregate:
    day: date
    values: list[float]

    @property
    def mean(self) -> float:
        return float(np.mean(self.values)) if self.values else float("nan")

    @property
    def n(self) -> int:
        return len(self.values)


def day_means(samples: Sequence[tuple[date, float]]) -> list[DayAggregate]:
    """Group (day, value) pairs into per-day aggregates."""
    buckets: dict[date, list[float]] = {}
    for day, value in samples:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            continue
        buckets.setdefault(day, []).append(float(value))
    return [DayAggregate(day=d, values=buckets[d]) for d in sorted(buckets)]


def block_bootstrap_ci(
    samples: Sequence[tuple[date, float]],
    *,
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int = 20260401,
) -> tuple[float, float, float]:
    """95% CI of the (event-weighted) mean lift, resampling DAYS with replacement.

    Returns (mean, ci_low, ci_high). With fewer than two days the CI is a point.
    """
    days = day_means(samples)
    if not days:
        return (float("nan"), float("nan"), float("nan"))
    means = np.array([d.mean for d in days], dtype=float)
    weights = np.array([d.n for d in days], dtype=float)
    point = float(np.sum(means * weights) / np.sum(weights))
    if len(days) < 2:
        return (point, point, point)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(days), size=(n_boot, len(days)))
    boot_means = means[idx]
    boot_weights = weights[idx]
    draws = np.sum(boot_means * boot_weights, axis=1) / np.sum(boot_weights, axis=1)
    lo = float(np.quantile(draws, alpha / 2))
    hi = float(np.quantile(draws, 1 - alpha / 2))
    return (point, lo, hi)


def t_p_value(samples: Sequence[tuple[date, float]], *, two_sided: bool = True) -> float:
    """p-value of the day-mean lift being different from zero (one-sample t on day means).

    p-values are never reported alone: they exist to rank tests inside the FDR family.
    """
    days = day_means(samples)
    values = np.array([d.mean for d in days], dtype=float)
    if len(values) < 3 or np.allclose(values, 0.0):
        return 1.0
    sd = float(np.std(values, ddof=1))
    if sd == 0.0:
        return 0.0 if abs(float(np.mean(values))) > 0 else 1.0
    t_stat = float(np.mean(values)) / (sd / math.sqrt(len(values)))
    try:
        from scipy import stats  # noqa: PLC0415

        if two_sided:
            return float(2 * stats.t.sf(abs(t_stat), df=len(values) - 1))
        return float(stats.t.sf(t_stat, df=len(values) - 1))
    except ImportError:  # pragma: no cover - scipy is a declared dependency
        # Normal approximation fallback (documented, rarely used)
        return float(math.erfc(abs(t_stat) / math.sqrt(2)))


def sign_agreement(samples: Sequence[tuple[date, float]]) -> float:
    """Share of days whose mean lift has the same sign as the overall mean."""
    days = day_means(samples)
    if not days:
        return float("nan")
    overall = float(np.mean([d.mean for d in days]))
    if overall == 0:
        return float("nan")
    same = sum(1 for d in days if (d.mean > 0) == (overall > 0) and d.mean != 0)
    return same / len(days)


__all__ = ["DayAggregate", "day_means", "block_bootstrap_ci", "t_p_value", "sign_agreement"]

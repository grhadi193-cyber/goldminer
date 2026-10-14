"""Statistics module (7.2/7.3) — pure code: pandas/numpy/scipy only, never an LLM.

Everything here operates on LOG-RETURNS (never levels), reports effect sizes with
bootstrap 95% CIs (block bootstrap by day), and corrects for multiple comparisons with
Benjamini–Hochberg FDR q=0.10 across the PRE-REGISTERED family of tests:

    event study      channel x kind x direction x horizon
    agreement        k-of-N channels agreeing within 30m, k = 2..5
    lead/lag         channel x lag (grid of lags, minutes -60..+60)

Anything computed outside that family is labelled "exploratory, uncorrected".
"""

from .bootstrap import block_bootstrap_ci, day_means, t_p_value
from .fdr import Family, bh_correct
from .series import MeasurementSeries, forward_return_bps

__all__ = ["block_bootstrap_ci", "day_means", "t_p_value", "Family", "bh_correct",
           "MeasurementSeries", "forward_return_bps"]

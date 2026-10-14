"""Price ingestion providers.

One canonical schema for every source (`price_bars`) so a better provider can be swapped in
without touching research code (R9). Each provider is a class implementing `PriceProvider`.
"""

from .base import Bar, PriceProvider, bars_to_rows, coverage_stats, expected_bars_between
from .symbols import SERIES, decision_series

__all__ = [
    "Bar",
    "PriceProvider",
    "bars_to_rows",
    "coverage_stats",
    "expected_bars_between",
    "SERIES",
    "decision_series",
]

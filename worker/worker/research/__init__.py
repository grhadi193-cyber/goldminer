"""Research layer: anti-leakage reads, state builder, Jev, replay, statistics, backtest.

Hard rule for every module in this package (R3):
all reads go through `worker.research.asof.ReadContext`, which refuses to return anything
with an event time after `as_of` and refuses to touch rows outside the configured split.
"""

from .asof import ReadContext, split_bounds

__all__ = ["ReadContext", "split_bounds"]

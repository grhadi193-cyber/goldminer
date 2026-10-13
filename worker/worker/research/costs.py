"""Execution cost model (R5) — deterministic code, never an LLM.

Two related but distinct numbers, both reported explicitly:

  * `signal_cost_bps(asset)`  = spread_bps + slippage_bps
        the one-sided drag subtracted from every reported signal lift and every event-study
        lift (T8 checks `lift == gross - spread - slippage` for the configured bps)
  * `round_trip_cost_bps(asset)` = spread_bps + 2 * slippage_bps
        the full drag of a simulated paper round trip: enter at the ask
        (mid + spread/2 + slippage), exit at the bid (mid - spread/2 - slippage)

Defaults (30/15 bps gold, 50/25 bps coin) are placeholders until the trader calibrates them
from real quotes — the whole point is that NET numbers are the only meaningful ones.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings

DEFAULT_ASSET = "gold"


@dataclass(frozen=True)
class CostModel:
    spread_bps: float
    slippage_bps: float

    @property
    def signal_cost_bps(self) -> float:
        return self.spread_bps + self.slippage_bps

    @property
    def round_trip_cost_bps(self) -> float:
        return self.spread_bps + 2.0 * self.slippage_bps

    @property
    def half_spread_bps(self) -> float:
        return self.spread_bps / 2.0

    def entry_price(self, mid: float) -> float:
        """What a buyer actually pays."""
        return mid * (1.0 + (self.half_spread_bps + self.slippage_bps) / 1e4)

    def exit_price(self, mid: float) -> float:
        """What a seller actually receives."""
        return mid * (1.0 - (self.half_spread_bps + self.slippage_bps) / 1e4)

    def as_dict(self) -> dict[str, float]:
        return {
            "spread_bps": self.spread_bps,
            "slippage_bps": self.slippage_bps,
            "signal_cost_bps": self.signal_cost_bps,
            "round_trip_cost_bps": self.round_trip_cost_bps,
        }


def cost_model(settings: Settings, asset: str = DEFAULT_ASSET) -> CostModel:
    table = settings.cost_bps
    entry = table.get(asset) or table[DEFAULT_ASSET]
    return CostModel(spread_bps=float(entry["spread_bps"]), slippage_bps=float(entry["slippage_bps"]))


def net_bps(gross_bps: float, settings: Settings, asset: str = DEFAULT_ASSET, *, one_sided: bool = True) -> float:
    """Net return in bps. `one_sided=True` is the convention for reported signal lifts."""
    cm = cost_model(settings, asset)
    drag = cm.signal_cost_bps if one_sided else cm.round_trip_cost_bps
    return gross_bps - drag


def log_return_bps(entry: float, exit_price: float) -> float:
    """Log-return in bps (NO cost applied — callers apply `net_bps` explicitly)."""
    if entry <= 0 or exit_price <= 0:
        raise ValueError("prices must be positive")
    import math

    return math.log(exit_price / entry) * 1e4


__all__ = ["CostModel", "cost_model", "net_bps", "log_return_bps", "DEFAULT_ASSET"]

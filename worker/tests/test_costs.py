"""T8 — cost maths are golden-tested.

Pinned convention: `gross_bps` is the frictionless MID-to-MID move, `net_bps = gross_bps -
round_trip_cost`, and the ask/bid fill prices are reported separately. Signal-level lifts use
the one-sided cost (`spread + slippage`); a paper round trip uses `spread + 2*slippage`.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from worker.config import Settings
from worker.research.costs import CostModel, cost_model, log_return_bps, net_bps
from worker.research.replay import simulate_paper_trade

FIXTURE = Path(__file__).parent / "fixtures" / "costs_golden.json"


def _settings(**over) -> Settings:
    base = {
        "gm_demo_mode": 0,
        "spread_bps_gold": 30.0,
        "slippage_bps_gold": 15.0,
        "spread_bps_coin": 50.0,
        "slippage_bps_coin": 25.0,
        "daily_loss_cap_bps": 200.0,
        "stop_loss_bps": 100.0,
        "position_size_g": 1000.0,
        "start_date": date(2026, 4, 1),
    }
    base.update(over)
    return Settings(**base)


def test_signal_cost_is_spread_plus_slippage():
    cm = cost_model(_settings(), "gold")
    assert cm.spread_bps == 30.0 and cm.slippage_bps == 15.0
    assert cm.signal_cost_bps == 45.0            # T8: gross - spread - slippage
    assert cm.round_trip_cost_bps == 60.0        # enter at ask, exit at bid


def test_reported_lift_equals_gross_minus_spread_minus_slippage():
    settings = _settings()
    for gross in (-120.0, -45.0, 0.0, 12.5, 45.0, 300.0):
        assert net_bps(gross, settings, "gold") == pytest.approx(gross - 30.0 - 15.0)
    for gross in (10.0, 80.0):
        assert net_bps(gross, settings, "coin") == pytest.approx(gross - 50.0 - 25.0)


@pytest.mark.parametrize("case", json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"])
def test_trade_simulation_matches_golden_fixture(case):
    """The expected values in the fixture are recomputed here from first principles."""
    settings = _settings()
    cm = cost_model(settings, case.get("asset", "gold"))
    # gross is the MID-to-MID move of the held window; the round trip is charged once in
    # `cost_bps`. Computing gross from the ask/bid fill prices and subtracting the spread again
    # would double count it (that bug is why this fixture is explicit about levels vs fills).
    expected_entry_fill, expected_exit_level, expected_exit_fill = expected_values_for(case, cm)
    expected_gross = (expected_exit_level / case["entry_mid"] - 1.0) * 1e4
    expected_net = expected_gross - cm.round_trip_cost_bps

    trade = simulate_paper_trade(
        settings=settings,
        entry_ts=datetime(2026, 5, 6, 4, 30, tzinfo=timezone.utc),
        entry_mid=case["entry_mid"],
        exit_ts=datetime(2026, 5, 6, 18, 30, tzinfo=timezone.utc),
        exit_mid=case["exit_mid"],
        session_low=case.get("session_low"),
        session_high=case.get("session_high"),
        asset=case.get("asset", "gold"),
        day_pnl_bps=case.get("day_pnl_bps", 0.0),
    )
    assert trade.exit_reason == case["expected_exit_reason"]
    assert trade.entry_price == pytest.approx(expected_entry_fill)
    assert trade.exit_price == pytest.approx(expected_exit_fill)
    assert trade.detail["exit_level"] == pytest.approx(expected_exit_level)
    assert trade.detail["gross_basis"] == "mid_to_mid"
    assert trade.gross_bps == pytest.approx(expected_gross, rel=1e-9)
    assert trade.cost_bps == pytest.approx(cm.round_trip_cost_bps)
    assert trade.cost_bps == pytest.approx(cm.spread_bps + 2 * cm.slippage_bps)
    assert trade.net_bps == pytest.approx(expected_net, rel=1e-9)
    assert trade.net_bps == pytest.approx(trade.gross_bps - trade.cost_bps, rel=1e-9)
    assert trade.pnl_toman == pytest.approx(trade.size_g * trade.entry_price * trade.net_bps / 1e4, rel=1e-9)
    if case["expected_exit_reason"] == "stop":
        assert trade.detail["session_low"] <= trade.detail["stop_price"]


def expected_values_for(case: dict, cm: CostModel) -> tuple[float, float, float]:
    """Independent recomputation of the documented rule (not a copy of the implementation).

    Returns `(entry_fill, exit_level, exit_fill)` where `exit_level` is a MID level and
    `exit_fill` is what the trader actually gets.
    """
    half = (cm.spread_bps / 2 + cm.slippage_bps) / 1e4
    entry_fill = case["entry_mid"] * (1 + half)
    stop_level = entry_fill * (1 - 100.0 / 1e4)
    cap_room = max(0.0, 200.0 - max(0.0, -case.get("day_pnl_bps", 0.0)))
    cap_level = entry_fill * (1 - (cap_room + cm.round_trip_cost_bps) / 1e4)
    # On the way down the HIGHER level is touched first and is therefore the binding one.
    binding = stop_level if stop_level >= cap_level else cap_level
    if case["expected_exit_reason"] in ("stop", "cap"):
        return entry_fill, binding, binding * (1 - half)
    if case["expected_exit_reason"] == "flat_2200":
        assert case.get("session_low", -1) > binding, "fixture case would actually hit an exit level"
        return entry_fill, case["exit_mid"], case["exit_mid"] * (1 - half)
    raise AssertionError(f"unknown expected exit reason {case['expected_exit_reason']!r}")


def test_log_return_is_cost_free_and_positive_for_a_rise():
    assert log_return_bps(100.0, 101.0) == pytest.approx(99.503, rel=1e-4)
    with pytest.raises(ValueError):
        log_return_bps(0.0, 1.0)


def test_costs_are_per_asset():
    settings = _settings()
    assert cost_model(settings, "coin").signal_cost_bps == 75.0
    assert cost_model(settings, "usdtirt").signal_cost_bps == 45.0
    assert cost_model(settings, "xauusd").signal_cost_bps == 10.0
    assert cost_model(settings, "unknown").signal_cost_bps == 45.0  # falls back to gold defaults

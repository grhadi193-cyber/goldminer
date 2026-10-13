"""Deterministic money management (R6). No LLM ever takes part in these decisions.

Jev's `final_action` is an INPUT. The verdict below — BUY / WAIT / AVOID — is computed here
from explicit checks, each of which is recorded with its numbers so the UI can show which
gate blocked a trade and the report can attribute every skip.

Checks, in order:
  session_window  08:00–22:00 Tehran only
  daily_loss_cap  after the cap is hit, no new trades (forced close still honoured)
  spread          spread must clear MAX_SPREAD_BPS_TRADEABLE
  confidence      Jev confidence must clear JEV_MIN_CONFIDENCE
  jev_action      Jev's own final_action must be "buy"
  direction       the modal day_direction must be "up" (long-only Phase 1)
  pumping         a pumped/contradictory signal set is a veto, not a discount
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from typing import Any

from ..config import Settings


@dataclass
class GateCheck:
    name: str
    passed: bool
    detail: str
    hard_veto: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail, "hard_veto": self.hard_veto}


@dataclass
class GateVerdict:
    action: str  # BUY | WAIT | AVOID
    reason: str
    checks: list[GateCheck] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    size_g: float = 0.0
    confidence: float | None = None

    @property
    def entered(self) -> bool:
        return self.action == "BUY"

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "blocked_by": self.blocked_by,
            "size_g": self.size_g,
            "confidence": self.confidence,
            "checks": [c.as_dict() for c in self.checks],
        }


def evaluate_gates(
    *,
    settings: Settings,
    now_local: time,
    spread_bps: float,
    jev_action: str | None,
    jev_confidence: float | None,
    day_direction: str | None,
    pumping_probability: float | None,
    contradiction_probability: float | None,
    day_pnl_bps: float = 0.0,
    pumping_veto_threshold: float = 0.6,
    contradiction_veto_threshold: float = 0.7,
) -> GateVerdict:
    checks: list[GateCheck] = []
    open_t, flat_t = settings.open_time, settings.flat_time

    in_session = open_t <= now_local <= flat_t
    checks.append(
        GateCheck("session_window", in_session,
                  f"local time {now_local.strftime('%H:%M')} vs {settings.open_at_local}-{settings.flat_at_local}",
                  hard_veto=True)
    )

    cap_ok = day_pnl_bps > -abs(settings.daily_loss_cap_bps)
    checks.append(
        GateCheck("daily_loss_cap", cap_ok,
                  f"day pnl {day_pnl_bps:.1f} bps vs cap -{settings.daily_loss_cap_bps:.0f} bps", hard_veto=True)
    )

    spread_ok = spread_bps <= settings.max_spread_bps_tradeable
    checks.append(
        GateCheck("spread", spread_ok,
                  f"spread {spread_bps:.1f} bps vs max {settings.max_spread_bps_tradeable:.0f} bps")
    )

    conf_ok = jev_confidence is not None and jev_confidence >= settings.jev_min_confidence
    checks.append(
        GateCheck("confidence", conf_ok,
                  f"confidence {('%.3f' % jev_confidence) if jev_confidence is not None else 'n/a'} "
                  f"vs min {settings.jev_min_confidence:.2f}")
    )

    action_ok = (jev_action or "").lower() == "buy"
    checks.append(GateCheck("jev_action", action_ok, f"Jev final_action={jev_action!r}"))

    direction_ok = (day_direction or "").lower() == "up"
    checks.append(
        GateCheck("direction", direction_ok,
                  f"modal day_direction={day_direction!r} (long-only Phase 1)", hard_veto=(day_direction or '').lower() == "down")
    )

    pumping_veto = pumping_probability is not None and pumping_probability >= pumping_veto_threshold
    checks.append(
        GateCheck("pumping", not pumping_veto,
                  f"coordinated_pumping P(yes)={pumping_probability} (veto >= {pumping_veto_threshold})",
                  hard_veto=True)
    )

    contradiction_veto = contradiction_probability is not None and contradiction_probability >= contradiction_veto_threshold
    checks.append(
        GateCheck("contradiction", not contradiction_veto,
                  f"credible_contranding_news P(yes)={contradiction_probability} "
                  f"(veto >= {contradiction_veto_threshold})", hard_veto=True)
    )

    blocked = [c.name for c in checks if not c.passed]
    hard_blocked = [c.name for c in checks if not c.passed and c.hard_veto]

    if not blocked:
        verdict = GateVerdict("BUY", "all gates passed", checks, [], float(settings.position_size_g), jev_confidence)
    elif hard_blocked:
        verdict = GateVerdict("AVOID", f"hard veto: {', '.join(hard_blocked)}", checks, blocked, 0.0, jev_confidence)
    elif not direction_ok:
        verdict = GateVerdict("AVOID", "day direction is not up (long-only)", checks, blocked, 0.0, jev_confidence)
    else:
        verdict = GateVerdict("WAIT", f"gates not met: {', '.join(blocked)}", checks, blocked, 0.0, jev_confidence)
    return verdict


__all__ = ["GateCheck", "GateVerdict", "evaluate_gates"]

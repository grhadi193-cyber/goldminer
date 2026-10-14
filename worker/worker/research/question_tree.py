"""The FIXED Jev question tree (§5). Exact ids, exact wording, no raw Persian ever.

Only three question types exist in the System One API — "noul", "choice", "score".
There is NO boolean type, so yes/no questions are asked as `noul` (probability of yes).

All five questions go in ONE request against the SAME state; Jev answers each independently.
Nothing here computes numbers: the state carries pre-formatted values.
"""

from __future__ import annotations

from typing import Any

QUESTION_IDS = (
    "day_direction",
    "credible_contrading_news",
    "coordinated_pumping",
    "signal_strength",
    "final_action",
)


def build_questions() -> dict[str, Any]:
    return {
        "day_direction": {
            "type": "choice",
            "instructions": (
                "Based on this state, will the domestic 18k gold price END the trading day UP, DOWN, "
                "or FLAT relative to the 08:00 decision price? FLAT means the move is within the flat "
                "band shown in `history.flat_band_bps`."
            ),
            "criteria": {
                "up": "Gold closes the day higher than the decision price by more than the flat band.",
                "down": "Gold closes the day lower than the decision price by more than the flat band.",
                "flat": "Gold closes the day within the flat band of the decision price.",
            },
        },
        "credible_contrading_news": {
            "type": "noul",
            "instructions": (
                "Do the signal snippets contain credible news items that contradict each other about "
                "gold's direction?"
            ),
            "criteria": {
                "true": "At least two specific, sourced news items point in opposite directions.",
                "false": "No such contradiction, or contradictions are only rumors/opinions.",
            },
        },
        "coordinated_pumping": {
            "type": "noul",
            "instructions": (
                "Do the recent signals look coordinated or pumped (multiple channels pushing the same "
                "direction with high intensity within a short window, with quoted prices far above the "
                "real market price)?"
            ),
            "criteria": {
                "true": "Synchronized, high-intensity, same-direction quoting that deviates clearly "
                        "from real price.",
                "false": "Normal independent chatter.",
            },
        },
        "signal_strength": {
            "type": "score",
            "instructions": (
                "How strong is the aggregate evidence in this state for gold moving UP today (versus "
                "down)? Rate the state, not your own forecast."
            ),
            "criteria": [
                "1 Clear bearish dominance: bearish signals dominate and USD/ounce evidence is flat or negative",
                "2 Lean bearish",
                "3 Mixed / neutral",
                "4 Lean bullish",
                "5 Clear bullish dominance: bullish signals dominate and USD/ounce evidence supports them",
            ],
        },
        "final_action": {
            "type": "choice",
            "instructions": (
                "Given `day_direction`, `signal_strength`, the pumping and contradiction flags, and the "
                "cost thresholds in the state, what should the paper trader do at this decision time?"
            ),
            "criteria": {
                "buy": "Evidence supports a long paper position and it clears cost/confidence gates.",
                "wait": "Evidence is mixed, weak, or gates are not met.",
                "avoid": "Pumping/contradiction flags are high, or spread is too wide, or evidence "
                         "clearly says down.",
            },
        },
    }


def question_types() -> dict[str, str]:
    return {qid: q["type"] for qid, q in build_questions().items()}


__all__ = ["QUESTION_IDS", "build_questions", "question_types"]

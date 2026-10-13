"""Benjamini–Hochberg FDR (q = 0.10) over an explicitly enumerated family of tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

DEFAULT_Q = 0.10


@dataclass
class Family:
    """A pre-registered family of tests. Members are added before correction runs."""

    name: str
    q: float = DEFAULT_Q
    members: list[dict[str, Any]] = field(default_factory=list)

    def add(self, key: tuple, p_value: float, **extra: Any) -> None:
        p = 1.0 if p_value is None else max(0.0, min(1.0, float(p_value)))
        self.members.append({"key": list(key), "p": p, **extra})

    def __len__(self) -> int:
        return len(self.members)


def bh_correct(family: Family) -> list[dict[str, Any]]:
    """Attach `q_value` and `fdr_significant` to each member (BH step-up procedure)."""
    m = len(family.members)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: family.members[i]["p"])
    q_values = [1.0] * m
    prev = 1.0
    for rank_from_end, idx in enumerate(reversed(order), start=1):
        rank = m - rank_from_end + 1
        value = family.members[idx]["p"] * m / rank
        prev = min(prev, value)
        q_values[idx] = min(1.0, prev)
    out: list[dict[str, Any]] = []
    for i, member in enumerate(family.members):
        enriched = {**member, "q_value": round(q_values[i], 6), "fdr_significant": q_values[i] <= family.q,
                    "family": family.name, "family_size": m, "q_threshold": family.q}
        out.append(enriched)
    return out


def family_summary(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    significant = [r for r in rows if r.get("fdr_significant")]
    return {
        "family": rows[0]["family"] if rows else None,
        "size": len(rows),
        "significant": len(significant),
        "min_q": min((r["q_value"] for r in rows), default=None),
    }


__all__ = ["Family", "bh_correct", "family_summary", "DEFAULT_Q"]

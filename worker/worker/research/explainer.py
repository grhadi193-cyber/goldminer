"""The ONE strong LLM in the system (R1d).

Two jobs, both at the very END of the pipeline, neither able to influence a decision:
  1. write plain-English explanations of numeric results it receives as JSON
  2. propose NEW hypotheses, which are inserted into `hypotheses` with status 'proposed'

Hard limits enforced in code:
  * it never sees raw market data, only the numbers the stats module computed
  * its output cannot gate, filter, or re-rank anything (no caller reads it except the report)
  * every proposed hypothesis is stored and later tested by CODE (see hypothesis.py)
  * if EXPLAINER_MODEL is empty, the whole step is skipped — the report still builds
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx
import psycopg

from ..config import Settings
from ..db import repo
from ..jsonutil import jsonb
from ..util.ratelimit import Backoff

log = logging.getLogger(__name__)

MAX_ARTIFACT_CHARS = 60_000

SYSTEM_PROMPT = """You are the explainer for a quantitative research pipeline about Iranian gold.
You receive ONLY numeric results (JSON) computed by deterministic code. Rules:
1. Never invent numbers. Quote only values present in the JSON. If a value is missing, say so.
2. Separate what the numbers show from what they cannot show. Sample sizes and confidence
   intervals bound every claim; a ~40-day out-of-sample window cannot settle a daily call.
3. Report net-of-cost figures as the headline; gross figures are context only.
4. Answer in compact Markdown with these sections, in this order:
   "## What the numbers say", "## What they cannot say", "## Hypotheses worth testing".
5. In the hypotheses section propose 3-6 hypotheses that are FALSIFIABLE BY CODE from this
   data (a specific statistic, a stated direction, and a measurable threshold). One per line,
   prefixed with "- ".
6. You do not make trading decisions and you never recommend a position size."""


@dataclass
class ExplainerOutput:
    narrative: str
    hypotheses: list[str]
    model: str
    skipped: bool = False
    reason: str = ""


def _summary_payload(artifact: dict[str, Any]) -> dict[str, Any]:
    """Trim the artifact to the numeric essentials the LM is allowed to see."""
    scoring = artifact.get("scoring") or {}
    channels = [
        {
            "username": c.get("username"),
            "label": c.get("label"),
            "leads_lag_minutes": c.get("leads_lag_minutes"),
            "n_events": c.get("n_events"),
            "lifts_net_bps": c.get("lifts_net_bps"),
            "lifts_gross_bps": c.get("lifts_gross_bps"),
            "ci_95": c.get("ci_95"),
            "q_values": c.get("q_values"),
            "pumper_flag": c.get("pumper_flag"),
            "quoted_bias_pct": c.get("quoted_bias_pct"),
        }
        for c in scoring.get("channels", [])
    ]
    return {
        "split": scoring.get("split"),
        "window": scoring.get("window"),
        "measurement_series": scoring.get("measurement_series"),
        "family": scoring.get("family"),
        "days_covered": scoring.get("days_covered"),
        "days_missing": scoring.get("days_missing"),
        "measure_coverage": scoring.get("measure_coverage"),
        "channels": channels,
        "agreement_events": scoring.get("agreement"),
        "baselines": artifact.get("baselines"),
        "calibration": {
            "n": (artifact.get("calibration") or {}).get("n"),
            "brier": (artifact.get("calibration") or {}).get("brier"),
            "ece": (artifact.get("calibration") or {}).get("ece"),
            "reliability": (artifact.get("calibration") or {}).get("reliability"),
            "sweep": (artifact.get("calibration") or {}).get("sweep"),
        },
        "backtest": {
            "days": (artifact.get("summary") or {}).get("days"),
            "trades": (artifact.get("summary") or {}).get("trades"),
            "net_bps_total": (artifact.get("summary") or {}).get("net_bps_total"),
        },
        "costs": (artifact.get("config") or {}).get("costs"),
    }


async def _call_openrouter(settings: Settings, payload: dict[str, Any]) -> tuple[str, str]:
    body = {
        "model": settings.explainer_model,
        "temperature": 0.2,
        "max_tokens": 1600,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "Numeric results JSON:\n```json\n"
                                        + json.dumps(payload, ensure_ascii=False)[:MAX_ARTIFACT_CHARS]
                                        + "\n```"},
        ],
    }
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/grhadi193-cyber/goldminer",
        "X-Title": "Mishkah explainer",
    }
    backoff = Backoff(base=2.0, factor=2.0, max_sleep=45.0)
    async with httpx.AsyncClient(timeout=120.0) as client:
        for attempt in range(1, 4):
            resp = await client.post(f"{settings.openrouter_base_url}/chat/completions", json=body, headers=headers)
            if resp.status_code in (429, 500, 502, 503, 504, 529):
                log.warning("explainer: HTTP %s (attempt %d)", resp.status_code, attempt)
                await backoff.sleep(attempt)
                continue
            resp.raise_for_status()
            data = resp.json()
            text = data["choices"][0]["message"]["content"] or ""
            return text, str(data.get("model") or settings.explainer_model)
    raise RuntimeError("explainer model unavailable after retries")


def extract_hypotheses(markdown: str) -> list[str]:
    """Pull the '- ' bullet lines out of the hypotheses section."""
    lines = markdown.splitlines()
    out: list[str] = []
    in_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("## "):
            in_section = "hypothes" in stripped.lower()
            continue
        if in_section and stripped.startswith("- "):
            text = stripped[2:].strip()
            if len(text) > 12:
                out.append(text)
    return out


def store_hypotheses(conn: psycopg.Connection, hypotheses: list[str], *, model: str, run_id: int | None = None) -> int:
    inserted = 0
    for text in hypotheses:
        row = conn.execute(
            """
            insert into hypotheses (text, proposed_by_model, status, run_id)
            values (%s, %s, 'proposed', %s)
            on conflict (text, proposed_by_model) do nothing
            returning id
            """,
            (text, model, run_id),
        ).fetchone()
        if row:
            inserted += 1
    repo.audit(conn, "worker:explainer", "hypotheses_proposed",
               {"n": len(hypotheses), "inserted": inserted, "model": model})
    conn.commit()
    return inserted


async def explain(
    conn: psycopg.Connection, settings: Settings, artifact: dict[str, Any], *, run_id: int | None = None
) -> ExplainerOutput:
    """Run the explainer (or skip cleanly when no model/key is configured)."""
    if not settings.explainer_model or not settings.openrouter_api_key:
        return ExplainerOutput(
            narrative="", hypotheses=[], model="", skipped=True,
            reason="EXPLAINER_MODEL/OPENROUTER_API_KEY not configured — narrative skipped by design",
        )
    payload = _summary_payload(artifact)
    try:
        text, model = await _call_openrouter(settings, payload)
    except Exception as exc:  # noqa: BLE001 - a narrative failure must not break a report
        log.warning("explainer failed: %s", exc)
        return ExplainerOutput(narrative="", hypotheses=[], model=settings.explainer_model, skipped=True,
                               reason=f"explainer error: {type(exc).__name__}: {exc}")
    hypotheses = extract_hypotheses(text)
    store_hypotheses(conn, hypotheses, model=model, run_id=run_id)
    return ExplainerOutput(narrative=text, hypotheses=hypotheses, model=model)


__all__ = ["ExplainerOutput", "explain", "extract_hypotheses", "store_hypotheses", "_summary_payload"]

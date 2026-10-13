"""Strict schema for the ONE structured record the cheap LLM is allowed to emit (R1a).

    { "topic": str, "direction": "up|down|flat|none", "kind": "news|rumor|price_quote|opinion",
      "intensity": 1-5, "quoted_price": number|null, "quoted_asset": str|null,
      "confidence": 0-1, "english_text": str, "persian_gloss": str|null }

Nothing else is accepted as a valid extraction. Invalid output is retried once with a
stricter reminder, then the message is marked `extraction_failed` and the pipeline moves on.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

log = logging.getLogger(__name__)

Direction = Literal["up", "down", "flat", "none"]
Kind = Literal["news", "rumor", "price_quote", "opinion"]

STRICT_EXTRA_KEYS = os.environ.get("EXTRACTION_STRICT_EXTRA_KEYS", "0") not in ("0", "", "false")


class ExtractionRecord(BaseModel):
    """The contract. Field order mirrors the prompt so diffs stay readable."""

    model_config = ConfigDict(extra="forbid" if STRICT_EXTRA_KEYS else "ignore")

    topic: str = Field(min_length=1, max_length=200)
    direction: Direction
    kind: Kind
    intensity: int = Field(ge=1, le=5)
    quoted_price: float | None = None
    quoted_asset: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    english_text: str = Field(min_length=1, max_length=4000)
    persian_gloss: str | None = None

    @field_validator("topic", "english_text")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v

    @field_validator("quoted_asset")
    @classmethod
    def _norm_asset(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower().replace(" ", "_").replace("-", "_")
        return v or None


class ExtractionValidationError(ValueError):
    """Raised when the model output cannot be coerced into `ExtractionRecord`."""

    def __init__(self, message: str, *, raw: str = "") -> None:
        self.raw = raw
        super().__init__(message)


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json_object(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model answer (tolerates code fences/prose)."""
    if not text or not text.strip():
        raise ExtractionValidationError("empty model answer", raw=text)
    candidates: list[str] = []
    fenced = _JSON_BLOCK.search(text)
    if fenced:
        candidates.append(fenced.group(1).strip())
    candidates.append(text.strip())
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ExtractionValidationError("no JSON object found in model answer", raw=text)


def parse_extraction(text: str) -> ExtractionRecord:
    payload = extract_json_object(text)
    if not STRICT_EXTRA_KEYS and "extra" in str(ExtractionRecord.model_config):
        pass
    try:
        return ExtractionRecord.model_validate(payload)
    except ValidationError as exc:
        raise ExtractionValidationError(f"schema validation failed: {exc.errors()[:4]}", raw=text) from exc


def known_glossary_assets() -> set[str]:
    """Canonical quoted_asset spellings the state builder understands."""
    return {
        "gold_18k_toman_per_gram",
        "abshodeh",
        "emami_coin",
        "whole_coin",
        "half_coin",
        "quarter_coin",
        "usdtirt",
        "usd_free_market",
        "nima_dollar",
        "xauusd_ounce",
        "gold_fund",
    }

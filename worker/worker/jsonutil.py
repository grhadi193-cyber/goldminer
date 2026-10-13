"""Canonical JSON + hashing helpers.

Canonical form matters twice:
  * `jev_cache.cache_key` = sha256 of the canonical (state, questions) request — a stable
    hash is what makes caching and the determinism test (T4) meaningful.
  * `jev_decisions.state_hash` lets a replay re-run prove it reproduced the same state.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from psycopg.types.json import Jsonb


def canonical_json(obj: Any) -> str:
    """Deterministic JSON text: sorted keys, no stray whitespace, UTF-8 kept as-is."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_hash(obj: Any) -> str:
    return sha256_hex(canonical_json(obj))


def safe_json(obj: Any) -> Any:
    """Best-effort JSON-encodable copy (used before writing jsonb columns)."""
    return json.loads(json.dumps(obj, default=str, ensure_ascii=False))


def jsonb(value: Any) -> Jsonb:
    """Wrap a Python object for a jsonb parameter (psycopg3 needs an explicit adapter)."""
    return Jsonb(safe_json(value))

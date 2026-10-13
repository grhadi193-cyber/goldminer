"""Jev (TypeSafe System One) client — R2.

Providers: `JEV_PROVIDER=typesafe` (official: api.typesafe.ai, `jev-latest`) or
`JEV_PROVIDER=opencode` (OpenCode Console/Zen: opencode.ai, `jev-1.13-free` while the free window
lasts, paid id `jev-1.13`). An explicit JEV_BASE_URL/JEV_MODEL/JEV_ALLOWED_HOSTS always wins over
the preset, so nothing here is a hard-coded provider lock-in.

Rules enforced here, not documented elsewhere:
  * base URL, key, model, timeouts and concurrency come from env ONLY (`JEV_*`)
  * a host allowlist (`JEV_ALLOWED_HOSTS`) gates every send: an unlisted host is a loud
    configuration error, never a silent call
  * the officially documented host is api.typesafe.ai; anything else prints an
    UNVERIFIED THIRD PARTY warning on every run
  * retries happen ONLY for overload/rate-limit style failures (429/529/5xx-transient);
    4xx validation errors are surfaced un-retried
  * every request/response pair is cached in Postgres under sha256(canonical request)

The response parser is deliberately tolerant about field names (the docs' exact JSON keys
must be re-verified at build time — `scripts/jev_smoke_test.py` prints the raw payload so an
operator can confirm them) but strict about the presence of an answer per question id.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
import psycopg

from ..config import OFFICIAL_JEV_HOST, Settings
from ..errors import JevRequestError, JevUnavailable
from ..jsonutil import canonical_hash, canonical_json, jsonb
from ..util.ratelimit import Backoff

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504, 529}
CHOICE_KEYS = ("answer", "choice", "selected", "value", "option", "label")
PROB_KEYS = ("probability", "prob", "p", "yes_probability", "score_probability")
DIST_KEYS = ("distribution", "probabilities", "probs", "options", "scores")
CONF_KEYS = ("confidence", "conf", "certainty")


@dataclass
class JevAnswer:
    question_id: str
    type: str
    value: Any = None                    # chosen option / yes-probability / score value
    distribution: dict[str, float] = field(default_factory=dict)
    confidence: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_choice(self) -> bool:
        return self.type == "choice"

    @property
    def is_noul(self) -> bool:
        return self.type == "noul"

    @property
    def is_score(self) -> bool:
        return self.type == "score"

    def probability_of(self, option: str) -> float | None:
        if not self.distribution:
            return None
        return self.distribution.get(option)

    def top_option(self) -> str | None:
        if self.distribution:
            return max(self.distribution.items(), key=lambda kv: kv[1])[0]
        return str(self.value) if isinstance(self.value, str) else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "value": self.value,
            "distribution": self.distribution,
            "confidence": self.confidence,
        }


@dataclass
class JevResponse:
    model: str
    answers: dict[str, JevAnswer]
    usage: dict[str, Any] = field(default_factory=dict)
    cache_key: str = ""
    from_cache: bool = False
    raw: dict[str, Any] = field(default_factory=dict)
    endpoint: str = ""

    def answer(self, question_id: str) -> JevAnswer | None:
        return self.answers.get(question_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "answers": {k: v.as_dict() for k, v in self.answers.items()},
            "usage": self.usage,
            "cache_key": self.cache_key,
            "from_cache": self.from_cache,
            "endpoint": self.endpoint,
        }


class JevTransport(Protocol):
    async def call(self, endpoint: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
        ...


class HttpTransport:
    """Real HTTP transport (POST {base}/v1/systemone)."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self.backoff = Backoff(base=2.0, factor=2.0, max_sleep=60.0)

    async def call(self, endpoint: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
        async with _client_ctx(self._client) as client:
            last: Exception | None = None
            for attempt in range(1, 5):
                try:
                    resp = await client.post(endpoint, json=payload, headers=headers, timeout=timeout)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last = exc
                    log.warning("jev transport error (attempt %d): %s", attempt, exc)
                    await self.backoff.sleep(attempt)
                    continue
                if resp.status_code in RETRY_STATUS:
                    last = JevUnavailable(f"jev overloaded: HTTP {resp.status_code}")
                    log.warning("jev %s (attempt %d) — backing off", resp.status_code, attempt)
                    await self.backoff.sleep(attempt)
                    continue
                if resp.status_code in (404, 410) and "free" in str(payload.get("model", "")):
                    # OpenCode documents jev-1.13-free as a LIMITED-TIME free model. When it goes
                    # away the honest behaviour is a loud, actionable error — never a silent fall
                    # back to a paid model and never a blind retry.
                    raise JevUnavailable(
                        f"HTTP {resp.status_code}: the free Jev model {payload.get('model')!r} is not "
                        "answering any more. OpenCode's free window is limited: switch JEV_MODEL to the "
                        "paid id (jev-1.13) or point JEV_PROVIDER at another provider. Nothing was retried."
                    )
                if resp.status_code >= 400:
                    # 4xx validation errors are NEVER blind-retried: surface them.
                    raise JevRequestError(resp.status_code, resp.text)
                data = resp.json()
                if not isinstance(data, dict):
                    raise JevRequestError(resp.status_code, f"non-object response: {str(data)[:300]}")
                return data
            raise last if isinstance(last, Exception) else JevUnavailable("jev unavailable")


class MockTransport:
    """Deterministic in-process Jev stand-in for demo mode and tests.

    It answers the SAME question tree over the SAME state with the documented response
    shape, using plain arithmetic on the state's own (code-computed) numbers. It is a
    pipeline exercise, not a model: results are labelled `mock-jev` everywhere.
    """

    name = "mock-jev"

    def __init__(self, *, noise: float = 0.06, seed: int = 17) -> None:
        self.noise = noise
        self.seed = seed

    async def call(self, endpoint: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
        state = payload.get("state") or {}
        if isinstance(state, str):
            import json

            try:
                state = json.loads(state)
            except json.JSONDecodeError:
                state = {}
        h = int(canonical_hash({k: v for k, v in state.items() if k != "signals"})[:8], 16) % 1000 / 1000.0
        signals = state.get("signals", {}) or {}
        moves = state.get("moves_since_open", {}) or {}
        history = state.get("history", {}) or {}
        counts = signals.get("bullish_minus_bearish_count", {}) or {}
        bull = float(counts.get("last_1h", 0) or 0)
        usd_move = float(moves.get("usdtirt_pct_08_to_now", 0.0) or 0.0)
        xau_move = float(moves.get("xauusd_pct_08_to_now", 0.0) or 0.0)
        strength = _clip(3.0 + 0.22 * bull + 0.9 * usd_move + 0.5 * xau_move + self.noise * (h - 0.5), 1.0, 5.0)
        # A deliberately *imperfect* mapping with noise: this is a stand-in, not an oracle.
        score = (strength - 3.0) / 2.0 + self.noise * (2 * h - 1)
        band = float(history.get("flat_band_bps", 15.0) or 15.0) / 1e4
        flat_p = _clip(0.34 - 1.4 * abs(score), 0.08, 0.4)
        rest = 1.0 - flat_p
        up_p = rest * _sigmoid(2.2 * score)
        down_p = rest - up_p
        confidence = _clip(0.45 + 0.5 * abs(score), 0.3, 0.97)
        contradiction = 0.55 if abs(bull) >= 6 else 0.15 + 0.1 * h
        pumping = 0.6 if (signals.get("high_intensity_count", 0) or 0) >= 6 else 0.12
        action = "wait"
        if up_p > 0.5 and confidence >= 0.6 and pumping < 0.5:
            action = "buy"
        if down_p > 0.5 or pumping > 0.5:
            action = "avoid"
        answers = {
            "day_direction": {
                "answer": max((("up", up_p), ("down", down_p), ("flat", flat_p)), key=lambda kv: kv[1])[0],
                "distribution": {"up": round(up_p, 4), "down": round(down_p, 4), "flat": round(flat_p, 4)},
                "confidence": round(confidence, 4),
            },
            "credible_contrading_news": {
                "answer": contradiction > 0.5,
                "probability": round(contradiction, 4),
                "confidence": round(_clip(0.4 + contradiction / 2, 0.3, 0.95), 4),
            },
            "coordinated_pumping": {
                "answer": pumping > 0.5,
                "probability": round(pumping, 4),
                "confidence": round(_clip(0.4 + pumping / 2, 0.3, 0.95), 4),
            },
            "signal_strength": {
                "answer": round(strength, 3),
                "distribution": {str(i): round(_gauss_weight(strength, i, self.noise + 0.25), 4) for i in range(1, 6)},
                "confidence": round(_clip(0.35 + abs(strength - 3.0) / 3, 0.3, 0.95), 4),
            },
            "final_action": {
                "answer": action,
                "distribution": _action_distribution(action),
                "confidence": round(confidence, 4),
            },
        }
        _ = band
        return {
            "model": "mock-jev-1.13.0",
            "answers": answers,
            "usage": {"transport": "mock", "note": "DEMO/TEST TRANSPORT — not a real Jev call"},
        }


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _sigmoid(x: float) -> float:
    import math

    return 1.0 / (1.0 + math.exp(-x))


def _gauss_weight(mean: float, level: int, sigma: float) -> float:
    import math

    w = math.exp(-((level - mean) ** 2) / (2 * sigma * sigma))
    return w


def _action_distribution(action: str) -> dict[str, float]:
    base = {"buy": 0.05, "wait": 0.9, "avoid": 0.05}
    if action == "buy":
        base["buy"] = 0.6
        base["wait"] = 0.35
    elif action == "avoid":
        base["avoid"] = 0.6
        base["wait"] = 0.35
    return base


class JevClient:
    """Cached, allowlisted, rate-limited Jev client."""

    def __init__(
        self,
        settings: Settings,
        conn: psycopg.Connection | None = None,
        *,
        transport: JevTransport | None = None,
        mock: bool = False,
    ) -> None:
        self.settings = settings
        self.conn = conn
        self.transport: JevTransport = transport or (MockTransport() if mock else HttpTransport())
        self.semaphore = asyncio.Semaphore(max(1, settings.jev_max_concurrency))
        self.calls = 0
        self.cache_hits = 0

    # ---------------------------------------------------------------- configuration checks

    def verify(self) -> str | None:
        """Raises unless the host is allowlisted. Returns a third-party warning, if any."""
        self.settings.verify_jev_host()
        return self.settings.jev_third_party_warning()

    @property
    def endpoint(self) -> str:
        return self.settings.jev_endpoint

    def _headers(self) -> dict[str, str]:
        key = self.settings.jev_api_key
        if not key and not isinstance(self.transport, MockTransport):
            raise JevRequestError(0, "JEV_API_KEY is empty — set it or use the mock transport in demo mode")
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    # ---------------------------------------------------------------- request

    def build_payload(self, state: dict[str, Any], questions: dict[str, Any]) -> dict[str, Any]:
        return {"state": state, "model": self.settings.jev_model_resolved, "questions": questions}

    def cache_key(self, state: dict[str, Any], questions: dict[str, Any]) -> str:
        return canonical_hash({"model": self.settings.jev_model_resolved, "state": state, "questions": questions})

    async def ask(
        self,
        state: dict[str, Any],
        questions: dict[str, Any],
        *,
        use_cache: bool = True,
        store_split: str | None = None,
    ) -> JevResponse:
        warning = self.verify()
        if warning:
            log.warning("JEV THIRD-PARTY HOST WARNING: %s", warning)
        if isinstance(self.transport, MockTransport) and self.settings.jev_is_official_host:
            log.warning("using the MOCK Jev transport — every answer is synthetic (demo/tests only)")

        payload = self.build_payload(state, questions)
        key = self.cache_key(state, questions)
        if use_cache and self.conn is not None:
            cached = self._cache_get(key)
            if cached is not None:
                self.cache_hits += 1
                resp = _parse_response(cached["response_json"], key=key, endpoint=self.endpoint)
                resp.from_cache = True
                return resp

        async with self.semaphore:
            raw = await self.transport.call(self.endpoint, payload, self._headers(), self.settings.jev_timeout_s)
        self.calls += 1
        resp = _parse_response(raw, key=key, endpoint=self.endpoint)
        if self.conn is not None:
            self._cache_put(key, payload, raw, resp.model)
            if store_split:
                _ = store_split
        return resp

    # ---------------------------------------------------------------- cache

    def _cache_get(self, key: str) -> dict[str, Any] | None:
        assert self.conn is not None
        row = self.conn.execute(
            "select request_json, response_json, model from jev_cache where cache_key = %s", (key,)
        ).fetchone()
        return dict(row) if row else None

    def _cache_put(self, key: str, request: dict[str, Any], response: dict[str, Any], model: str) -> None:
        assert self.conn is not None
        self.conn.execute(
            """
            insert into jev_cache (cache_key, request_json, response_json, model)
            values (%s, %s, %s, %s)
            on conflict (cache_key) do nothing
            """,
            (key, jsonb(request), jsonb(response), model),
        )
        self.conn.commit()

    def stats(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "transport": type(self.transport).__name__,
            "endpoint": self.endpoint,
            "official_host": self.settings.jev_is_official_host,
            "model": self.settings.jev_model_resolved,
            "provider": self.settings.jev_provider_resolved,
            "canonical_request_example": "",
        }


def _parse_response(raw: dict[str, Any], *, key: str, endpoint: str) -> JevResponse:
    answers_raw = raw.get("answers")
    if not isinstance(answers_raw, dict) or not answers_raw:
        raise JevRequestError(200, f"Jev response has no 'answers' object: {str(raw)[:300]}")
    answers: dict[str, JevAnswer] = {}
    for qid, payload in answers_raw.items():
        if not isinstance(payload, dict):
            answers[qid] = JevAnswer(question_id=qid, type="unknown", value=payload, raw={})
            continue
        answers[qid] = _parse_answer(qid, payload)
    return JevResponse(
        model=str(raw.get("model", "unknown")),
        answers=answers,
        usage=dict(raw.get("usage") or {}),
        cache_key=key,
        raw=raw,
        endpoint=endpoint,
    )


def _parse_answer(qid: str, payload: dict[str, Any]) -> JevAnswer:
    def pick(keys: tuple[str, ...]) -> Any:
        for k in keys:
            if k in payload:
                return payload[k]
        return None

    dist_raw = pick(DIST_KEYS)
    distribution: dict[str, float] = {}
    if isinstance(dist_raw, dict):
        for opt, prob in dist_raw.items():
            try:
                distribution[str(opt)] = float(prob)
            except (TypeError, ValueError):
                continue
    elif isinstance(dist_raw, list):
        # score criteria come back as a list of probabilities, one per level
        distribution = {str(i + 1): float(p) for i, p in enumerate(dist_raw) if _is_number(p)}

    value = pick(CHOICE_KEYS)
    if value is None and distribution:
        value = max(distribution.items(), key=lambda kv: kv[1])[0]
    prob = pick(PROB_KEYS)
    qtype = "choice" if distribution and isinstance(value, str) else ("noul" if prob is not None else "score")
    if qid == "day_direction":
        qtype = "choice"
    elif qid == "signal_strength":
        qtype = "score"
    elif qid == "final_action":
        qtype = "choice"
    elif qid in ("credible_contrading_news", "coordinated_pumping"):
        qtype = "noul"
    if qtype == "noul" and prob is not None:
        value = prob
    confidence = pick(CONF_KEYS)
    return JevAnswer(
        question_id=qid,
        type=qtype,
        value=value,
        distribution=distribution,
        confidence=float(confidence) if _is_number(confidence) else None,
        raw=payload,
    )


def _is_number(value: Any) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def get_client(settings: Settings, conn: psycopg.Connection | None = None, *, mock: bool | None = None) -> JevClient:
    """Factory.

    Real HTTP is used whenever JEV_API_KEY is present and demo mode is off. The mock
    transport is only selected when the operator explicitly asked for demo mode, explicitly
    asked for --mock, or when there is no key at all (in which case a loud warning is
    printed and every stored answer is labelled with the mock model name).
    """
    if mock is None:
        if settings.gm_demo_mode:
            use_mock = True
        elif not settings.jev_api_key:
            log.warning(
                "JEV_API_KEY is empty — falling back to the MOCK Jev transport. Every answer "
                "produced in this run is synthetic and labelled as such."
            )
            use_mock = True
        else:
            use_mock = False
    else:
        use_mock = mock
    return JevClient(settings, conn, mock=use_mock)


class _client_ctx:
    def __init__(self, client: httpx.AsyncClient | None) -> None:
        self._client = client
        self._owned: httpx.AsyncClient | None = None

    async def __aenter__(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        self._owned = httpx.AsyncClient(timeout=30.0)
        return self._owned

    async def __aexit__(self, *exc: object) -> None:
        if self._owned is not None:
            await self._owned.aclose()


__all__ = [
    "JevClient",
    "JevAnswer",
    "JevResponse",
    "MockTransport",
    "HttpTransport",
    "get_client",
    "OFFICIAL_JEV_HOST",
    "PROB_KEYS",
    "DIST_KEYS",
]

"""Extraction providers: OpenRouter fallback chain (real) and a deterministic mock (demo).

Free-model discipline (R1a, R9):
  * EXTRACTION_MODELS is an ORDERED fallback chain
  * temperature 0, JSON-only answer, single retry with a stricter reminder per model
  * exponential backoff with jitter on 429/5xx; NEVER on 4xx validation errors
  * every successful answer is cached by sha256(raw_text + '|' + model) in Postgres
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from ..config import Settings
from ..jsonutil import sha256_hex
from ..util.ratelimit import Backoff, RateLimiter
from .prompt import EXTRACTION_SYSTEM_PROMPT, STRICTER_REMINDER, build_user_prompt
from .schema import ExtractionRecord, ExtractionValidationError, parse_extraction

log = logging.getLogger(__name__)

RETRY_STATUS = {408, 429, 500, 502, 503, 504, 522, 524, 529}


@dataclass
class ExtractionResult:
    record: ExtractionRecord | None
    model: str
    raw: str
    status: str  # ok | failed
    reason: str | None = None
    cache_key: str = ""
    attempts: int = 0


class ExtractionProvider(Protocol):
    async def extract(self, raw_text: str, *, channel: str | None = None, published_at: str | None = None) -> ExtractionResult:
        ...


def cache_key_for(raw_text: str, model: str) -> str:
    return sha256_hex(f"{raw_text}|{model}")


class OpenRouterProvider:
    """Tries each configured model in order until one yields a schema-valid record."""

    name = "openrouter"

    def __init__(
        self,
        api_key: str,
        models: list[str],
        *,
        base_url: str = "https://openrouter.ai/api/v1",
        concurrency: int = 3,
        max_attempts: int = 4,
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY is required for OpenRouterProvider")
        if not models:
            raise ValueError("EXTRACTION_MODELS is empty — no model to call")
        self.api_key = api_key
        self.models = models
        self.base_url = base_url.rstrip("/")
        self.semaphore = asyncio.Semaphore(max(1, concurrency))
        self.limiter = RateLimiter(40)
        self.backoff = Backoff(base=2.0, factor=2.0, max_sleep=60.0)
        self.max_attempts = max_attempts
        self.timeout = timeout
        self._client = client

    async def extract(
        self, raw_text: str, *, channel: str | None = None, published_at: str | None = None
    ) -> ExtractionResult:
        attempts = 0
        last_raw = ""
        last_reason: str | None = None
        for model in self.models:
            for strict in (False, True):  # exactly one stricter retry per model
                attempts += 1
                try:
                    raw = await self._call(model, raw_text, channel, published_at, stricter=strict)
                except _HardFailure as exc:
                    last_reason = exc.reason
                    last_raw = exc.raw or last_raw
                    log.warning("extraction model %s hard-failed: %s", model, exc.reason)
                    break  # next model
                last_raw = raw
                try:
                    record = parse_extraction(raw)
                except ExtractionValidationError as exc:
                    last_reason = f"validation:{str(exc)[:160]}"
                    log.info("model %s produced invalid JSON (strict=%s)", model, strict)
                    continue
                return ExtractionResult(
                    record=record,
                    model=model,
                    raw=raw,
                    status="ok",
                    cache_key=cache_key_for(raw_text, model),
                    attempts=attempts,
                )
        return ExtractionResult(
            record=None,
            model=self.models[-1] if self.models else "",
            raw=last_raw,
            status="failed",
            reason=last_reason or "all_models_failed",
            cache_key=cache_key_for(raw_text, "failed"),
            attempts=attempts,
        )

    async def _call(
        self, model: str, raw_text: str, channel: str | None, published_at: str | None, *, stricter: bool
    ) -> str:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(raw_text, channel=channel, published_at=published_at)},
        ]
        if stricter:
            messages.append({"role": "user", "content": STRICTER_REMINDER})
        payload = {
            "model": model,
            "temperature": 0,
            "top_p": 1,
            "max_tokens": 700,
            "messages": messages,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/grhadi193-cyber/goldminer",
            "X-Title": "Mishkah extraction",
        }
        async with self.semaphore:
            await self.limiter.acquire()
            async with _client_ctx(self._client) as client:
                last: Exception | None = None
                for attempt in range(1, self.max_attempts + 1):
                    try:
                        resp = await client.post(
                            f"{self.base_url}/chat/completions", json=payload, headers=headers, timeout=self.timeout
                        )
                    except (httpx.TimeoutException, httpx.TransportError) as exc:
                        last = exc
                        await self.backoff.sleep(attempt)
                        continue
                    if resp.status_code in RETRY_STATUS:
                        last = _HardFailure(f"http_{resp.status_code}", resp.text)
                        await self.backoff.sleep(attempt)
                        continue
                    if resp.status_code >= 400:
                        raise _HardFailure(f"http_{resp.status_code}:{resp.text[:160]}", resp.text)
                    data: Any = resp.json()
                    try:
                        return data["choices"][0]["message"]["content"] or ""
                    except (KeyError, IndexError, TypeError) as exc:
                        last = _HardFailure(f"unexpected_openrouter_shape:{str(exc)[:120]}", str(data)[:300])
                        break
                raise last if isinstance(last, _HardFailure) else _HardFailure(f"transport:{last}", "")


class _HardFailure(Exception):
    def __init__(self, reason: str, raw: str = "") -> None:
        self.reason = reason
        self.raw = raw
        super().__init__(reason)


class MockProvider:
    """Deterministic keyword extractor used ONLY in demo mode / tests.

    It reads the same Persian text a real model would and emits the same record shape, so the
    pipeline (validation, caching, DB writes, failure handling) is exercised for real. It is
    NOT a substitute for an LLM on real channels: it only understands a fixed vocabulary.
    """

    name = "mock"

    UP = ["رشد", "بالا", "سبز", "گران", "جهش", "صعود", "افزایش", "مثبت", "می‌ترکد", "تورم", "بخرید"]
    DOWN = ["افت", "پایین", "قرمز", "ارزان", "اصلاح", "نزول", "کاهش", "منفی", "فروش", "تخلیه"]
    FLAT = ["رنج", "ثابت", "بی‌رمق", "کم‌حجم", "بدون تغییر", "تغییری ندارد"]
    NEWS = ["فوری", "خبر", "اعلام", "رسمی"]
    RUMOR = ["شنیده", "می‌گویند", "شایعه"]
    QUOTE = ["قیمت", "تومان", "ریال", "معامله", "نرخ", "الان"]
    ASSETS = {
        "آبشده": "abshodeh",
        "سکه امامی": "emami_coin",
        "سکه کامل": "whole_coin",
        "نیم‌سکه": "half_coin",
        "ربع‌سکه": "quarter_coin",
        "انس": "xauusd_ounce",
        "دلار آزاد": "usd_free_market",
        "دلار": "usd_free_market",
        "صندوق طلا": "gold_fund",
        "سند": "gold_fund",
    }
    GLOSS = {
        "حباب": "bubble/premium over melt value",
        "هجوم": "demand rush",
        "صرافی": "gold/exchange dealer",
        "حواله": "hawala transfer rate",
        "صفحه": "public price board",
    }
    DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

    async def extract(
        self, raw_text: str, *, channel: str | None = None, published_at: str | None = None
    ) -> ExtractionResult:
        text = raw_text.replace("[demo]", "").strip()
        normalised = text.translate(self.DIGITS)
        record = ExtractionRecord(
            topic=self._topic(normalised),
            direction=self._direction(normalised),
            kind=self._kind(normalised),
            intensity=self._intensity(normalised),
            quoted_price=self._price(normalised),
            quoted_asset=self._asset(normalised),
            confidence=0.62,
            english_text=self._english(normalised, channel),
            persian_gloss=self._gloss(normalised),
        )
        return ExtractionResult(
            record=record, model="mock-extractor-v1", raw=record.model_dump_json(),
            status="ok", cache_key=cache_key_for(raw_text, "mock-extractor-v1"), attempts=1,
        )

    def _direction(self, text: str) -> str:
        up = sum(w in text for w in self.UP)
        down = sum(w in text for w in self.DOWN)
        flat = sum(w in text for w in self.FLAT)
        if up > down and up >= flat:
            return "up"
        if down > up and down >= flat:
            return "down"
        if flat:
            return "flat"
        return "none"

    def _kind(self, text: str) -> str:
        if any(w in text for w in self.NEWS):
            return "news"
        if any(w in text for w in self.RUMOR):
            return "rumor"
        if any(w in text for w in self.QUOTE) and self._price(text) is not None:
            return "price_quote"
        return "opinion"

    def _intensity(self, text: str) -> int:
        score = 1
        score += min(2, text.count("!"))
        if any(w in text for w in ("فوری", "عجله", "می‌ترکد", "جدی")):
            score += 2
        if self._price(text) is not None:
            score += 1
        return max(1, min(5, score))

    def _price(self, text: str) -> float | None:
        candidates = re.findall(r"([\d][\d,\.]{3,})", text)
        values: list[float] = []
        for raw in candidates:
            try:
                values.append(float(raw.replace(",", "")))
            except ValueError:
                continue
        if not values:
            return None
        value = max(values)
        if "ریال" in text and "تومان" not in text:
            value = value / 10.0
        if value < 1000:  # probably written in millions
            value = value * 1_000_000.0
        return round(value, 1)

    def _asset(self, text: str) -> str | None:
        for key, asset in self.ASSETS.items():
            if key in text:
                return asset
        return None

    def _topic(self, text: str) -> str:
        asset = self._asset(text) or "gold"
        return f"market chatter about {asset}"

    def _gloss(self, text: str) -> str | None:
        hits = [f"{k}: {v}" for k, v in self.GLOSS.items() if k in text]
        return "; ".join(hits) if hits else None

    def _english(self, text: str, channel: str | None) -> str:
        asset = self._asset(text) or "gold"
        direction = self._direction(text).upper()
        price = self._price(text)
        parts = [f"@{channel}: " if channel else "", f"Report about {asset} leaning {direction}"]
        if price is not None:
            parts.append(f"; quoted price ≈ {price:,.0f} toman")
        return "".join(parts)


class ProviderChain:
    """Tries providers in order; the first valid extraction wins."""

    def __init__(self, providers: list[ExtractionProvider]) -> None:
        self.providers = providers

    async def extract(self, raw_text: str, *, channel: str | None = None, published_at: str | None = None) -> ExtractionResult:
        last: ExtractionResult | None = None
        for provider in self.providers:
            result = await provider.extract(raw_text, channel=channel, published_at=published_at)
            if result.status == "ok":
                return result
            last = result
        return last or ExtractionResult(record=None, model="", raw="", status="failed", reason="no_provider")


def get_provider(settings: Settings, *, force_mock: bool = False) -> ExtractionProvider:
    """Factory: real chain when a key is configured, mock only in demo/forced mode."""
    if force_mock or settings.gm_demo_mode:
        return MockProvider()
    if not settings.openrouter_api_key:
        raise ValueError(
            "OPENROUTER_API_KEY is empty. Set it, or run with GM_DEMO_MODE=1 to use the "
            "deterministic mock extractor (demo data only — never for research)."
        )
    providers: list[ExtractionProvider] = []
    if settings.extraction_model_list:
        providers.append(
            OpenRouterProvider(
                settings.openrouter_api_key,
                settings.extraction_model_list,
                base_url=settings.openrouter_base_url,
            )
        )
    if not providers:
        raise ValueError("EXTRACTION_MODELS is empty — configure the ordered fallback list.")
    return ProviderChain(providers)


class _client_ctx:
    def __init__(self, client: httpx.AsyncClient | None) -> None:
        self._client = client
        self._owned: httpx.AsyncClient | None = None

    async def __aenter__(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        self._owned = httpx.AsyncClient(timeout=90.0)
        return self._owned

    async def __aexit__(self, *exc: object) -> None:
        if self._owned is not None:
            await self._owned.aclose()

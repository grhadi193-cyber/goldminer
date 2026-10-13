"""T7 — Jev client contract: allowlist, retry policy, cache, endpoint shape (R2)."""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pytest

from worker.config import Settings
from worker.errors import JevHostNotAllowed, JevRequestError, JevUnavailable
from worker.jsonutil import canonical_hash
from worker.research.jev import HttpTransport, JevClient, MockTransport, get_client, _parse_response
from worker.research.question_tree import build_questions, question_types


class Recorder:
    """Counts calls and can be scripted to fail."""

    def __init__(self, statuses: list[int] | None = None, payload: dict[str, Any] | None = None) -> None:
        self.statuses = list(statuses or [])
        self.payload = payload
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            import json

            self.calls.append((str(request.url), json.loads(request.content), dict(request.headers)))
            status = self.statuses.pop(0) if self.statuses else 200
            if status >= 400:
                return httpx.Response(status, json={"error": "nope"})
            body = self.payload or {
                "model": "jev-1.13.0",
                "answers": {"day_direction": {"answer": "up", "distribution": {"up": 0.6, "down": 0.25, "flat": 0.15},
                                              "confidence": 0.72}},
                "usage": {"input_tokens": 10},
            }
            return httpx.Response(status, json=body)

        return httpx.MockTransport(handler)


def _settings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        # explicit, so a developer's .env (e.g. GM_DEMO_MODE=1) cannot change test behaviour
        "gm_demo_mode": 0,
        "database_url": "postgresql://unused",
        "start_date": date(2026, 4, 1),
        "jev_base_url": "https://api.typesafe.ai",
        "jev_allowed_hosts": "api.typesafe.ai",
        "jev_api_key": "test-key",
    }
    base.update(over)
    return Settings(**base)


def _client(settings: Settings, recorder: Recorder) -> JevClient:
    client = httpx.AsyncClient(transport=recorder.transport())
    return JevClient(settings, None, transport=HttpTransport(client=client))


async def test_allowlist_blocks_unlisted_host_and_sends_nothing():
    recorder = Recorder()
    settings = _settings(jev_base_url="https://jev-proxy.example.com", jev_allowed_hosts="api.typesafe.ai")
    client = _client(settings, recorder)
    with pytest.raises(JevHostNotAllowed) as exc:
        await client.ask({"a": 1}, build_questions())
    assert "api.typesafe.ai" in str(exc.value)
    assert recorder.calls == []


async def test_third_party_host_is_warned_but_allowed_when_listed(caplog):
    recorder = Recorder()
    settings = _settings(jev_base_url="https://jev-proxy.example.com", jev_allowed_hosts="api.typesafe.ai,jev-proxy.example.com")
    client = _client(settings, recorder)
    with caplog.at_level("WARNING"):
        await client.ask({"a": 1}, build_questions())
    assert any("THIRD PARTY" in r.message or "UNVERIFIED" in str(r.message) for r in caplog.records)
    assert len(recorder.calls) == 1


async def test_429_is_retried_then_succeeds():
    recorder = Recorder(statuses=[429, 529, 200])
    client = _client(_settings(), recorder)
    client.transport.backoff.base = 0.001  # type: ignore[attr-defined]
    client.transport.backoff.factor = 1.0  # type: ignore[attr-defined]
    resp = await client.ask({"a": 1}, build_questions())
    assert resp.answers["day_direction"].value == "up"
    assert len(recorder.calls) == 3


async def test_400_is_not_retried_and_is_surfaced():
    recorder = Recorder(statuses=[400])
    client = _client(_settings(), recorder)
    with pytest.raises(JevRequestError) as exc:
        await client.ask({"a": 1}, build_questions())
    assert exc.value.status == 400
    assert len(recorder.calls) == 1


async def test_overload_exhaustion_raises_unavailable():
    recorder = Recorder(statuses=[429, 429, 429, 429, 429])
    client = _client(_settings(), recorder)
    client.transport.backoff.base = 0.001  # type: ignore[attr-defined]
    client.transport.backoff.factor = 1.0  # type: ignore[attr-defined]
    with pytest.raises(JevUnavailable):
        await client.ask({"a": 1}, build_questions())


async def test_identical_request_hits_cache(conn):
    recorder = Recorder()
    settings = _settings(database_url="postgresql://unused")
    client = _client(settings, recorder)
    client.conn = conn
    state = {"day": {"date": "2026-05-06"}, "signals": {"bullish_minus_bearish_count": {"last_1h": 3}}}
    first = await client.ask(state, build_questions())
    second = await client.ask(state, build_questions())
    assert len(recorder.calls) == 1
    assert first.from_cache is False and second.from_cache is True
    assert first.cache_key == second.cache_key == canonical_hash(
        {"model": settings.jev_model, "state": state, "questions": build_questions()}
    )


async def test_cache_key_changes_when_state_changes(conn):
    recorder = Recorder()
    client = _client(_settings(), recorder)
    client.conn = conn
    questions = build_questions()
    await client.ask({"a": 1}, questions)
    await client.ask({"a": 2}, questions)
    assert len(recorder.calls) == 2


def test_endpoint_normalisation():
    assert _settings(jev_base_url="https://api.typesafe.ai").jev_endpoint == "https://api.typesafe.ai/v1/systemone"
    assert _settings(jev_base_url="https://api.typesafe.ai/").jev_endpoint == "https://api.typesafe.ai/v1/systemone"
    assert _settings(jev_base_url="https://api.typesafe.ai/v1").jev_endpoint == "https://api.typesafe.ai/v1/systemone"
    assert _settings(jev_base_url="https://api.typesafe.ai/v1/systemone").jev_endpoint.endswith("/v1/systemone")
    assert _settings(jev_base_url="http://localhost:9999").jev_endpoint == "http://localhost:9999/v1/systemone"


def test_request_payload_shape():
    recorder = Recorder()
    client = _client(_settings(jev_model="jev-latest"), recorder)
    payload = client.build_payload({"x": 1}, build_questions())
    assert set(payload) == {"state", "model", "questions"}
    assert payload["model"] == "jev-latest"
    assert set(payload["questions"]) == set(question_types())
    for qid, question in payload["questions"].items():
        assert question["type"] in ("noul", "choice", "score")
        if question["type"] == "choice":
            assert isinstance(question["criteria"], dict)
        if question["type"] == "score":
            assert isinstance(question["criteria"], list) and 2 <= len(question["criteria"]) <= 10
        assert question["instructions"]


def test_authorization_header_and_url_used():
    recorder = Recorder()
    client = _client(_settings(), recorder)
    import asyncio

    asyncio.run(client.ask({"a": 1}, build_questions()))
    url, payload, headers = recorder.calls[0]
    assert url == "https://api.typesafe.ai/v1/systemone"
    assert headers["authorization"] == "Bearer test-key"
    assert "state" in payload


def test_mock_transport_is_used_when_no_key():
    settings = _settings(jev_api_key="", gm_demo_mode=0)
    client = get_client(settings, None)
    assert isinstance(client.transport, MockTransport)


def test_real_transport_is_used_when_key_present():
    settings = _settings(jev_api_key="k")
    client = get_client(settings, None)
    assert isinstance(client.transport, HttpTransport)


def test_response_parser_tolerates_alternative_field_names():
    raw = {
        "model": "jev-1.13.0",
        "answers": {
            "day_direction": {"choice": "flat", "probabilities": {"up": 0.2, "down": 0.3, "flat": 0.5}, "conf": 0.61},
            "coordinated_pumping": {"probability": 0.71, "confidence": 0.8},
            "signal_strength": {"score": 3.4, "distribution": [0.05, 0.2, 0.4, 0.3, 0.05], "confidence": 0.5},
        },
    }
    parsed = _parse_response(raw, key="k", endpoint="e")
    assert parsed.answers["day_direction"].value == "flat"
    assert parsed.answers["day_direction"].top_option() == "flat"
    assert parsed.answers["day_direction"].confidence == 0.61
    assert parsed.answers["coordinated_pumping"].type == "noul"
    assert parsed.answers["coordinated_pumping"].value == 0.71
    assert parsed.answers["signal_strength"].type == "score"
    assert parsed.answers["signal_strength"].distribution["4"] == 0.3


def test_response_without_answers_is_an_error():
    with pytest.raises(JevRequestError):
        _parse_response({"model": "x"}, key="k", endpoint="e")

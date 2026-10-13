"""Provider resolution for Jev (R2).

The client must never be locked to one vendor, but it must also never send a request to a host
the operator has not allowed, and it must never pretend that a paid model answered when the free
one is gone.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pytest

from worker.config import JEV_PROVIDER_PRESETS, Settings
from worker.errors import ConfigError, JevHostNotAllowed, JevUnavailable
from worker.research.jev import HttpTransport, JevClient
from worker.research.question_tree import build_questions


def _settings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "gm_demo_mode": 0,
        "database_url": "postgresql://unused",
        "start_date": date(2026, 4, 1),
        "jev_api_key": "test-key",
        # explicit empties: a developer's .env must not decide what these tests assert
        "jev_base_url": "",
        "jev_model": "",
        "jev_allowed_hosts": "",
        "jev_provider": "",
    }
    base.update(over)
    return Settings(**base)


def test_default_is_the_official_typesafe_endpoint():
    s = _settings()
    assert s.jev_provider_resolved == "typesafe"
    assert s.jev_base_url_resolved == "https://api.typesafe.ai"
    assert s.jev_endpoint == "https://api.typesafe.ai/v1/systemone"
    assert s.jev_model_resolved == "jev-latest"
    assert s.allowed_jev_hosts == ["api.typesafe.ai"]
    assert s.jev_is_official_host is True
    assert s.jev_third_party_warning() is None


def test_opencode_preset_gives_the_free_model_and_its_own_allowlist():
    s = _settings(jev_provider="opencode")
    assert s.jev_endpoint == "https://opencode.ai/inference/v1/systemone"
    assert s.jev_model_resolved == "jev-1.13-free"
    assert s.allowed_jev_hosts == ["opencode.ai"]
    # allowlisted, so no refusal — but the operator still gets the third-party warning
    s.verify_jev_host()
    warning = s.jev_third_party_warning()
    assert warning is not None and "opencode.ai" in warning
    assert "UNVERIFIED THIRD PARTY" in warning


def test_opencode_paid_id_and_legacy_zen_path_are_available():
    paid = _settings(jev_provider="opencode-paid")
    assert paid.jev_model_resolved == "jev-1.13"
    legacy = _settings(jev_provider="opencode-zen")
    assert legacy.jev_endpoint == "https://opencode.ai/zen/v1/systemone"


def test_explicit_env_always_wins_over_the_preset():
    s = _settings(jev_provider="opencode", jev_base_url="https://gateway.internal/jev",
                  jev_model="jev-1.13", jev_allowed_hosts="gateway.internal")
    assert s.jev_endpoint == "https://gateway.internal/jev/v1/systemone"
    assert s.jev_model_resolved == "jev-1.13"
    assert s.allowed_jev_hosts == ["gateway.internal"]


def test_full_endpoint_pasted_into_base_url_is_used_verbatim():
    s = _settings(jev_base_url="https://api.typesafe.ai/v1/systemone", jev_allowed_hosts="api.typesafe.ai")
    assert s.jev_endpoint == "https://api.typesafe.ai/v1/systemone"


def test_unlisted_host_refuses_before_any_byte_is_sent():
    s = _settings(jev_base_url="https://jev-proxy.example.com", jev_allowed_hosts="api.typesafe.ai")
    with pytest.raises(JevHostNotAllowed):
        s.verify_jev_host()


def test_unknown_provider_is_a_loud_config_error():
    s = _settings(jev_provider="someones-gateway")
    with pytest.raises(ConfigError) as exc:
        s.jev_provider_resolved
    assert "JEV_PROVIDER" in str(exc.value)


async def test_free_model_404_is_not_retried_and_explains_the_swap():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(404, json={"error": "model not available"})

    settings = _settings(jev_provider="opencode")
    client = JevClient(settings, None, transport=HttpTransport(client=httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    with pytest.raises(JevUnavailable) as exc:
        await client.ask({"a": 1}, build_questions())
    assert len(calls) == 1                      # no blind retries on a "model is gone" answer
    assert "jev-1.13-free" in str(exc.value)
    assert "jev-1.13" in str(exc.value)


def test_preset_table_is_documented_and_reachable():
    assert set(JEV_PROVIDER_PRESETS) >= {"typesafe", "opencode", "opencode-paid"}
    for name, (base, model, host) in JEV_PROVIDER_PRESETS.items():
        assert base.startswith("https://"), name
        assert model, name
        assert "." in host, name

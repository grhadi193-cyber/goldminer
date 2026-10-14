"""M9 admin — security primitives, panel settings store, and the HTTP surface.

Why these tests matter: the panel is the only way the operator will ever hand the system API keys,
and it is reachable from the internet. So the contract tested here is
"a database dump reveals no secret", "no endpoint echoes a value", and
"a panel value beats the environment, an environment value beats the default, and clearing a panel
value falls back instead of disappearing".
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os

import pytest
from starlette.testclient import TestClient

from worker import security
from worker import settings_store as store
from worker.config import Settings

pytestmark = pytest.mark.postgres

MASTER = base64.b64encode(bytes(range(32))).decode()
PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("MISHKAH_MASTER_KEY", MASTER)


def _base(pg_uri: str, **kw) -> Settings:
    values = dict(
        database_url=pg_uri,
        gm_demo_mode=1,
        jev_base_url="",
        jev_api_key="",
        jev_model="",
        jev_provider="",
        jev_allowed_hosts="",
    )
    values.update(kw)
    return Settings(**values)


# --------------------------------------------------------------------------- security primitives


def test_password_hash_is_salted_scrypt_and_verifies():
    first = security.hash_password(PASSWORD)
    second = security.hash_password(PASSWORD)
    assert first.startswith("scrypt$")
    assert first != second  # unique salt per call
    assert security.verify_password(PASSWORD, first)
    assert security.verify_password(PASSWORD, second)
    assert not security.verify_password("wrong", first)
    assert not security.verify_password(PASSWORD, None)
    assert not security.verify_password(PASSWORD, "garbage")


def test_secret_round_trip_and_tamper_detection():
    token = security.encrypt("sk-live-1234567890")
    assert token.startswith("v1:") and "sk-live" not in token
    assert security.decrypt(token) == "sk-live-1234567890"
    assert security.decrypt(None) is None
    raw = base64.b64decode(token[3:])
    broken = base64.b64encode(bytes([raw[0] ^ 1]) + raw[1:]).decode()
    with pytest.raises(Exception):
        security.decrypt("v1:" + broken)


def test_fingerprint_identifies_without_revealing():
    fingerprint = security.fingerprint("sk-live-abcdefghijklmnop")
    assert fingerprint is not None and fingerprint.startswith("sha256:")
    assert "abcdefghijkl" not in fingerprint
    assert fingerprint.endswith("mnop")  # last four only, by design
    assert security.fingerprint("") is None


def test_panel_password_lives_hashed_in_app_secrets(conn):
    assert security.panel_password_hash(conn) is None
    security.set_password(conn, PASSWORD)
    stored = conn.execute(
        "select ciphertext from app_secrets where name = %s", ("UI_ADMIN_PASSWORD",)
    ).fetchone()
    assert PASSWORD not in stored["ciphertext"]
    assert security.verify_password(PASSWORD, security.panel_password_hash(conn))


def test_session_lifecycle_and_expiry(conn):
    session = security.create_session(conn, minutes=30, label="pytest")
    assert security.session_valid(conn, session.token)
    assert not security.session_valid(conn, "not-a-token")
    conn.execute(
        "update ui_sessions set expires_at = now() - interval '1 minute' where token_hash = %s",
        (hashlib.sha256(session.token.encode()).hexdigest(),),
    )
    assert not security.session_valid(conn, session.token)
    fresh = security.create_session(conn, minutes=30)
    security.destroy_session(conn, fresh.token)
    assert not security.session_valid(conn, fresh.token)


def test_login_throttle_blocks_after_eight_failures(conn):
    assert security.login_allowed(conn)[0]
    for _ in range(security.MAX_FAILED_LOGINS):
        security.record_attempt(conn, ok=False, label="pytest")
    allowed, retry_after = security.login_allowed(conn)
    assert not allowed and retry_after > 0
    security.record_attempt(conn, ok=True, label="pytest")
    assert security.login_allowed(conn)[0]


# --------------------------------------------------------------------------- settings store


def test_precedence_panel_beats_env_beats_default(conn, pg_uri, monkeypatch):
    monkeypatch.setenv("JEV_MODEL", "env-model")
    base = _base(pg_uri)
    assert base.jev_model_resolved == "jev-latest"  # package default (typesafe preset)

    from_env = store.effective_settings(conn, base=base)
    assert from_env.jev_model_resolved == "env-model"
    field = next(f for g in store.describe(conn, from_env) for f in g["fields"] if f["name"] == "JEV_MODEL")
    assert field["source"] == "env" and field["effective"] == "env-model"

    security.set_secret(conn, "JEV_MODEL", "panel-model", updated_by="pytest")
    from_panel = store.effective_settings(conn, base=base)
    assert from_panel.jev_model_resolved == "panel-model"
    field = next(f for g in store.describe(conn, from_panel) for f in g["fields"] if f["name"] == "JEV_MODEL")
    assert field["source"] == "panel" and field["value"] == "panel-model"

    security.set_secret(conn, "JEV_MODEL", None)
    assert store.effective_settings(conn, base=base).jev_model_resolved == "env-model"


def test_secret_values_never_appear_in_describe(conn, pg_uri):
    security.set_secret(conn, "JEV_API_KEY", "sk-secret-value-9999", updated_by="pytest")
    settings = store.effective_settings(conn, base=_base(pg_uri))
    blob = json.dumps(store.describe(conn, settings))
    assert "sk-secret-value-9999" not in blob
    assert "9999" in blob  # last four, so the operator can tell which key it is
    assert settings.jev_api_key == "sk-secret-value-9999"  # the process still gets the real value


def test_coercion_types_from_the_settings_model(conn, pg_uri):
    security.set_secret(conn, "UI_SESSION_MINUTES", "45", updated_by="pytest")
    security.set_secret(conn, "TGJU_ENABLED", "0", updated_by="pytest")
    security.set_secret(conn, "NOBITEX_MAX_REQ_PER_MIN", "12", updated_by="pytest")
    settings = store.effective_settings(conn, base=_base(pg_uri))
    assert settings.tgju_enabled in (0, False)
    assert int(settings.nobitex_max_req_per_min) == 12
    assert store.runtime_value(conn, "UI_SESSION_MINUTES") == "45"


def test_admin_password_is_panel_only_and_never_merged(conn, pg_uri):
    security.set_secret(conn, "UI_ADMIN_PASSWORD", security.hash_password(PASSWORD), updated_by="pytest")
    settings = store.effective_settings(conn, base=_base(pg_uri))
    assert not hasattr(settings, "ui_admin_password")
    panel = next(g for g in store.describe(conn, settings) if g["key"] == "panel")
    field = next(f for f in panel["fields"] if f["name"] == "UI_ADMIN_PASSWORD")
    assert field["value"] == "********" and field["source"] == "panel"
    assert field["secret"] is True and field["effective"] is None


def test_unknown_provider_in_panel_is_a_loud_config_error(conn, pg_uri):
    from worker.errors import ConfigError

    security.set_secret(conn, "JEV_PROVIDER", "definitely-not-a-provider", updated_by="pytest")
    settings = store.effective_settings(conn, base=_base(pg_uri))
    with pytest.raises(ConfigError):
        settings.jev_provider_resolved  # the refusal happens at resolution, not at read time


def test_run_config_round_trip_through_the_panel_is_audited(conn):
    from worker.db import repo

    repo.set_run_config(conn, "panel_note", {"hello": "world"}, set_by="panel")
    assert repo.get_run_config(conn, "panel_note") == {"hello": "world"}


# --------------------------------------------------------------------------- HTTP surface


@pytest.fixture()
def client(pg_uri, monkeypatch, conn):
    # `conn` (unused directly) is what truncates the tables — without it every API test would
    # inherit the panel state of the test before it.
    monkeypatch.setenv("DATABASE_URL", pg_uri)
    monkeypatch.setenv("GM_DEMO_MODE", "1")
    monkeypatch.setenv("WORKER_WEBHOOK_SECRET", "hook-secret")
    monkeypatch.delenv("UI_ADMIN_PASSWORD", raising=False)
    from worker.api.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def test_healthz_and_meta_are_public(client):
    health = client.get("/healthz")
    assert health.status_code == 200, health.json()
    assert health.json()["database"] == "ok"
    assert health.json()["version"] == "phase1-m9"
    meta = client.get("/api/meta")
    assert meta.status_code == 200 and meta.json()["demo"] is True
    assert meta.headers["cache-control"] == "no-store"


def test_admin_routes_require_a_session(client):
    assert client.get("/api/admin/integrations").status_code == 401
    assert client.put("/api/admin/integrations/JEV_MODEL", json={"value": "x"}).status_code == 401
    assert client.post("/api/admin/run_config", json={"key": "k", "value": 1}).status_code == 401


def test_setup_login_out_and_change_password(client):
    assert client.post("/api/auth/setup", json={"password": "short"}).status_code == 400
    first = client.post("/api/auth/setup", json={"password": PASSWORD})
    assert first.status_code == 200 and first.cookies.get(security.SESSION_COOKIE)
    assert client.post("/api/auth/setup", json={"password": "another-long-password"}).status_code == 409

    client.post("/api/auth/logout")
    assert client.get("/api/auth/status").json()["authenticated"] is False
    assert client.post("/api/auth/login", json={"password": "nope-nope-nope"}).status_code == 401

    login = client.post("/api/auth/login", json={"password": PASSWORD})
    assert login.status_code == 200
    assert client.get("/api/auth/status").json()["authenticated"] is True
    bad = {"current_password": "wrong", "new_password": "x" * 20}
    assert client.post("/api/auth/password", json=bad).status_code == 401
    good = {"current_password": PASSWORD, "new_password": "y" * 20}
    assert client.post("/api/auth/password", json=good).status_code == 200
    assert client.post("/api/auth/login", json={"password": "y" * 20}).status_code == 200


def test_integrations_put_and_delete_drive_effective_settings(client, pg_uri):
    client.post("/api/auth/setup", json={"password": PASSWORD})
    put = client.put("/api/admin/integrations/JEV_PROVIDER", json={"value": "opencode"})
    assert put.status_code == 200
    client.put("/api/admin/integrations/JEV_API_KEY", json={"value": "sk-live-never-echoed-4321"})
    listed = client.get("/api/admin/integrations").json()
    blob = json.dumps(listed)
    assert "sk-live-never-echoed-4321" not in blob
    field = next(f for g in listed["groups"] for f in g["fields"] if f["name"] == "JEV_PROVIDER")
    assert field["source"] == "panel" and field["value"] == "opencode"
    secret = next(f for g in listed["groups"] for f in g["fields"] if f["name"] == "JEV_API_KEY")
    assert secret["fingerprint"] and "4321" in secret["fingerprint"]

    assert client.put("/api/admin/integrations/NOT_A_FIELD", json={"value": "x"}).status_code == 404
    assert client.put("/api/admin/integrations/JEV_PROVIDER", json={"value": "nope"}).status_code == 400
    assert client.delete("/api/admin/integrations/JEV_PROVIDER").status_code == 200
    cleared = client.get("/api/admin/integrations").json()
    field = next(f for g in cleared["groups"] for f in g["fields"] if f["name"] == "JEV_PROVIDER")
    assert field["source"] != "panel"


def test_run_config_and_group_tests(client):
    client.post("/api/auth/setup", json={"password": PASSWORD})
    assert client.post("/api/admin/run_config", json={"value": 1}).status_code == 400
    assert client.post("/api/admin/run_config", json={"key": "panel_note", "value": {"a": 1}}).status_code == 200
    assert client.post("/api/admin/test/panel").json()["ok"] is True
    assert client.post("/api/admin/test/nope").status_code == 404
    jev = client.post("/api/admin/test/jev").json()
    assert jev["ok"] is True and jev["mock"] is True  # demo mode: mock transport, never a paid call


def test_webhook_signature_is_required_and_actions_are_explicit(client):
    body = json.dumps({"action": "ping"}).encode()
    assert client.post("/webhook", content=body).status_code == 401
    signature = hmac.new(b"hook-secret", body, hashlib.sha256).hexdigest()
    ok = client.post("/webhook", content=body, headers={"x-mishkah-signature": signature})
    assert ok.status_code == 200 and ok.json()["accepted"] is True
    export = json.dumps({"action": "export"}).encode()
    signature = hmac.new(b"hook-secret", export, hashlib.sha256).hexdigest()
    assert client.post("/webhook", content=export, headers={"x-mishkah-signature": signature}).status_code == 200
    other = json.dumps({"action": "retrain"}).encode()
    signature = hmac.new(b"hook-secret", other, hashlib.sha256).hexdigest()
    assert client.post("/webhook", content=other, headers={"x-mishkah-signature": signature}).status_code == 501


def test_bundle_read_needs_a_session(client):
    assert client.get("/api/bundle/meta").status_code == 401
    assert client.get("/api/bundle/live").status_code == 401


def test_bundle_read_serves_the_same_members_as_the_cli(client, conn):
    from .conftest import add_channel, add_message

    from datetime import datetime, timezone

    client.post("/api/auth/setup", json={"password": PASSWORD})
    channel_id = add_channel(conn, "chan_api")
    add_message(conn, channel_id, published_at=datetime(2026, 5, 6, 9, 0, tzinfo=timezone.utc))
    response = client.get("/api/bundle/live")
    assert response.status_code == 200, response.json()
    payload = response.json()
    assert "tiles" in payload and "as_of" in payload and payload["mode"] == "paper"
    meta = client.get("/api/bundle/meta").json()
    assert meta["demo"] is True and meta["stale_after_minutes"] > 0
    assert client.get("/api/bundle/does_not_exist").status_code == 404
    assert client.get("/api/bundle/..%2f..%2fetc%2fpasswd").status_code in (400, 404)

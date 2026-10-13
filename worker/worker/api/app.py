"""Starlette app: bundle reads, admin panel API, signed webhook.

Design constraints this file obeys:

  * The WORKER is the only writer. The panel talks to this API; it never talks to Postgres.
  * Reads are cheap and cacheable; writes are few, audited, and small (one row each).
  * No secret ever leaves this process. `GET /api/admin/integrations` returns fingerprints and
    "is it set", never a value.
  * Auth: scrypt-hashed password (env or panel-managed) → opaque session token in an HttpOnly
    cookie; login is rate limited per 15-minute window and every attempt is recorded.
  * Bundle reads reuse `ui_bundle.build_bundle`, i.e. the same anti-leakage reads as the CLI
    exporter, so a deployed UI cannot show a state the replay could not have seen.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from .. import security, settings_store
from ..config import REPO_ROOT, Settings, get_settings
from ..db import conn as dbconn
from ..db import repo
from ..errors import ConfigError, JevHostNotAllowed
from ..jsonutil import jsonb
from ..research.jev import JevClient
from ..research.question_tree import build_questions
from ..ui_bundle import build_bundle

log = logging.getLogger(__name__)

VERSION = "phase1-m9"
BUNDLE_TTL_SECONDS = float(os.environ.get("MISHKAH_BUNDLE_TTL", "30"))
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}

TEST_QUESTION = {
    "connection_ok": {
        "type": "noul",
        "instructions": "This is a connectivity check from a research pipeline. Answer true.",
        "criteria": {"true": "Answer yes", "false": "Answer no"},
    }
}


# --------------------------------------------------------------------------- helpers


def _json(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store"})


def _error(status: int, code: str, message: str) -> JSONResponse:
    return _json({"error": code, "message": message}, status=status)


def _settings_with_db(conn) -> Settings:
    return settings_store.effective_settings(conn)


def _session_token(request: Request) -> str | None:
    token = request.cookies.get(security.SESSION_COOKIE)
    if token:
        return token
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def _require_session(conn, request: Request) -> JSONResponse | None:
    if security.session_valid(conn, _session_token(request)):
        return None
    return _error(401, "unauthorized", "log in first")


def _session_minutes(conn=None) -> int:
    raw = settings_store.runtime_value(conn, "UI_SESSION_MINUTES", default="720")
    try:
        return max(5, int(float(raw or 720)))
    except (TypeError, ValueError):
        return 720


def _set_cookie(response: Response, token: str, *, secure: bool, minutes: int = 720) -> Response:
    max_age = minutes * 60
    response.set_cookie(
        security.SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )
    return response


def _bundle_cache_key(split: str, days: int, max_decisions: int) -> str:
    return f"{split}:{days}:{max_decisions}"


def invalidate_bundle_cache() -> None:
    _CACHE.clear()


def _cached_bundle(conn, settings: Settings, *, split: str, days: int, max_decisions: int) -> dict[str, Any]:
    key = _bundle_cache_key(split, days, max_decisions)
    hit = _CACHE.get(key)
    now = time.time()
    if hit and now - hit[0] < BUNDLE_TTL_SECONDS:
        return hit[1]
    members = build_bundle(conn, settings, split_name=split, days=days, max_decisions=max_decisions)
    _CACHE[key] = (now, members)
    return members


# --------------------------------------------------------------------------- public reads


def healthz(request: Request) -> JSONResponse:
    payload: dict[str, Any] = {"ok": True, "version": VERSION, "time": datetime.now(tz=timezone.utc).isoformat()}
    try:
        with dbconn.sync_conn() as conn:
            row = conn.execute("select 1 as ok").fetchone()
            payload["database"] = "ok" if row and row["ok"] == 1 else "unexpected"
            payload["password_set"] = security.panel_password_hash(conn) is not None
    except Exception as exc:  # noqa: BLE001
        log.exception("healthz failed")
        payload["ok"] = False
        payload["database"] = f"error: {type(exc).__name__}: {exc}"
    return _json(payload, status=200 if payload["ok"] else 503)


def api_meta(request: Request) -> JSONResponse:
    with dbconn.sync_conn() as conn:
        settings = _settings_with_db(conn)
        split = repo.get_run_config(conn, "split") or {}
        return _json(
            {
                "version": VERSION,
                "demo": bool(settings.gm_demo_mode),
                "jev": {
                    "provider": settings.jev_provider_resolved,
                    "model": settings.jev_model_resolved,
                    "host": settings.jev_host,
                    "mock": bool(settings.gm_demo_mode or not settings.jev_api_key),
                },
                "split": split,
                "session_required": True,
            }
        )


def api_bundle(request: Request) -> Response:
    member = request.path_params.get("member") or "meta.json"
    if ".." in member or member.startswith("/"):
        return _error(400, "bad_member", "invalid member name")
    if not member.endswith(".json"):
        # `/api/bundle/live` and `/api/bundle/timeline/2026-06-01` both name a bundle file.
        member += ".json"
    split = request.query_params.get("split", "discovery")
    days = int(request.query_params.get("days", "45"))
    max_decisions = int(request.query_params.get("max_decisions", "60"))
    try:
        with dbconn.sync_conn() as conn:
            guard = _require_session(conn, request)
            if guard:
                return guard
            settings = _settings_with_db(conn)
            members = _cached_bundle(conn, settings, split=split, days=days, max_decisions=max_decisions)
    except ValueError as exc:
        return _error(404, "no_bundle", str(exc))
    payload = members.get(member)
    if payload is None:
        return _error(404, "no_member", f"{member} is not in the bundle")
    return Response(
        json.dumps(payload, default=str, ensure_ascii=False),
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


# --------------------------------------------------------------------------- auth


def auth_status(request: Request) -> JSONResponse:
    with dbconn.sync_conn() as conn:
        token = _session_token(request)
        return _json(
            {
                "password_set": security.panel_password_hash(conn) is not None,
                "authenticated": security.session_valid(conn, token),
                "session_minutes": _session_minutes(conn),
            }
        )


async def auth_login(request: Request) -> JSONResponse:
    body = await _read_json(request)
    password = str((body or {}).get("password", ""))
    with dbconn.sync_conn() as conn:
        allowed, failures = security.login_allowed(conn)
        if not allowed:
            return _error(429, "too_many_attempts",
                          f"{failures} failed logins in the last {security.LOGIN_WINDOW_MINUTES} minutes — wait a bit")
        stored = security.panel_password_hash(conn)
        if stored is None:
            return _error(503, "no_password",
                          "no panel password is configured: set UI_ADMIN_PASSWORD in the environment, "
                          "or create one with POST /api/auth/setup")
        if not security.verify_password(password, stored):
            security.record_attempt(conn, ok=False, note=request.client.host if request.client else "")
            return _error(401, "bad_password", "wrong password")
        security.record_attempt(conn, ok=True)
        security.purge_expired_sessions(conn)
        minutes = _session_minutes(conn)
        session = security.create_session(conn, minutes=minutes,
                                          label=request.headers.get("user-agent", "")[:120])
        repo.audit(conn, "panel", "login", {"from": request.client.host if request.client else "?"})
        conn.commit()
    response = _json({"ok": True, "expires_at": session.expires_at.isoformat(), "token": session.token})
    return _set_cookie(response, session.token, secure=request.url.scheme == "https", minutes=minutes)


async def auth_setup(request: Request) -> JSONResponse:
    """First-run password creation. Only possible while NO password exists anywhere."""
    body = await _read_json(request)
    password = str((body or {}).get("password", ""))
    if len(password) < 10:
        return _error(400, "weak_password", "use at least 10 characters")
    with dbconn.sync_conn() as conn:
        if security.panel_password_hash(conn) is not None:
            return _error(409, "already_set", "a panel password already exists; use POST /api/auth/password while logged in")
        security.set_password(conn, password, updated_by="first-run")
        security.record_attempt(conn, ok=True, note="first-run setup")
        repo.audit(conn, "panel", "password_created", {"from": request.client.host if request.client else "?"})
        conn.commit()
        minutes = _session_minutes(conn)
        session = security.create_session(conn, minutes=minutes,
                                          label=request.headers.get("user-agent", "")[:120])
    response = _json({"ok": True, "expires_at": session.expires_at.isoformat(), "token": session.token})
    return _set_cookie(response, session.token, secure=request.url.scheme == "https", minutes=minutes)


async def auth_logout(request: Request) -> JSONResponse:
    with dbconn.sync_conn() as conn:
        security.destroy_session(conn, _session_token(request))
    response = _json({"ok": True})
    response.delete_cookie(security.SESSION_COOKIE, path="/")
    return response


async def auth_change_password(request: Request) -> JSONResponse:
    body = await _read_json(request)
    current = str((body or {}).get("current_password", ""))
    new = str((body or {}).get("new_password", ""))
    if len(new) < 10:
        return _error(400, "weak_password", "use at least 10 characters")
    with dbconn.sync_conn() as conn:
        guard = _require_session(conn, request)
        if guard:
            return guard
        if not security.verify_password(current, security.panel_password_hash(conn)):
            security.record_attempt(conn, ok=False, note="change-password")
            return _error(401, "bad_password", "current password is wrong")
        security.set_password(conn, new, updated_by="panel")
        repo.audit(conn, "panel", "password_changed", {})
        conn.commit()
    return _json({"ok": True})


# --------------------------------------------------------------------------- admin API


def admin_integrations(request: Request) -> JSONResponse:
    with dbconn.sync_conn() as conn:
        guard = _require_session(conn, request)
        if guard:
            return guard
        settings = _settings_with_db(conn)
        return _json({
            "groups": settings_store.describe(conn, settings),
            "worker": {"version": VERSION, "demo": bool(settings.gm_demo_mode)},
        })


async def admin_set_integration(request: Request) -> JSONResponse:
    name = request.path_params["name"]
    field = settings_store.FIELD_INDEX.get(name)
    if field is None:
        return _error(404, "unknown_field", f"{name} is not a panel-managed setting")
    body = await _read_json(request)
    value = (body or {}).get("value")
    if value is None:
        return _error(400, "no_value", "send {\"value\": \"...\"} (empty string clears the override)")
    text = str(value)
    with dbconn.sync_conn() as conn:
        guard = _require_session(conn, request)
        if guard:
            return guard
        if name == "UI_ADMIN_PASSWORD":
            if len(text) < 10:
                return _error(400, "weak_password", "use at least 10 characters")
            security.set_password(conn, text, updated_by="panel")
        elif text == "":
            security.set_secret(conn, name, None)
        else:
            problem = settings_store.validate(field, text)
            if problem:
                return _error(400, "bad_value", problem)
            try:
                settings_store._coerce(field, _settings_with_db(conn), text)
            except Exception as exc:  # noqa: BLE001
                return _error(400, "bad_value", f"{name} expects {field.kind}: {exc}")
            security.set_secret(conn, name, text, updated_by="panel")
        repo.audit(conn, "panel", "integration_set", {"name": name, "cleared": text == ""})
        conn.commit()
    invalidate_bundle_cache()
    return _json({"ok": True, "name": name})


async def admin_clear_integration(request: Request) -> JSONResponse:
    name = request.path_params["name"]
    field = settings_store.FIELD_INDEX.get(name)
    if field is None:
        return _error(404, "unknown_field", f"{name} is not a panel-managed setting")
    with dbconn.sync_conn() as conn:
        guard = _require_session(conn, request)
        if guard:
            return guard
        security.set_secret(conn, name, None)
        repo.audit(conn, "panel", "integration_cleared", {"name": name})
        conn.commit()
    invalidate_bundle_cache()
    return _json({"ok": True, "name": name, "cleared": True})


async def admin_run_config(request: Request) -> JSONResponse:
    body = await _read_json(request)
    key = str((body or {}).get("key", ""))
    if not key:
        return _error(400, "no_key", "send {\"key\": ..., \"value\": ...}")
    value = (body or {}).get("value")
    with dbconn.sync_conn() as conn:
        guard = _require_session(conn, request)
        if guard:
            return guard
        conn.execute(
            """
            insert into run_config (key, value_json, set_at, set_by)
            values (%s, %s, now(), %s)
            on conflict (key) do update set value_json = excluded.value_json, set_at = now(), set_by = excluded.set_by
            """,
            (key, jsonb(value), "panel"),
        )
        repo.audit(conn, "panel", "run_config_set", {"key": key})
        conn.commit()
    invalidate_bundle_cache()
    return _json({"ok": True, "key": key})


async def admin_test(request: Request) -> JSONResponse:
    key = request.path_params["group"]
    if key not in {group.test for group in settings_store.GROUPS}:
        return _error(404, "unknown_group", f"no test named {key!r}")
    with dbconn.sync_conn() as conn:
        guard = _require_session(conn, request)
        if guard:
            return guard
        settings = _settings_with_db(conn)
        try:
            result = await _run_test(key, conn, settings)
        except Exception as exc:  # noqa: BLE001 - a test endpoint reports, it never raises
            result = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        repo.audit(conn, "panel", "integration_test", {"group": key, "ok": result.get("ok")})
        conn.commit()
    return _json(result)


# --------------------------------------------------------------------------- webhook


async def webhook(request: Request) -> JSONResponse:
    """Signed fire-and-forget trigger. An external scheduler pings this; the worker returns
    immediately and does the work in the background (no platform cron is required, and the free
    tier is never asked to run a long request)."""
    raw = await request.body()
    secret = os.environ.get("WORKER_WEBHOOK_SECRET", "")
    if not secret:
        return _error(503, "no_secret", "WORKER_WEBHOOK_SECRET is not configured")
    signature = request.headers.get("x-mishkah-signature", "")
    expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return _error(401, "bad_signature", "signature does not match the body")
    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return _error(400, "bad_json", "body must be JSON")
    action = str(body.get("action", "ping"))
    with dbconn.sync_conn() as conn:
        repo.audit(conn, "webhook", "accepted", {"action": action})
        conn.commit()
    if action == "export":
        invalidate_bundle_cache()
        return _json({"accepted": True, "action": action, "note": "bundle cache invalidated; the next read rebuilds it"})
    if action == "ping":
        return _json({"accepted": True, "action": action})
    return _error(501, "not_implemented",
                  f"action {action!r} is not wired yet — long work runs on the worker, not in a web request")


# --------------------------------------------------------------------------- internals


async def _read_json(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return None
    return body if isinstance(body, dict) else None


async def _run_test(key: str, conn, settings: Settings) -> dict[str, Any]:
    if key == "jev":
        try:
            settings.verify_jev_host()
        except (ConfigError, JevHostNotAllowed) as exc:
            return {"ok": False, "detail": f"host refused before sending anything: {exc}"}
        warning = settings.jev_third_party_warning()
        if settings.gm_demo_mode or not settings.jev_api_key:
            client = JevClient(settings, conn, mock=True)
            response = await client.ask({"note": "panel connectivity check"}, TEST_QUESTION)
            return {
                "ok": True,
                "mock": True,
                "detail": "demo mode or no key: the mock transport answered. This is NOT a real Jev call.",
                "model": response.model,
                "endpoint": settings.jev_endpoint,
            }
        client = JevClient(settings, conn)
        started = time.time()
        response = await client.ask({"note": "panel connectivity check"}, {"connection_ok": TEST_QUESTION["connection_ok"]})
        return {
            "ok": True,
            "mock": False,
            "detail": f"real call answered in {(time.time() - started) * 1000:.0f} ms",
            "model": response.model,
            "endpoint": settings.jev_endpoint,
            "answer": response.answers.get("connection_ok").as_dict() if response.answers.get("connection_ok") else None,
            "warning": warning,
        }
    if key == "telegram":
        # find_spec, not an import: the read-only ingestion module is the only place allowed to
        # import the Telegram library (unit test T10 greps for exactly that).
        from importlib.util import find_spec

        installed = find_spec("telethon") is not None
        return {
            "ok": installed and bool(settings.telegram_api_id) and bool(settings.telegram_api_hash),
            "detail": ("telethon installed" if installed else "telethon is NOT installed in this deployment") +
                      f"; api_id {'set' if settings.telegram_api_id else 'missing'}, "
                      f"api_hash {'set' if settings.telegram_api_hash else 'missing'}, "
                      f"session {'set' if settings.telegram_session else 'missing'}",
        }
    if key == "openrouter":
        if not settings.openrouter_api_key:
            return {"ok": False, "detail": "OPENROUTER_API_KEY is not set (extraction and narrative stay disabled)"}
        import httpx

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                f"{settings.openrouter_base_url.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
            )
        return {"ok": resp.status_code == 200, "detail": f"GET /models -> HTTP {resp.status_code}",
                "extraction_models": settings.extraction_models, "explainer_model": settings.explainer_model}
    if key == "prices":
        if settings.gm_demo_mode:
            return {"ok": True, "demo": True, "detail": "demo mode: price sources are not contacted at all"}
        import httpx

        url = f"{settings.nobitex_base.rstrip('/')}/market/udf/history"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params={"symbol": "USDTIRT", "resolution": "60", "from": 1, "to": 2})
        return {"ok": resp.status_code < 500, "detail": f"GET {url} -> HTTP {resp.status_code}"}
    if key == "panel":
        sessions = conn.execute("select count(*) as n from ui_sessions where expires_at > now()").fetchone()
        try:
            key_source = "env" if os.environ.get(security.MASTER_KEY_ENV) else "local file"
        except Exception:  # noqa: BLE001
            key_source = "unknown"
        return {
            "ok": True,
            "detail": f"master key: {key_source}; password: {'set' if security.panel_password_hash(conn) else 'NOT set'}; "
                      f"active sessions: {int(sessions['n'] if sessions else 0)}",
        }
    return {"ok": False, "detail": f"no test named {key!r}"}


# --------------------------------------------------------------------------- static hosting

LANDING = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Mishkah worker</title>
<style>
 body{{background:#0B0E14;color:#E6EDF3;font:14px/1.6 ui-sans-serif,system-ui,sans-serif;margin:0;padding:48px}}
 code,pre{{color:#22D3EE;font-family:ui-monospace,SFMono-Regular,monospace}}
 a{{color:#34D399}} .dim{{color:#8B949E}} .amber{{color:#F59E0B}}
 .card{{border:1px solid #1F2937;background:#111827;border-radius:8px;padding:16px 20px;margin:12px 0;max-width:760px}}
</style></head><body>
<h1>Mishkah · worker</h1>
<p class="dim">Research + paper-trading monitor for Iranian physical gold. Phase 1: research and paper only.</p>
<div class="card"><b>The API is running.</b>
 <ul>
  <li><a href="/healthz">/healthz</a> — liveness, database, whether a panel password exists</li>
  <li><a href="/api/meta">/api/meta</a> — version, demo flag, resolved Jev route, frozen split</li>
  <li><a href="/api/bundle/meta">/api/bundle/meta</a> — the UI data bundle (same read path as the CLI)</li>
  <li><a href="/data/meta.json">/data/meta.json</a> — the exported bundle, as files</li>
 </ul>
</div>
<div class="card"><b>Panel</b>
 <p>Open the Mission Control UI and set a password there (or set <code>UI_ADMIN_PASSWORD</code> in the environment).
 Panel-managed API keys are stored AES-256-GCM encrypted and are never returned to a browser.</p>
</div>
<div class="card"><b class="amber">The UI bundle is not built into this deployment.</b>
 <p class="dim">Serve the exported Mission Control:</p>
 <pre>cd ui &amp;&amp; npm ci &amp;&amp; npm run build      # writes ui/out/</pre>
 <p class="dim">then restart the worker: it serves <code>ui/out</code> at <code>/</code> automatically.</p>
</div>
</body></html>
"""


def bundle_dir() -> Path:
    """Where `ui export` writes the bundle.

    `MISHKAH_BUNDLE_DIR` lets a read-only container (Render) or a mounted disk decide, instead of
    hardcoding a path inside the image. The panel does not depend on it: the browser reads the
    bundle from `/api/bundle/*`, which is built from the database and therefore always current.
    """
    return Path(os.environ.get("MISHKAH_BUNDLE_DIR") or (REPO_ROOT / "ui" / "public" / "data"))


def _ui_export_dir() -> Path:
    return REPO_ROOT / "ui" / "out"


def create_app() -> Starlette:
    routes = [
        Route("/healthz", healthz),
        Route("/api/meta", api_meta),
        Route("/api/bundle/{member:path}", api_bundle),
        Route("/api/auth/status", auth_status, methods=["GET"]),
        Route("/api/auth/login", auth_login, methods=["POST"]),
        Route("/api/auth/setup", auth_setup, methods=["POST"]),
        Route("/api/auth/logout", auth_logout, methods=["POST"]),
        Route("/api/auth/password", auth_change_password, methods=["POST"]),
        Route("/api/admin/integrations", admin_integrations, methods=["GET"]),
        Route("/api/admin/integrations/{name}", admin_set_integration, methods=["PUT"]),
        Route("/api/admin/integrations/{name}", admin_clear_integration, methods=["DELETE"]),
        Route("/api/admin/run_config", admin_run_config, methods=["POST"]),
        Route("/api/admin/test/{group}", admin_test, methods=["POST"]),
        Route("/webhook", webhook, methods=["POST"]),
    ]
    routes.extend(_static_routes())
    return Starlette(routes=routes, debug=False)


def _static_routes() -> list[Any]:
    """Serve what a single-service deployment needs, and never lie about what is missing.

    `/data` is the exported bundle (the UI fetches it at runtime). `/` is the Mission Control
    static export when it has been built; otherwise a landing page that says exactly what to run.
    Everything stays on ONE origin, so the browser needs no CORS, no proxy and no localhost.
    """
    from starlette.responses import HTMLResponse
    from starlette.staticfiles import StaticFiles

    routes: list[Any] = []
    export = _ui_export_dir()
    if (export / "index.html").exists():
        routes.append(Mount("/", app=StaticFiles(directory=str(export), html=True), name="ui"))
    else:
        async def landing(request: Request) -> Response:  # pragma: no cover - trivial
            return HTMLResponse(LANDING)

        routes.append(Route("/", landing))
    return routes


app = create_app()


__all__ = ["app", "bundle_dir", "create_app", "invalidate_bundle_cache", "VERSION", "LANDING"]

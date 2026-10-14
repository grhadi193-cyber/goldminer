"""Panel-managed configuration (M9): the list of settings the operator may change from the web
panel, the type coercion, and the merge of stored values over the environment.

Order of precedence, in ONE place so it cannot drift:

    explicit panel value (encrypted in `app_secrets`)
        >  process environment / .env
        >  package default

Why the panel may write anything at all: the operator has to rotate a Jev key or switch provider
without shell access, and a research pipeline whose keys live only in a dashboard nobody can reach
is not operable. Why it is safe: the panel only ever writes to `app_secrets`, values are encrypted
with a key that never enters the database, no API echoes a secret back, and every write goes
through the audit log.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg

from . import security
from .config import JEV_PROVIDER_PRESETS, Settings, get_settings


@dataclass(frozen=True)
class Field:
    name: str                 # the ENV name, which is also the app_secrets.name
    label: str
    help: str
    secret: bool = False
    kind: str = "text"        # text | password | select | bool | number
    options: tuple[str, ...] = ()
    settings_field: str = ""  # Settings attribute (defaults to the lowercase name)


@dataclass(frozen=True)
class Group:
    key: str
    title: str
    subtitle: str
    test: str | None
    fields: tuple[Field, ...]


def _f(name: str, label: str, help_: str, **kw: Any) -> Field:
    return Field(name=name, label=label, help=help_, settings_field=kw.pop("settings_field", name.lower()), **kw)


GROUPS: tuple[Group, ...] = (
    Group(
        key="jev",
        title="Interview model — Jev",
        subtitle="The five-question day panel. Nothing else in the system is allowed to decide; this is "
                 "the only model that sees the prepared state.",
        test="jev",
        fields=(
            _f("JEV_PROVIDER", "Provider",
               "typesafe = api.typesafe.ai (official). opencode = OpenCode Console/Zen with the "
               "limited-time free model jev-1.13-free. Anything you set explicitly below wins.",
               kind="select", options=("", *sorted(JEV_PROVIDER_PRESETS))),
            _f("JEV_API_KEY", "API key", "Stored encrypted; never shown again. Paste a fresh key to rotate it.",
               secret=True, kind="password"),
            _f("JEV_BASE_URL", "Base URL override", "Leave empty to use the provider preset.", kind="text"),
            _f("JEV_MODEL", "Model override", "Empty = preset model (typesafe: jev-latest, opencode: jev-1.13-free)."),
            _f("JEV_ALLOWED_HOSTS", "Allowed hosts",
               "Comma separated. EMPTY = the preset host. The client refuses to send anything to a host "
               "that is not listed here — that refusal is the only technical guard on egress."),
        ),
    ),
    Group(
        key="openrouter",
        title="Extraction + narrative",
        subtitle="Cheap models do Persian→English strict-JSON extraction only. One strong model writes the "
                 "final narrative and proposes hypotheses; it never computes a number.",
        test="openrouter",
        fields=(
            _f("OPENROUTER_API_KEY", "OpenRouter API key", "Used for extraction and (optionally) the narrative.",
               secret=True, kind="password"),
            _f("EXTRACTION_MODELS", "Extraction model chain",
               "Comma separated, first model that returns valid JSON wins."),
            _f("EXPLAINER_MODEL", "Narrative model", "Leave empty to skip the narrative entirely."),
        ),
    ),
    Group(
        key="telegram",
        title="Telegram ingestion (read-only)",
        subtitle="A dedicated NON-personal account. The worker has no send/post/forward code path at all; "
                 "a unit test greps the source to keep it that way.",
        test="telegram",
        fields=(
            _f("TELEGRAM_API_ID", "API id", "From my.telegram.org.", kind="number"),
            _f("TELEGRAM_API_HASH", "API hash", "From my.telegram.org.", secret=True, kind="password"),
            _f("TELEGRAM_SESSION", "Session string",
               "Telethon StringSession. Treat it like a password: it IS the login.", secret=True, kind="password"),
            _f("TELEGRAM_CHANNELS", "Channels", "Comma separated @usernames to backfill."),
        ),
    ),
    Group(
        key="prices",
        title="Price sources",
        subtitle="Domestic gold/coin, free-market USD (USDTIRT), and the ounce.",
        test="prices",
        fields=(
            _f("NOBITEX_BASE", "Nobitex base URL", "UDF history endpoint host for USDTIRT."),
            _f("XAUUSD_PROVIDER", "Ounce provider", "yfinance by default."),
            _f("TGJU_ENABLED", "TGJU enabled", "Unofficial domestic quotes.", kind="bool"),
            _f("TSETMC_ENABLED", "TSETMC enabled", "Tradable gold-fund proxy (~0.1 g per unit).", kind="bool"),
            _f("NOBITEX_MAX_REQ_PER_MIN", "Nobitex requests / minute", "Stay well under the public limit.",
               kind="number"),
        ),
    ),
    Group(
        key="panel",
        title="Panel + worker",
        subtitle="Access and scheduling for this deployment.",
        test="panel",
        fields=(
            _f("UI_ADMIN_PASSWORD", "Panel password",
               "Stored as a scrypt hash, never in plaintext, never returned. Changing it invalidates nothing "
               "already logged in, so log out on shared machines.", secret=True, kind="password"),
            _f("UI_SESSION_MINUTES", "Session length (minutes)", "How long a browser stays logged in.",
               kind="number"),
            _f("WORKER_WEBHOOK_SECRET", "Webhook secret",
               "HMAC secret for the fire-and-forget trigger endpoint (external scheduler → worker).",
               secret=True, kind="password"),
            _f("GM_DEMO_MODE", "Demo mode", "1 = synthetic data, mock Jev transport, no network calls.",
               kind="bool"),
        ),
    ),
)

FIELD_INDEX: dict[str, Field] = {f.name: f for group in GROUPS for f in group.fields}


def known(field: Field) -> bool:
    """True when the ENV name maps onto a real Settings field (panel-only knobs do not)."""
    return field.settings_field in Settings.model_fields


def field_default(field: Field) -> Any:
    return Settings.model_fields[field.settings_field].default if known(field) else None


def validate(field: Field, raw: str | None) -> str | None:
    """Returns a human error, or None when the value is acceptable. Empty means 'clear it'."""
    text = (raw or "").strip()
    if not text:
        return None
    if field.kind == "select":
        allowed = [option for option in field.options if option]
        if text not in allowed:
            return f"{field.name} must be one of: {', '.join(allowed)} — or empty to use the preset"
    elif field.kind == "bool":
        if text.lower() not in ("0", "1", "true", "false", "yes", "no", "on", "off"):
            return f"{field.name} must be 0/1 (or true/false)"
    elif field.kind == "number":
        try:
            float(text)
        except ValueError:
            return f"{field.name} must be a number"
    return None


def _coerce(field: Field, settings: Settings, raw: str | None) -> Any:
    if raw is None or raw == "":
        return None
    if not known(field):
        return raw.strip()
    annotation = Settings.model_fields[field.settings_field].annotation
    text = raw.strip()
    if annotation is bool:
        return text.lower() in ("1", "true", "yes", "on")
    if annotation is int:
        return int(float(text))
    if annotation is float:
        return float(text)
    if annotation is int | None or str(annotation).endswith("| None"):
        if annotation is int | None:
            return int(float(text))
        return text
    return text


def stored_overrides(conn: psycopg.Connection, settings: Settings) -> dict[str, Any]:
    """Panel values that differ from what the environment already provides."""
    overrides: dict[str, Any] = {}
    for name, field in FIELD_INDEX.items():
        if name == "UI_ADMIN_PASSWORD" or not known(field):
            continue  # handled by the auth code / read directly, never merged into Settings
        try:
            value = security.get_secret(conn, name, default=None)
        except Exception:  # noqa: BLE001 - a broken ciphertext must surface, not silently pass
            raise
        if value is None:
            continue
        try:
            coerced = _coerce(field, settings, value)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"stored value for {name} cannot be parsed: {exc}") from exc
        if coerced is not None:
            overrides[field.settings_field] = coerced
    return overrides


def apply_overrides(settings: Settings, overrides: dict[str, Any]) -> Settings:
    if not overrides:
        return settings
    data = settings.model_dump()
    data.update(overrides)
    return Settings(**data)


def effective_settings(conn: psycopg.Connection | None = None, *, base: Settings | None = None) -> Settings:
    """Settings with panel values applied. This is what every long-running process should use."""
    settings = base or get_settings()
    if conn is None:
        return settings
    return apply_overrides(settings, stored_overrides(conn, settings))


def describe(conn: psycopg.Connection, settings: Settings) -> list[dict[str, Any]]:
    """Panel-safe view: values for non-secrets, fingerprints for secrets, never a secret value."""
    stored = security.list_secrets(conn)
    out: list[dict[str, Any]] = []
    for group in GROUPS:
        fields: list[dict[str, Any]] = []
        for field in group.fields:
            meta = stored.get(field.name)
            env_value = runtime_value(conn, field.name, default=None)
            db_value: str | None = None
            if meta is not None and field.name != "UI_ADMIN_PASSWORD":
                db_value = security.get_secret(conn, field.name, default=None)
            elif meta is not None:
                db_value = "********"
            effective_attr = getattr(settings, field.settings_field, None) if known(field) else env_value
            entry: dict[str, Any] = {
                "name": field.name,
                "label": field.label,
                "help": field.help,
                "kind": field.kind,
                "options": list(field.options),
                "secret": field.secret,
                "source": "panel" if meta else ("env" if env_value else "unset"),
                "fingerprint": meta["fingerprint"] if meta else (security.fingerprint(env_value) if env_value else None),
                "updated_at": meta["updated_at"] if meta else None,
                "updated_by": meta["updated_by"] if meta else None,
            }
            if field.secret:
                # "********" means 'a value is stored here'; the value itself never leaves the worker.
                entry["value"] = "********" if meta else None
            else:
                entry["value"] = db_value if db_value is not None else env_value
            entry["effective"] = None if field.secret else effective_attr
            fields.append(entry)
        out.append(
            {"key": group.key, "title": group.title, "subtitle": group.subtitle, "test": group.test, "fields": fields}
        )
    return out


def runtime_value(conn: psycopg.Connection | None, name: str, *, default: str | None = None) -> str | None:
    """A panel-managed value if one exists, else the process environment. Used for knobs that are
    not `Settings` fields (session length, master key is refused on purpose)."""
    import os

    if conn is not None:
        try:
            value = security.get_secret(conn, name, default=None)
        except Exception:  # noqa: BLE001 - fall back to the environment rather than 500
            value = None
        if value:
            return value
    return os.environ.get(name) or default


__all__ = [
    "FIELD_INDEX",
    "GROUPS",
    "validate",
    "field_default",
    "known",
    "runtime_value",
    "Field",
    "Group",
    "apply_overrides",
    "describe",
    "effective_settings",
    "stored_overrides",
]

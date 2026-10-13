"""Worker configuration (all env-driven; no hosts/keys/model names hardcoded — R2/R13.2)."""

from __future__ import annotations

import os
from datetime import date, time
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import ConfigError, JevHostNotAllowed

REPO_ROOT = Path(__file__).resolve().parents[2]

# The only host in TypeSafe's public documentation. Anything else is treated as an
# UNVERIFIED third party and warned about on every run (R2).
OFFICIAL_JEV_HOST = "api.typesafe.ai"

_WEEKDAY_NAMES = {
    "mon": 0, "monday": 0,
    "tue": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}


def _csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [p.strip() for p in value.replace("\n", ",").split(",") if p.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", Path.cwd() / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # database
    database_url: str = ""
    database_url_direct: str = ""

    # telegram
    telegram_api_id: int | None = None
    telegram_api_hash: str = ""
    telegram_session: str = ""
    telegram_channels: str = ""

    # jev
    jev_base_url: str = "https://api.typesafe.ai"
    jev_api_key: str = ""
    jev_allowed_hosts: str = OFFICIAL_JEV_HOST
    jev_model: str = "jev-latest"
    jev_timeout_s: float = 20.0
    jev_max_concurrency: int = 3

    # openrouter
    openrouter_api_key: str = ""
    extraction_models: str = ""
    explainer_model: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # price sources
    nobitex_base: str = "https://apiv2.nobitex.ir"
    xauusd_provider: str = "yfinance"
    tgju_enabled: bool = True
    tsetmc_enabled: bool = True
    tsetmc_symbols: str = ""
    nobitex_max_req_per_min: int = 30

    # worker/ui
    worker_webhook_secret: str = ""
    worker_base_url: str = "http://127.0.0.1:8787"
    worker_port: int = 8787
    app_timezone: str = "Asia/Tehran"

    # research window
    start_date: date | None = None
    history_months: int = 6
    discovery_months: int = 4
    checkpoints_local: str = "08:00,12:00,16:00"
    trading_weekdays: str = "5,6,0,1,2"
    flat_band_bps: float = 15.0

    # risk / costs
    spread_bps_gold: float = 30.0
    slippage_bps_gold: float = 15.0
    spread_bps_coin: float = 50.0
    slippage_bps_coin: float = 25.0
    max_spread_bps_tradeable: float = 60.0
    jev_min_confidence: float = 0.60
    position_size_g: float = 1000.0
    stop_loss_bps: float = 100.0
    daily_loss_cap_bps: float = 200.0
    flat_at_local: str = "22:00"
    open_at_local: str = "08:00"
    paper_only: bool = True

    # demo / tests
    gm_demo_mode: int = 0
    gm_demo_database_url: str = ""

    # ---- derived helpers -------------------------------------------------------------

    @property
    def db_url(self) -> str:
        if self.gm_demo_mode and self.gm_demo_database_url:
            return self.gm_demo_database_url
        if not self.database_url:
            raise ConfigError(
                "DATABASE_URL is not set. Copy .env.example to .env and fill it in "
                "(Supabase/Neon pooled connection string)."
            )
        return self.database_url

    @property
    def migration_db_url(self) -> str:
        return self.database_url_direct or self.db_url

    @property
    def allowed_jev_hosts(self) -> list[str]:
        return [h.lower() for h in _csv(self.jev_allowed_hosts)]

    @property
    def jev_host(self) -> str:
        return (urlparse(self.jev_base_url).hostname or "").lower()

    @property
    def jev_endpoint(self) -> str:
        """Full /v1/systemone endpoint, tolerant of operators pasting it into JEV_BASE_URL."""
        base = self.jev_base_url.rstrip("/")
        if base.endswith("/v1/systemone"):
            return base
        if base.endswith("/v1"):
            return f"{base}/systemone"
        return f"{base}/v1/systemone"

    @property
    def jev_is_official_host(self) -> bool:
        return self.jev_host == OFFICIAL_JEV_HOST

    def jev_third_party_warning(self) -> str | None:
        """Non-None when the configured Jev host is not the officially documented one."""
        if self.jev_is_official_host:
            return None
        return (
            f"JEV_BASE_URL points at {self.jev_host!r}, which is NOT the TypeSafe-documented "
            f"host ({OFFICIAL_JEV_HOST}). Treat it as an UNVERIFIED THIRD PARTY: it may log, "
            "store, or reuse everything you send, including market signals and research "
            "state. You are responsible for verifying who operates that host and its "
            "data-retention policy. The allowed-host allowlist is the only technical guard."
        )

    def verify_jev_host(self) -> None:
        """Raise unless the configured Jev host is explicitly allowlisted (R2)."""
        host = self.jev_host
        if not host:
            raise ConfigError(f"JEV_BASE_URL is not a valid URL: {self.jev_base_url!r}")
        if host not in self.allowed_jev_hosts:
            raise JevHostNotAllowed(host, self.allowed_jev_hosts)

    @property
    def worker_secret_file(self) -> Path:
        return REPO_ROOT / ".secrets" / "worker_credentials.json"

    @property
    def checkpoint_times(self) -> list[time]:
        out: list[time] = []
        for raw in _csv(self.checkpoints_local):
            hh, _, mm = raw.partition(":")
            out.append(time(int(hh), int(mm or 0)))
        return sorted(out)

    @property
    def open_time(self) -> time:
        hh, _, mm = self.open_at_local.partition(":")
        return time(int(hh), int(mm or 0))

    @property
    def flat_time(self) -> time:
        hh, _, mm = self.flat_at_local.partition(":")
        return time(int(hh), int(mm or 0))

    @property
    def trading_weekday_set(self) -> set[int]:
        out: set[int] = set()
        for raw in _csv(self.trading_weekdays):
            token = raw.strip().lower()
            if token in _WEEKDAY_NAMES:
                out.add(_WEEKDAY_NAMES[token])
            elif token.isdigit():
                out.add(int(token))
            else:
                raise ConfigError(f"Unrecognised TRADING_WEEKDAYS entry: {raw!r}")
        if not out:
            raise ConfigError("TRADING_WEEKDAYS is empty")
        return out

    @property
    def extraction_model_list(self) -> list[str]:
        return _csv(self.extraction_models)

    @property
    def telegram_channel_list(self) -> list[str]:
        return [c.lstrip("@") for c in _csv(self.telegram_channels)]

    @property
    def tsetmc_symbol_list(self) -> list[str]:
        return _csv(self.tsetmc_symbols)

    @property
    def cost_bps(self) -> dict[str, dict[str, float]]:
        """Cost model per asset class (R5). Deterministic code, never an LLM."""
        return {
            "gold": {"spread_bps": self.spread_bps_gold, "slippage_bps": self.slippage_bps_gold},
            "coin": {"spread_bps": self.spread_bps_coin, "slippage_bps": self.slippage_bps_coin},
            "usdtirt": {"spread_bps": self.spread_bps_gold, "slippage_bps": self.slippage_bps_gold},
            "xauusd": {"spread_bps": 5.0, "slippage_bps": 5.0},
        }

    def assert_paper_only(self) -> None:
        """R6: Phase 1 must never place a real order. There is no such code path — this is
        the second line of defence, so a stray config flip is loudly rejected."""
        if not self.paper_only:
            raise ConfigError(
                "PAPER_ONLY=false is refused by this build. Phase 1 is research + paper "
                "trading only and no order-placing code path exists. Keeping the flag honest "
                "is deliberate: see README 'Non-negotiable rules'."
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()


def demo_mode() -> bool:
    return os.environ.get("GM_DEMO_MODE", "0") not in ("0", "", "false", "False")


__all__ = ["Settings", "get_settings", "reset_settings_cache", "demo_mode", "OFFICIAL_JEV_HOST", "Field"]

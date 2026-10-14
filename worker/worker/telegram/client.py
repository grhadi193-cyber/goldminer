"""Telethon client construction (read-only usage).

Use a DEDICATED NON-PERSONAL account: this worker logs into Telegram, and account safety is
the operator's responsibility (README §11). The session string lives in `TELEGRAM_SESSION`
and is never logged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Protocol

from ..config import Settings
from ..errors import ConfigError

log = logging.getLogger(__name__)


@dataclass
class RawMessage:
    """Provider-neutral view of one Telegram message version."""

    msg_id: int
    published_at: datetime          # Telegram's own message date -> event time (R3)
    text: str
    edit_date: datetime | None = None
    media_ref: str | None = None

    @property
    def published_utc(self) -> datetime:
        ts = self.published_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)

    @property
    def edit_utc(self) -> datetime | None:
        if self.edit_date is None:
            return None
        ts = self.edit_date
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)


class ReadOnlyTelegramClient(Protocol):
    async def iter_messages(self, username: str, *, since: datetime | None, limit: int | None) -> AsyncIterator[RawMessage]:
        ...

    async def resolve_title(self, username: str) -> str | None:
        ...


class TelethonReadOnlyClient:
    """Thin adapter over Telethon. Only read APIs are called here."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    async def connect(cls, settings: Settings) -> "TelethonReadOnlyClient":
        if not (settings.telegram_api_id and settings.telegram_api_hash and settings.telegram_session):
            raise ConfigError(
                "Telegram credentials incomplete: TELEGRAM_API_ID, TELEGRAM_API_HASH and "
                "TELEGRAM_SESSION are all required. See README §setup."
            )
        try:
            from telethon import TelegramClient  # noqa: PLC0415
            from telethon.sessions import StringSession  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ConfigError(
                "telethon is not installed. Install the 'telegram' extra: pip install -e worker[telegram]"
            ) from exc
        session = StringSession(settings.telegram_session)
        client = TelegramClient(session, settings.telegram_api_id, settings.telegram_api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            raise ConfigError("Telegram session is not authorised — generate it once with a dedicated account.")
        log.info("telegram client connected (read-only usage)")
        return cls(client)

    async def resolve_title(self, username: str) -> str | None:
        try:
            entity = await self._client.get_entity(username)
        except Exception as exc:  # noqa: BLE001 - a bad handle must not abort the whole run
            log.warning("cannot resolve %s: %s", username, exc)
            return None
        return getattr(entity, "title", None) or getattr(entity, "username", None)

    async def iter_messages(
        self, username: str, *, since: datetime | None, limit: int | None
    ) -> AsyncIterator[RawMessage]:
        from telethon import errors  # noqa: PLC0415

        kwargs: dict[str, Any] = {"limit": limit}
        if since is not None:
            kwargs["offset_date"] = since
            kwargs["reverse"] = True
        while True:
            try:
                async for message in self._client.iter_messages(username, **kwargs):
                    yield _to_raw(message)
                break
            except errors.FloodWaitError as exc:  # be polite (R9)
                wait = int(getattr(exc, "seconds", 60)) + 5
                log.warning("FloodWait for %ss on %s — sleeping (politely)", wait, username)
                import asyncio

                await asyncio.sleep(wait)

    async def close(self) -> None:
        await self._client.disconnect()


class FakeTelegramClient:
    """Deterministic read-only client for tests and offline dry-runs."""

    def __init__(self, messages: dict[str, list[RawMessage]] | None = None) -> None:
        self.messages = messages or {}

    async def resolve_title(self, username: str) -> str | None:
        return f"fake {username}"

    async def iter_messages(self, username: str, *, since: datetime | None, limit: int | None):
        rows = sorted(self.messages.get(username, []), key=lambda m: (m.published_utc, m.msg_id))
        count = 0
        for row in rows:
            if since is not None and row.published_utc < since:
                continue
            yield row
            count += 1
            if limit and count >= limit:
                break

    async def close(self) -> None:
        return None


def _to_raw(message: Any) -> RawMessage:
    media_ref = None
    if getattr(message, "media", None) is not None:
        media_ref = f"{type(message.media).__name__}"
    return RawMessage(
        msg_id=int(message.id),
        published_at=message.date,
        text=(message.message or ""),
        edit_date=getattr(message, "edit_date", None),
        media_ref=media_ref,
    )


__all__ = ["RawMessage", "ReadOnlyTelegramClient", "TelethonReadOnlyClient", "FakeTelegramClient"]

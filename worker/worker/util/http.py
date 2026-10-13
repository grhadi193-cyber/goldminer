"""HTTP helpers: one shared JSON-get with retry-on-transient, no retry on 4xx validation."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .ratelimit import Backoff

log = logging.getLogger(__name__)

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504, 522, 524, 529}


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str = "") -> None:
        self.status = status
        self.url = url
        self.body = body
        super().__init__(f"HTTP {status} for {url}: {body[:300]}")


async def get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    max_attempts: int = 4,
    backoff: Backoff | None = None,
    timeout: float | None = None,
) -> Any:
    """GET with retry on transient statuses. Raises HttpError on a hard 4xx (fail loudly)."""
    bo = backoff or Backoff()
    last: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = await client.get(url, params=params, headers=headers, timeout=timeout)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = exc
            log.warning("transient transport error on %s (attempt %d): %s", url, attempt, exc)
            if attempt == max_attempts:
                break
            await bo.sleep(attempt)
            continue
        if resp.status_code in RETRY_STATUS:
            log.warning("http %s on %s (attempt %d)", resp.status_code, url, attempt)
            last = HttpError(resp.status_code, url, resp.text)
            if attempt == max_attempts:
                raise last
            await bo.sleep(attempt)
            continue
        if resp.status_code >= 400:
            raise HttpError(resp.status_code, url, resp.text)
        return resp.json()
    raise last if last else HttpError(0, url, "unknown failure")

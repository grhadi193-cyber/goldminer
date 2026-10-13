"""Typed errors. Configuration problems must be loud, never silent fallbacks (R2)."""

from __future__ import annotations


class GoldminerError(Exception):
    """Base class for all worker errors."""


class ConfigError(GoldminerError):
    """Missing/invalid configuration."""


class JevHostNotAllowed(ConfigError):
    """`JEV_BASE_URL` host is not present in `JEV_ALLOWED_HOSTS` — refuse to send anything."""

    def __init__(self, host: str, allowed: list[str]) -> None:
        self.host = host
        self.allowed = allowed
        super().__init__(
            f"REFUSING to call Jev: host {host!r} is not in JEV_ALLOWED_HOSTS "
            f"({', '.join(allowed) if allowed else '<empty>'}).\n"
            "Before adding it, verify WHO operates that host and WHAT its data-retention "
            "policy is: every third-party gateway (anything other than api.typesafe.ai) may "
            "log or reuse the state you send, including your market signals and derived "
            "research state. The operator is responsible for that verification."
        )


class JevRequestError(GoldminerError):
    """Non-retryable Jev error (validation/4xx). Surfaced, never blind-retried (R2)."""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"Jev request rejected with HTTP {status}: {body[:800]}")


class JevUnavailable(GoldminerError):
    """429/529-style overload after the retry budget was exhausted."""


class SourceError(GoldminerError):
    """An upstream data source behaved unexpectedly (see docs/source_notes.md)."""


class CoverageError(GoldminerError):
    """A coverage acceptance criterion failed (R8 — never patch data to fit assumptions)."""


class NotEnoughData(GoldminerError):
    """Requested window has no data; callers must degrade explicitly, never fabricate."""


class AntiLeakageError(GoldminerError):
    """A read attempted to cross the as-of boundary (R3)."""

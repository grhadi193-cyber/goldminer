"""Logging setup: human-readable on TTY, single-line JSON otherwise (free-tier friendly)."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str | None = None) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    lvl = (level or os.environ.get("GM_LOG_LEVEL") or "INFO").upper()
    handler = logging.StreamHandler(sys.stderr)
    if sys.stderr.isatty():
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    else:
        handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(lvl)
    _CONFIGURED = True


def log_event(logger: logging.Logger, msg: str, **fields: Any) -> None:
    logger.info(msg, extra={"extra_fields": fields})

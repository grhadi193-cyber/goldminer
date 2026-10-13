"""T10 + the structural rules: read-only Telegram (R7), paper-only (R6), no order paths.

These are deliberately blunt instruments. A grep cannot prove the absence of a capability,
but combined with "no order API client exists in the dependency set" and the runtime refusal
in `Settings.assert_paper_only()`, it makes a violation loud and reviewable.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from worker.config import Settings
from worker.errors import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_SRC = REPO_ROOT / "worker" / "worker"
SCRIPTS = REPO_ROOT / "scripts"

# Telegram write/coordination APIs (R7): no ingestion or analysis module may reference them.
FORBIDDEN_TELEGRAM = [
    "send_message",
    "send_file",
    "send_media",
    "send_photo",
    "send_document",
    "forward_messages",
    "send_read_acknowledge",
    "edit_message",
    "delete_messages",
    "send_reaction",
    "create_channel",
    "create_group",
    "join_chat",
    "add_chat_user",
    "invite_to_channel",
    "promote_chat_member",
    "set_chat_photo",
    "send_code_request",
    "SignInRequest",
    "SendMessageRequest",
]

# Order-placing capabilities (R6): Phase 1 must not contain any of these.
FORBIDDEN_ORDER_API = [
    "place_order",
    "placeOrder",
    "submit_order",
    "create_order",
    "new_order",
    "createOrder",
    "cancelOrder",
    "/orders",
    "market_order",
    "limit_order",
    "post_only",
    "leverage",
]

# Files allowed to mention Telegram at all (read-only ingestion + docs of the rule).
TELEGRAM_ALLOWED_FILES = {
    "worker/worker/telegram/client.py",   # read-only adapter (connect + iter_messages only)
    "worker/worker/telegram/ingest.py",
    "worker/worker/telegram/__init__.py",
}

TELEGRAM_ALLOWED_FILES_ABS = {str((REPO_ROOT / p).resolve()) for p in TELEGRAM_ALLOWED_FILES}


def _python_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


@pytest.mark.parametrize("token", FORBIDDEN_TELEGRAM)
def test_no_telegram_write_api_anywhere(token: str):
    offenders: list[str] = []
    for path in _python_files(WORKER_SRC) + _python_files(SCRIPTS):
        text = path.read_text(encoding="utf-8")
        if token in text:
            offenders.append(f"{path.relative_to(REPO_ROOT)} contains {token!r}")
    assert not offenders, (
        "R7 violation — Telegram write/coordination API referenced:\n" + "\n".join(offenders) +
        "\nIngestion is READ-ONLY. If this is a false positive (e.g. a docstring), rename the "
        "reference so the grep stays meaningful."
    )


@pytest.mark.parametrize("token", FORBIDDEN_ORDER_API)
def test_no_order_placement_api_anywhere(token: str):
    offenders: list[str] = []
    for path in _python_files(WORKER_SRC) + _python_files(SCRIPTS):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if token in line and "FORBIDDEN_ORDER_API" not in line and "#" not in line.split(token)[0][-3:]:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {line.strip()[:120]}")
    assert not offenders, "R6 violation — order-placement API referenced:\n" + "\n".join(offenders)


def test_telethon_is_only_imported_by_the_read_only_ingestion_module():
    offenders: list[str] = []
    for path in _python_files(WORKER_SRC):
        text = path.read_text(encoding="utf-8")
        if re.search(r"^\s*(import telethon|from telethon)", text, re.MULTILINE):
            if str(path.resolve()) not in TELEGRAM_ALLOWED_FILES_ABS:
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"telethon imported outside the read-only ingestion module: {offenders}"


def test_paper_only_flag_cannot_be_turned_off():
    assert Settings(gm_demo_mode=0, paper_only=True).paper_only is True
    with pytest.raises(ConfigError):
        Settings(gm_demo_mode=0, paper_only=False).assert_paper_only()
    # the flag is not merely decorative: a false value must fail loudly at every call site
    settings = Settings(gm_demo_mode=0, paper_only=False)
    with pytest.raises(ConfigError):
        settings.assert_paper_only()


def test_paper_tables_only_accept_paper_mode(conn):
    import psycopg
    from psycopg.types.json import Jsonb

    row = conn.execute(
        "insert into paper_positions (day_date, opened_ts, direction, size_g, entry_price) "
        "values (%s, now(), 'long', 1000, 1000000) returning mode",
        (date(2026, 5, 6),),
    ).fetchone()
    assert row["mode"] == "paper"
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "insert into paper_positions (day_date, opened_ts, direction, size_g, entry_price, mode) "
            "values (%s, now(), 'long', 1000, 1000000, 'real')",
            (date(2026, 5, 6),),
        )
    _ = Jsonb


def test_no_broker_credentials_are_referenced():
    """Any environment variable that smells like a broker key would be a Phase-1 violation."""
    suspicious = ["EXCHANGE_API", "BROKER_API", "NOBITEX_TOKEN", "NOBITEX_API_KEY", "TELEGRAM_SEND"]
    for path in _python_files(WORKER_SRC) + [REPO_ROOT / ".env.example"]:
        text = path.read_text(encoding="utf-8")
        for token in suspicious:
            assert token not in text, f"{path.relative_to(REPO_ROOT)} references {token}"

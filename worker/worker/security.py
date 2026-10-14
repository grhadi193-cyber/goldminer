"""Panel security: password hashing, encrypted secret storage, sessions (M9 admin).

Threat model, stated plainly because it decides every choice below:

  * The panel is reachable from the internet once deployed. Therefore: ONE password, hashed with
    scrypt, never stored in plaintext anywhere, never returned by an API, and login is rate limited
    per window so the endpoint cannot be brute-forced at network speed.
  * API keys typed into the panel are secrets at rest. They are encrypted with AES-256-GCM under a
    master key that lives in the process environment (`MISHKAH_MASTER_KEY`) — NOT in the database.
    A database dump alone therefore reveals nothing usable.
  * The API never echoes a secret back. It returns a fingerprint (sha256 prefix + last 4 chars) so
    the operator can tell WHICH key is stored without seeing it.
  * Sessions are opaque random tokens; only their sha256 is stored, so a leak of `ui_sessions` does
    not hand out live sessions.

Losing `MISHKAH_MASTER_KEY` makes stored secrets unrecoverable by design: re-enter them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg

from .errors import ConfigError

log = logging.getLogger(__name__)

MASTER_KEY_ENV = "MISHKAH_MASTER_KEY"
KEY_FILE = Path(".secrets") / "master.key"
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
SESSION_COOKIE = "mishkah_session"
LOGIN_WINDOW_MINUTES = 15
MAX_FAILED_LOGINS = 8


# --------------------------------------------------------------------------- master key


def _key_file() -> Path:
    from .config import REPO_ROOT

    return REPO_ROOT / KEY_FILE


def master_key(*, create: bool = True) -> bytes:
    """`MISHKAH_MASTER_KEY` (base64/urlsafe or raw) or a local key file created on first use.

    On a deployed host the env var is the right answer (the filesystem may be ephemeral and an
    unrecoverable key that only lived on a recycled container is a support ticket waiting to
    happen). Locally the file keeps development friction low.
    """
    raw = os.environ.get(MASTER_KEY_ENV, "").strip()
    if raw:
        return _normalise_key(raw)
    path = _key_file()
    if path.exists():
        return _normalise_key(path.read_text(encoding="utf-8").strip())
    if not create:
        raise ConfigError(
            f"{MASTER_KEY_ENV} is not set and {path} does not exist — cannot read encrypted panel secrets"
        )
    key = secrets.token_urlsafe(48)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - platform dependent
        pass
    log.warning("generated a new local master key at %s — back it up; stored panel secrets need it", path)
    return _normalise_key(key)


def _normalise_key(raw: str) -> bytes:
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            candidate = decoder(raw + "=" * (-len(raw) % 4))
            if len(candidate) >= 32:
                return candidate[:32]
        except Exception:  # noqa: BLE001 - fall through to the digest form
            continue
    # Any sufficiently long passphrase is acceptable; derive a 32-byte key from it.
    return hashlib.scrypt(raw.encode("utf-8"), salt=b"mishkah-master-key", n=SCRYPT_N, r=SCRYPT_R, p=1, dklen=32)


# --------------------------------------------------------------------------- encryption


def encrypt(value: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    blob = AESGCM(master_key()).encrypt(nonce, value.encode("utf-8"), b"mishkah:v1")
    return "v1:" + base64.b64encode(nonce + blob).decode("ascii")


def decrypt(token: str | None) -> str | None:
    if not token:
        return None
    if not token.startswith("v1:"):
        raise ConfigError("stored secret has an unknown format — refusing to guess")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    raw = base64.b64decode(token[3:])
    nonce, blob = raw[:12], raw[12:]
    return AESGCM(master_key()).decrypt(nonce, blob, b"mishkah:v1").decode("utf-8")


def fingerprint(value: str | None) -> str | None:
    """Displayable identity of a secret: enough to recognise it, not enough to use it."""
    if not value:
        return None
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    tail = value[-4:] if len(value) >= 8 else ""
    return f"sha256:{digest}…{tail}"


# --------------------------------------------------------------------------- passwords


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=1, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        candidate = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except Exception:  # noqa: BLE001 - a malformed hash must never verify
        return False
    return hmac.compare_digest(candidate, expected)


# --------------------------------------------------------------------------- panel password


def panel_password_hash(conn: psycopg.Connection) -> str | None:
    row = conn.execute("select ciphertext from app_secrets where name = 'UI_ADMIN_PASSWORD'").fetchone()
    if row and row["ciphertext"]:
        return decrypt(row["ciphertext"])
    raw = os.environ.get("UI_ADMIN_PASSWORD", "").strip()
    if not raw:
        return None
    # Bootstrap: hash what the operator put in the environment and store the HASH (never the
    # password) so later changes can happen from the panel itself.
    stored = hash_password(raw)
    set_secret(conn, "UI_ADMIN_PASSWORD", stored, updated_by="env-bootstrap", already_hashed=True)
    return stored


def set_password(conn: psycopg.Connection, password: str, *, updated_by: str = "panel") -> None:
    set_secret(conn, "UI_ADMIN_PASSWORD", hash_password(password), updated_by=updated_by, already_hashed=True)


# --------------------------------------------------------------------------- secret storage


def set_secret(
    conn: psycopg.Connection,
    name: str,
    value: str | None,
    *,
    updated_by: str = "panel",
    already_hashed: bool = False,
) -> None:
    if not value:
        conn.execute("delete from app_secrets where name = %s", (name,))
        conn.commit()
        return
    conn.execute(
        """
        insert into app_secrets (name, ciphertext, fingerprint, updated_at, updated_by)
        values (%s, %s, %s, now(), %s)
        on conflict (name) do update
            set ciphertext = excluded.ciphertext,
                fingerprint = excluded.fingerprint,
                updated_at = now(),
                updated_by = excluded.updated_by
        """,
        (name, encrypt(value), "hashed-scrypt" if already_hashed else fingerprint(value), updated_by),
    )
    conn.commit()


def get_secret(conn: psycopg.Connection, name: str, *, default: str | None = None) -> str | None:
    row = conn.execute("select ciphertext from app_secrets where name = %s", (name,)).fetchone()
    if row and row["ciphertext"]:
        try:
            return decrypt(row["ciphertext"])
        except Exception as exc:  # noqa: BLE001 - surface clearly instead of silently using env
            log.error("cannot decrypt stored secret %s: %s", name, exc)
            raise
    env = os.environ.get(name)
    return env if env not in (None, "") else default


def list_secrets(conn: psycopg.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute("select name, fingerprint, updated_at, updated_by from app_secrets").fetchall()
    return {
        r["name"]: {
            "fingerprint": r["fingerprint"],
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            "updated_by": r["updated_by"],
        }
        for r in rows
    }


# --------------------------------------------------------------------------- sessions


@dataclass
class Session:
    token: str
    expires_at: datetime


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(conn: psycopg.Connection, *, minutes: int = 720, label: str = "") -> Session:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(tz=timezone.utc) + timedelta(minutes=minutes)
    conn.execute(
        "insert into ui_sessions (token_hash, expires_at, label) values (%s, %s, %s)",
        (_hash_token(token), expires, label[:120]),
    )
    conn.commit()
    return Session(token=token, expires_at=expires)


def session_valid(conn: psycopg.Connection, token: str | None) -> bool:
    if not token:
        return False
    row = conn.execute("select expires_at from ui_sessions where token_hash = %s", (_hash_token(token),)).fetchone()
    if row is None:
        return False
    return row["expires_at"] > datetime.now(tz=timezone.utc)


def destroy_session(conn: psycopg.Connection, token: str | None) -> None:
    if not token:
        return
    conn.execute("delete from ui_sessions where token_hash = %s", (_hash_token(token),))
    conn.commit()


def purge_expired_sessions(conn: psycopg.Connection) -> int:
    row = conn.execute("delete from ui_sessions where expires_at < now() returning token_hash").fetchall()
    conn.commit()
    return len(row)


# --------------------------------------------------------------------------- login limits


def record_attempt(conn: psycopg.Connection, *, ok: bool, label: str = "", note: str = "") -> None:
    if ok:
        # A correct password proves the operator's identity; keeping the typos they made before it
        # would lock them out of their own panel for the rest of the window.
        conn.execute("delete from login_attempts where ok = false")
    conn.execute(
        "insert into login_attempts (ok, label, note) values (%s, %s, %s)",
        (ok, label[:120], note[:300]),
    )
    conn.commit()


def login_allowed(conn: psycopg.Connection) -> tuple[bool, int]:
    """(allowed, failures_in_window). Slows brute force without locking the operator out forever."""
    row = conn.execute(
        """
        select count(*) as failures from login_attempts
        where ok = false and ts > now() - interval '%s minutes'
        """ % LOGIN_WINDOW_MINUTES
    ).fetchone()
    failures = int(row["failures"] or 0)
    return failures < MAX_FAILED_LOGINS, failures


__all__ = [
    "MASTER_KEY_ENV",
    "SESSION_COOKIE",
    "Session",
    "create_session",
    "decrypt",
    "destroy_session",
    "encrypt",
    "fingerprint",
    "get_secret",
    "hash_password",
    "list_secrets",
    "login_allowed",
    "master_key",
    "panel_password_hash",
    "purge_expired_sessions",
    "record_attempt",
    "session_valid",
    "set_password",
    "set_secret",
    "verify_password",
]

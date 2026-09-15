"""Single-admin login for the break-glass web UI.

No pre-shared token to configure: the first visit to /login generates a random
password (and a stable session-signing secret), stores only their hashes/values in
sqlite, and shows the password once. From then on it's a normal username+password
login backed by a signed, HttpOnly session cookie.

Deliberately minimal — one admin account, no password reset flow, no rate limiting.
This is a single-operator tool, not a multi-tenant service.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

_PBKDF2_ITERATIONS = 260_000
DEFAULT_USERNAME = "admin"
SESSION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60  # 30 days


@dataclass(frozen=True)
class AdminAccount:
    username: str
    password_hash: str
    session_secret: str


def _hash_password(
    password: str, *, salt: bytes | None = None, iterations: int = _PBKDF2_ITERATIONS
) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored_hash: str) -> bool:
    try:
        algo, iterations_str, salt_hex, _digest_hex = stored_hash.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = bytes.fromhex(salt_hex)
        iterations = int(iterations_str)
    except ValueError:
        return False
    # Recompute with the iteration count *stored in the hash*, not the current
    # constant — otherwise raising _PBKDF2_ITERATIONS later would silently break
    # verification for every password hashed before the change.
    candidate = _hash_password(password, salt=salt, iterations=iterations)
    return hmac.compare_digest(candidate, stored_hash)


class AdminUserStore:
    """Sqlite-backed, single-row admin account. Same short-lived-connection pattern
    as BreakGlassStore — safe to share the process with it (and, if pointed at the
    same file, they coexist as separate tables without conflict)."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_account (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    username TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    session_secret TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def get(self) -> AdminAccount | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT username, password_hash, session_secret FROM admin_account WHERE id = 1"
            ).fetchone()
        return AdminAccount(**dict(row)) if row else None

    def get_or_create(self) -> tuple[AdminAccount, str | None]:
        """Returns (account, generated_password). generated_password is only set the
        one time this call is the one that creates the account — the caller uses that
        to decide whether to show the reveal banner."""
        existing = self.get()
        if existing is not None:
            return existing, None

        password = secrets.token_urlsafe(18)
        account = AdminAccount(
            username=DEFAULT_USERNAME,
            password_hash=_hash_password(password),
            session_secret=secrets.token_hex(32),
        )
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO admin_account VALUES (1, :username, :password_hash, :session_secret)",
                account.__dict__,
            )
        return account, password

    def verify(self, username: str, password: str) -> bool:
        account = self.get()
        if account is None or not hmac.compare_digest(username, account.username):
            return False
        return _verify_password(password, account.password_hash)


def sign_session(session_secret: str, username: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"u": username, "exp": int(time.time()) + SESSION_MAX_AGE_SECONDS}).encode()
    ).decode()
    signature = hmac.new(session_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def verify_session(session_secret: str, cookie_value: str) -> str | None:
    """Returns the session's username if the cookie is validly signed and unexpired."""
    try:
        payload, signature = cookie_value.rsplit(".", 1)
    except ValueError:
        return None
    expected = hmac.new(session_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
    except (ValueError, UnicodeDecodeError):
        return None
    if data.get("exp", 0) < time.time():
        return None
    username = data.get("u")
    return username if isinstance(username, str) else None

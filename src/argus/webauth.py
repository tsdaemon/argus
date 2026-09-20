"""Single-admin login for the whole app: the UI, the chat API, and the break-glass pages.

The first visit to /login creates the account and a stable session-signing secret, storing
only the password's hash and the secret. The password is `ARGUS_ADMIN_PASSWORD` when that is
set (a deployment: nothing is shown, and a visitor cannot choose it), otherwise a generated
one shown once (local development). From then on it's a normal username+password
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
import time

from argus.db.admin import AdminAccount, AdminRepository

_PBKDF2_ITERATIONS = 260_000
DEFAULT_USERNAME = "admin"
SESSION_COOKIE = "argus_session"
SESSION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60  # 30 days


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


class AdminAuth:
    """The single admin account."""

    def __init__(self, repository: AdminRepository) -> None:
        self._repository = repository

    async def get(self) -> AdminAccount | None:
        return await self._repository.get_admin()

    async def get_or_create(
        self, initial_password: str | None = None
    ) -> tuple[AdminAccount, str | None]:
        """Returns (account, generated_password). generated_password is only set when this
        call created the account with a password it made up — the caller uses that to decide
        whether to show the reveal banner. With `initial_password` the account gets that one
        and nothing is revealed."""
        existing = await self.get()
        if existing is not None:
            return existing, None

        generated = None if initial_password else secrets.token_urlsafe(18)
        password = initial_password or generated
        account = AdminAccount(
            username=DEFAULT_USERNAME,
            password_hash=_hash_password(password),
            session_secret=secrets.token_hex(32),
        )
        if await self._repository.create_admin_if_absent(account):
            return account, generated
        # Another first visit stored its account between the read and the insert.
        stored = await self.get()
        assert stored is not None
        return stored, None

    async def verify(self, username: str, password: str) -> bool:
        account = await self.get()
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

from __future__ import annotations

import time

from argus.webauth import (
    DEFAULT_USERNAME,
    AdminAuth,
    _hash_password,
    _verify_password,
    sign_session,
    verify_session,
)
from tests.fakes import InMemoryAdmin


def test_hash_and_verify_password_roundtrip():
    hashed = _hash_password("correct horse battery staple")
    assert _verify_password("correct horse battery staple", hashed)
    assert not _verify_password("wrong", hashed)


def test_hash_is_salted_differently_each_time():
    assert _hash_password("same password") != _hash_password("same password")


def test_verify_password_rejects_garbage_hash():
    assert not _verify_password("anything", "not-a-real-hash")
    assert not _verify_password("anything", "bcrypt$junk")


def test_verify_uses_iteration_count_stored_in_the_hash_not_the_current_constant():
    # Simulates _PBKDF2_ITERATIONS having been raised after this hash was created —
    # verification must still work against the older, lower count embedded in it.
    old_hash = _hash_password("a password", iterations=1_000)
    assert _verify_password("a password", old_hash)


async def test_get_or_create_generates_account_only_once():
    admin = AdminAuth(InMemoryAdmin())

    account1, password1 = await admin.get_or_create()
    account2, password2 = await admin.get_or_create()

    assert password1 is not None
    assert password2 is None
    assert account1 == account2
    assert account1.username == DEFAULT_USERNAME


async def test_a_configured_initial_password_is_used_and_never_revealed():
    admin = AdminAuth(InMemoryAdmin())

    account, revealed = await admin.get_or_create("from-the-environment")

    assert revealed is None
    assert await admin.verify(account.username, "from-the-environment")


async def test_get_or_create_generated_password_actually_verifies():
    admin = AdminAuth(InMemoryAdmin())

    account, password = await admin.get_or_create()

    assert password is not None
    assert await admin.verify(account.username, password)
    assert not await admin.verify(account.username, "wrong-password")


async def test_verify_unknown_username_fails():
    admin = AdminAuth(InMemoryAdmin())
    _account, password = await admin.get_or_create()

    assert not await admin.verify("not-admin", password)


async def test_account_persists_across_instances_of_the_same_repository():
    repository = InMemoryAdmin()
    account, password = await AdminAuth(repository).get_or_create()

    reopened = AdminAuth(repository)

    assert await reopened.get() == account
    assert await reopened.verify(account.username, password)


def test_session_sign_and_verify_roundtrip():
    secret = "session-secret"
    cookie = sign_session(secret, "admin")

    assert verify_session(secret, cookie) == "admin"


def test_session_verify_rejects_wrong_secret():
    cookie = sign_session("secret-a", "admin")
    assert verify_session("secret-b", cookie) is None


def test_session_verify_rejects_tampered_payload():
    cookie = sign_session("secret", "admin")
    payload, signature = cookie.rsplit(".", 1)
    tampered = f"{payload}x.{signature}"
    assert verify_session("secret", tampered) is None


def test_session_verify_rejects_expired_session(monkeypatch):
    secret = "secret"
    cookie = sign_session(secret, "admin")

    future = time.time() + 40 * 24 * 60 * 60
    monkeypatch.setattr(time, "time", lambda: future)

    assert verify_session(secret, cookie) is None


def test_session_verify_rejects_malformed_cookie():
    assert verify_session("secret", "not-even-close-to-valid") is None
    assert verify_session("secret", "") is None

from __future__ import annotations

import time
from pathlib import Path

from argus.webauth import (
    DEFAULT_USERNAME,
    AdminUserStore,
    _hash_password,
    _verify_password,
    sign_session,
    verify_session,
)


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


def test_get_or_create_generates_account_only_once(tmp_path: Path):
    store = AdminUserStore(tmp_path / "auth.sqlite")

    account1, password1 = store.get_or_create()
    account2, password2 = store.get_or_create()

    assert password1 is not None
    assert password2 is None
    assert account1 == account2
    assert account1.username == DEFAULT_USERNAME


def test_get_or_create_generated_password_actually_verifies(tmp_path: Path):
    store = AdminUserStore(tmp_path / "auth.sqlite")

    account, password = store.get_or_create()

    assert password is not None
    assert store.verify(account.username, password)
    assert not store.verify(account.username, "wrong-password")


def test_verify_unknown_username_fails(tmp_path: Path):
    store = AdminUserStore(tmp_path / "auth.sqlite")
    _account, password = store.get_or_create()

    assert not store.verify("not-admin", password)


def test_persists_across_instances(tmp_path: Path):
    path = tmp_path / "auth.sqlite"
    account, password = AdminUserStore(path).get_or_create()

    reopened = AdminUserStore(path)

    assert reopened.get() == account
    assert reopened.verify(account.username, password)


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

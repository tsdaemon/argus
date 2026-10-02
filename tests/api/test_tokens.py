from __future__ import annotations

import re
from datetime import timedelta

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from argus.api.auth import add_auth
from argus.api.tokens import add_token_routes
from argus.db.tokens import TokenAuth, now
from argus.webauth import AdminAuth
from tests.fakes import InMemoryAdmin, InMemoryTokens


def token_client():
    tokens = InMemoryTokens()
    auth = TokenAuth(tokens)
    admin = AdminAuth(InMemoryAdmin())
    app = FastAPI()
    add_auth(app, admin, initial_password="password", tokens=auth)
    add_token_routes(app, admin, auth)
    return TestClient(app, follow_redirects=False), tokens, auth


def login(client):
    client.get("/login")
    client.post("/login", data={"username": "admin", "password": "password"})


def csrf(client):
    return re.search(r'name="csrf" value="([^"]+)"', client.get("/settings/tokens").text)[1]


def test_issue_reveal_once_revoke_and_csrf():
    client, tokens, _auth = token_client()
    assert client.get("/settings/tokens").status_code == 401
    assert client.post("/settings/tokens", data={"name": "hermes"}).status_code == 401
    login(client)
    assert client.post("/settings/tokens", data={"name": "hermes"}).status_code == 403
    response = client.post("/settings/tokens", data={"name": "hermes", "csrf": csrf(client)})
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    secret = re.search(r"argus_[a-f0-9]{32}_[A-Za-z0-9_-]+", response.text)[0]
    token = next(iter(tokens.tokens.values()))
    assert secret not in repr(token)
    assert token.secret_hash not in client.get("/settings/tokens").text
    assert secret not in client.get("/settings/tokens").text
    assert client.post(f"/settings/tokens/{token.id}/revoke", data={}).status_code == 403
    assert (
        client.post(f"/settings/tokens/{token.id}/revoke", data={"csrf": csrf(client)}).status_code
        == 303
    )
    assert "Revoked" in client.get("/settings/tokens").text
    client.cookies.set("argus_session", "invalid")
    assert (
        client.get("/settings/tokens", headers={"Authorization": f"Bearer {secret}"}).status_code
        == 401
    )


@pytest.mark.parametrize("value", ["", "bogus", "argus_bogus_x", "Bearer hi", "argus_" + "x" * 500])
async def test_invalid_credentials_fail_closed(value):
    assert await TokenAuth(InMemoryTokens()).verify(value) is None


async def test_tokens_expire_and_record_use():
    store = InMemoryTokens()
    auth = TokenAuth(store)
    token, secret = await auth.issue("hermes", now() + timedelta(days=1))
    assert await auth.verify(secret)
    assert (await store.get(token.id)).last_used_at is not None
    assert await auth.verify(secret + "wrong") is None
    from dataclasses import replace

    store.tokens[token.id] = replace(token, expires_at=now() - timedelta(seconds=1))
    assert await auth.verify(secret) is None

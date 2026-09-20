"""The app-wide login: what it gates, what it leaves open, and how sessions work."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from starlette.testclient import TestClient

from argus.api.auth import add_auth, safe_next
from argus.webauth import SESSION_COOKIE, AdminAuth, sign_session
from tests.fakes import InMemoryAdmin


def make_app(initial_password: str | None = None) -> tuple[FastAPI, AdminAuth]:
    admin = AdminAuth(InMemoryAdmin())
    app = FastAPI()
    add_auth(app, admin, initial_password=initial_password)

    @app.get("/")
    async def home():
        return PlainTextResponse("the ui")

    @app.post("/agent")
    async def agent():
        return {"ok": True}

    @app.get("/agent/health")
    async def health():
        return {"status": "ok"}

    @app.get("/mcp")
    async def mcp():
        return PlainTextResponse("mcp reached")

    return app, admin


def logged_in(client: TestClient, admin: AdminAuth) -> None:
    account, _ = asyncio.run(admin.get_or_create())
    client.cookies.set(SESSION_COOKIE, sign_session(account.session_secret, account.username))


def test_a_browser_page_request_is_sent_to_the_login_form_and_back():
    app, _ = make_app()
    client = TestClient(app, follow_redirects=False)

    resp = client.get("/?tab=chat", headers={"accept": "text/html"})

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=/%3Ftab%3Dchat"


def test_api_calls_without_a_session_get_401_not_a_redirect():
    app, _ = make_app()
    client = TestClient(app, follow_redirects=False)

    assert client.post("/agent", json={}).status_code == 401
    assert client.get("/", headers={"accept": "application/json"}).status_code == 401


def test_login_health_and_mcp_stay_open():
    app, _ = make_app()
    client = TestClient(app, follow_redirects=False)

    assert client.get("/login").status_code == 200
    assert client.get("/agent/health").status_code == 200
    assert client.get("/mcp").text == "mcp reached"  # its own bearer token guards it


def test_a_valid_session_reaches_the_app():
    app, admin = make_app()
    client = TestClient(app, follow_redirects=False)
    logged_in(client, admin)

    assert client.get("/").text == "the ui"
    assert client.post("/agent", json={}).status_code == 200


def test_a_forged_or_foreign_session_does_not_count():
    app, _ = make_app()
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(SESSION_COOKIE, sign_session("some-other-secret", "admin"))

    assert client.post("/agent", json={}).status_code == 401


def test_first_visit_creates_the_account_and_reveals_a_generated_password_once():
    app, admin = make_app()
    client = TestClient(app)

    first = client.get("/login")
    second = client.get("/login")

    assert "Admin account created" in first.text
    assert "Admin account created" not in second.text
    assert asyncio.run(admin.get()) is not None


def test_a_configured_password_logs_in_and_is_never_shown():
    app, _ = make_app(initial_password="from-the-environment")
    client = TestClient(app, follow_redirects=False)

    page = client.get("/login")
    bad = client.post("/login", data={"username": "admin", "password": "guess"})
    good = client.post(
        "/login", data={"username": "admin", "password": "from-the-environment", "next": "/x"}
    )

    assert "Admin account created" not in page.text
    assert bad.headers["location"].startswith("/login?error=1")
    assert SESSION_COOKIE not in bad.cookies
    assert good.status_code == 303 and good.headers["location"] == "/x"
    assert SESSION_COOKIE in good.cookies


def test_logout_ends_the_session():
    app, _ = make_app(initial_password="pw")
    client = TestClient(app, follow_redirects=False)
    client.get("/login")
    client.post("/login", data={"username": "admin", "password": "pw"})
    assert client.get("/").status_code == 200

    client.post("/logout")

    assert client.get("/", headers={"accept": "text/html"}).status_code == 303


@pytest.mark.parametrize(
    "target,expected",
    [("/agent", "/agent"), ("//evil.example", "/"), ("https://evil.example", "/"), (None, "/"), ("", "/")],
)
def test_next_only_ever_points_at_this_site(target, expected):
    assert safe_next(target) == expected

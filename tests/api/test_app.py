from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from argus.api.app import build_app
from argus.config import AgentConfig, ArgusConfig, ProviderEntry
from argus.webauth import SESSION_COOKIE, AdminAuth, sign_session
from tests.fakes import InMemoryAdmin, InMemoryBreakGlass


def make_config(tmp_path: Path) -> ArgusConfig:
    return ArgusConfig(
        providers={},
        agent=AgentConfig(
            workspace_root=str(tmp_path / "workspace"), api_key="sk-or-fake-not-real"
        ),
    )


def test_health_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ARGUS_MCP_TOKEN", raising=False)
    app = build_app(make_config(tmp_path), InMemorySaver())
    client = TestClient(app)

    resp = client.get("/agent/health")

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_mcp_not_mounted_without_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ARGUS_MCP_TOKEN", raising=False)
    app = build_app(make_config(tmp_path), InMemorySaver())
    client = TestClient(app)

    resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})

    assert resp.status_code == 404


def test_mcp_mounted_and_reachable_with_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Regression test: FastMCP's mounted sub-app needs its own `lifespan` forwarded
    into the parent FastAPI app's constructor, or every /mcp request 500s with
    'task group was not initialized' — this only surfaces on a real request, not at
    app-build time, which is why it needs an actual TestClient round-trip to catch."""
    monkeypatch.setenv("ARGUS_MCP_TOKEN", "test-token")
    config = make_config(tmp_path)
    config.providers["docker"] = ProviderEntry(enabled=True, socket="unix:///nonexistent")
    app = build_app(config, InMemorySaver())

    with TestClient(app) as client:
        resp = client.post(
            "/mcp",
            headers={
                "Authorization": "Bearer test-token",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
        )

    assert resp.status_code == 200
    assert "argus" in resp.text


def test_mcp_rejects_wrong_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARGUS_MCP_TOKEN", "test-token")
    app = build_app(make_config(tmp_path), InMemorySaver())

    with TestClient(app) as client:
        resp = client.post(
            "/mcp",
            headers={"Authorization": "Bearer wrong-token"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )

    assert resp.status_code == 401


def test_agent_run_endpoint_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """POST /agent should be routed to the AG-UI endpoint (not swallowed by a mounted
    MCP app registered after it) even when /mcp is also mounted in the same process."""
    monkeypatch.setenv("ARGUS_MCP_TOKEN", "test-token")
    app = build_app(make_config(tmp_path), InMemorySaver())

    with TestClient(app) as client:
        resp = client.post("/agent", json={})

    # A 422 naming AG-UI's RunAgentInput fields proves this reached our endpoint, not
    # the MCP mount's generic 404 for an unrecognized path.
    assert resp.status_code == 422
    assert "threadId" in resp.text


@pytest.mark.parametrize("mcp_enabled", [False, True])
@pytest.mark.parametrize("launcher_enabled", [False, True])
def test_frontend_assets_and_launcher_availability(
    tmp_path, monkeypatch, mcp_enabled, launcher_enabled
):
    if mcp_enabled:
        monkeypatch.setenv("ARGUS_MCP_TOKEN", "test-token")
    else:
        monkeypatch.delenv("ARGUS_MCP_TOKEN", raising=False)
    config = make_config(tmp_path)
    config.providers["breakglass"] = ProviderEntry(
        enabled=True,
        launcher={
            "type": "ssh",
            "hosts": {"test-host": {"host": "test-host", "user": "operator"}},
        }
        if launcher_enabled
        else None,
    )
    frontend = tmp_path / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text("<title>Argus chat</title>")
    (frontend / "assets" / "app.js").write_text("/* UI bundle */")
    admin_repository = InMemoryAdmin()
    app = build_app(
        config,
        InMemorySaver(),
        breakglass=InMemoryBreakGlass(),
        admin=admin_repository,
        frontend_dir=frontend,
    )
    with TestClient(app) as client:
        # The whole app sits behind the admin login; only /mcp, /login and health are open.
        gate = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
        assert gate.status_code == 303 and gate.headers["location"].startswith("/login")
        assert client.get("/api/ui-config").status_code == 401
        assert client.get("/agent/health").status_code == 200

        account, _ = asyncio.run(AdminAuth(admin_repository).get_or_create())
        client.cookies.set(SESSION_COOKIE, sign_session(account.session_secret, account.username))

        assert "Argus chat" in client.get("/").text
        assert client.get("/assets/app.js").text == "/* UI bundle */"
        assert client.get("/api/ui-config").json() == {
            "launch_enabled": mcp_enabled and launcher_enabled,
            "auth_enabled": True,
        }
        if mcp_enabled and launcher_enabled:
            assert client.get("/launch", follow_redirects=False).status_code == 200
        assert client.post("/agent", json={}).status_code == 422


@pytest.mark.parametrize("with_store", [True, False])
def test_the_agent_gets_the_break_glass_provider_only_when_a_store_is_supplied(
    tmp_path, monkeypatch, with_store
):
    monkeypatch.delenv("ARGUS_MCP_TOKEN", raising=False)
    config = make_config(tmp_path)
    config.providers["breakglass"] = ProviderEntry(enabled=True)
    handed_to_agent: dict = {}

    def capture(**kwargs):
        handed_to_agent.update(kwargs["providers"])
        return object()

    monkeypatch.setattr("argus.api.app.build_agent", capture)
    monkeypatch.setattr("argus.api.app.add_chat_routes", lambda *args: None)

    stores = {"breakglass": InMemoryBreakGlass(), "admin": InMemoryAdmin()} if with_store else {}
    build_app(config, InMemorySaver(), **stores)

    assert ("breakglass" in handed_to_agent) is with_store

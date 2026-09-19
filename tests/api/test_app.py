from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from argus.api.app import build_app
from argus.config import AgentConfig, ArgusConfig, ProviderEntry


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
        store_path=str(tmp_path / "breakglass.sqlite"),
        launcher={"type": "ssh", "host": "test-host", "user": "operator"}
        if launcher_enabled
        else None,
    )
    frontend = tmp_path / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text("<title>Argus chat</title>")
    (frontend / "assets" / "app.js").write_text("/* UI bundle */")
    app = build_app(config, InMemorySaver(), frontend_dir=frontend)
    with TestClient(app) as client:
        assert "Argus chat" in client.get("/").text
        assert client.get("/assets/app.js").text == "/* UI bundle */"
        assert client.get("/api/ui-config").json() == {
            "launch_enabled": mcp_enabled and launcher_enabled,
        }
        if mcp_enabled and launcher_enabled:
            response = client.get("/launch", follow_redirects=False)
            assert response.status_code == 303
            assert response.headers["location"].startswith("/login")
        assert client.post("/agent", json={}).status_code == 422

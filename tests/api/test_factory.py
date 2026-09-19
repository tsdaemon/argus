"""The `uvicorn --factory` entry point and the lifespan that owns the app's resources."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from argus.api.app import build_app, create_app
from argus.config import ArgusConfig
from tests.fakes import InMemoryHistory


def call_like_uvicorn(factory):
    """uvicorn calls an app factory from inside its running event loop; so do we."""

    async def call():
        return factory()

    return asyncio.run(call())


def test_the_factory_needs_a_config_path(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ARGUS_CONFIG", raising=False)

    with pytest.raises(RuntimeError, match="ARGUS_CONFIG"):
        call_like_uvicorn(create_app)


def test_the_factory_builds_the_app_without_touching_the_database(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Nothing connects until the lifespan runs, so building (and importing) is cheap."""
    monkeypatch.delenv("ARGUS_MCP_TOKEN", raising=False)
    config = tmp_path / "argus.yaml"
    config.write_text(
        "providers: {}\n"
        "agent:\n"
        "  database_url: postgresql://argus:x@127.0.0.1:1/argus\n"
        "  api_key: test\n"
        f"  workspace_root: {tmp_path / 'workspace'}\n"
    )
    monkeypatch.setenv("ARGUS_CONFIG", str(config))

    app = call_like_uvicorn(create_app)

    paths = {route.path for route in app.routes}
    assert {"/agent", "/api/threads", "/agent/health"} <= paths


def test_resources_open_on_startup_and_close_on_shutdown(tmp_path):
    events: list[str] = []

    @asynccontextmanager
    async def resources():
        events.append("open")
        try:
            yield
        finally:
            events.append("close")

    app = build_app(
        ArgusConfig(agent={"workspace_root": str(tmp_path / "workspace"), "api_key": "test"}),
        InMemorySaver(),
        InMemoryHistory(),
        resources=resources,
    )
    assert events == []

    with TestClient(app) as client:
        assert events == ["open"]
        assert client.get("/agent/health").json() == {"status": "ok"}

    assert events == ["open", "close"]

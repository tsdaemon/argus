"""Assembles the FastAPI app. `uvicorn argus.api.app:create_app --factory` runs it.

Route order matters: a `Mount("/", ...)` can swallow a request that only partially
matches an earlier route (right path, wrong method), so the AG-UI routes are added
before the MCP surface is mounted at root — never the reverse.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.base import BaseCheckpointSaver

from argus.agent.api import build_agent
from argus.agent.tracing import setup_tracing
from argus.api.chat import add_chat_routes
from argus.config import ArgusConfig, load_config
from argus.db.checkpointer import make_checkpointer
from argus.db.history import HistoryRepository, SqlHistory, make_engine
from argus.mcp.auth import StaticTokenVerifier
from argus.mcp.server import build_http_app, build_server
from argus.policy import PolicyEngine
from argus.providers.registry import instantiate_providers


def build_app(
    config: ArgusConfig,
    checkpointer: BaseCheckpointSaver,
    history: HistoryRepository | None = None,
    *,
    frontend_dir: Path | None = None,
    resources: Callable[[], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    # FastMCP's app needs its own lifespan forwarded into the parent FastAPI
    # constructor (an internal task group otherwise never starts), so it must exist
    # before `FastAPI(...)` is built, even though it's mounted at the end (see above).
    mcp_app = None
    launch_enabled = False
    mcp_token = os.environ.get("ARGUS_MCP_TOKEN")
    if mcp_token:
        mcp, mcp_providers = build_server(config, auth=StaticTokenVerifier(mcp_token))
        mcp_app = build_http_app(mcp, mcp_providers)
        launch_enabled = getattr(mcp_providers.get("breakglass"), "launcher", None) is not None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            if resources is not None:
                await stack.enter_async_context(resources())
            if mcp_app is not None:
                await stack.enter_async_context(mcp_app.lifespan(app))
            yield

    app = FastAPI(lifespan=lifespan)

    policy = PolicyEngine(
        None,
        default_mutate=config.policy.default_mutate,
        default_destructive=config.policy.default_destructive,
        overrides=config.policy.overrides,
    )
    providers = instantiate_providers(config, exclude={"breakglass"})
    provider_settings = {name: config.providers[name].settings() for name in providers}

    agui_agent = build_agent(
        config=config.agent,
        providers=providers,
        provider_settings=provider_settings,
        policy=policy,
        checkpointer=checkpointer,
    )
    add_chat_routes(app, agui_agent, history)

    @app.get("/agent/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/ui-config")
    async def ui_config():
        return {"launch_enabled": launch_enabled}

    # Production assets are copied into the package by Docker; local builds live
    # in frontend/dist. Register exact routes before the optional root MCP mount.
    if frontend_dir is None:
        frontend_dir = Path(__file__).with_name("static")
        if not frontend_dir.is_dir():
            frontend_dir = Path(__file__).resolve().parents[3] / "frontend" / "dist"
    if (frontend_dir / "index.html").is_file():

        @app.get("/", include_in_schema=False)
        async def frontend():
            return FileResponse(frontend_dir / "index.html", headers={"Cache-Control": "no-cache"})

        app.mount("/assets", StaticFiles(directory=frontend_dir / "assets"), name="assets")

    # One process, both interfaces — /mcp and /breakglass/launch (the agent UI's
    # break-glass action reuses this page rather than a second implementation).
    if mcp_app is not None:
        app.mount("/", mcp_app)

    return app


def create_app() -> FastAPI:
    """The `uvicorn --factory` entry point: config from `ARGUS_CONFIG`, resources per lifespan.

    Everything that needs the event loop (the Postgres pool, the checkpointer's tables) opens
    on startup and closes on shutdown, so uvicorn can run and reload this like any app.
    """
    path = os.environ.get("ARGUS_CONFIG")
    if not path:
        raise RuntimeError("Set ARGUS_CONFIG to the path of the argus config file.")
    config = load_config(path)
    setup_tracing(config.agent.otel)
    checkpointer, pool = make_checkpointer(config.agent.database_url)
    engine = make_engine(config.agent.database_url)

    @asynccontextmanager
    async def resources() -> AsyncIterator[None]:
        try:
            async with pool:
                await checkpointer.setup()  # idempotent, safe on every boot
                yield
        finally:
            await engine.dispose()

    return build_app(config, checkpointer, SqlHistory(engine), resources=resources)

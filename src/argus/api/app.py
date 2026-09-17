"""Assembles the FastAPI app for `argus agent serve`.

Route order matters: a `Mount("/", ...)` can swallow a request that only partially
matches an earlier route (right path, wrong method), so the AG-UI routes are added
before the MCP surface is mounted at root — never the reverse.
"""

from __future__ import annotations

import os

from ag_ui_langgraph import add_langgraph_fastapi_endpoint
from fastapi import FastAPI
from langgraph.checkpoint.base import BaseCheckpointSaver

from argus.agent.api import build_agent
from argus.config import ArgusConfig
from argus.mcp.auth import StaticTokenVerifier
from argus.mcp.server import build_http_app, build_server
from argus.policy import PolicyEngine
from argus.providers.registry import instantiate_providers


def build_app(config: ArgusConfig, checkpointer: BaseCheckpointSaver) -> FastAPI:
    # FastMCP's app needs its own lifespan forwarded into the parent FastAPI
    # constructor (an internal task group otherwise never starts), so it must exist
    # before `FastAPI(...)` is built, even though it's mounted at the end (see above).
    mcp_app = None
    mcp_token = os.environ.get("ARGUS_MCP_TOKEN")
    if mcp_token:
        mcp, mcp_providers = build_server(config, auth=StaticTokenVerifier(mcp_token))
        mcp_app = build_http_app(mcp, mcp_providers)

    app = FastAPI(lifespan=mcp_app.lifespan if mcp_app is not None else None)

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
    add_langgraph_fastapi_endpoint(app, agui_agent, "/agent")

    @app.get("/agent/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # One process, both interfaces — /mcp and /breakglass/launch (the agent UI's
    # break-glass action reuses this page rather than a second implementation).
    if mcp_app is not None:
        app.mount("/", mcp_app)

    return app

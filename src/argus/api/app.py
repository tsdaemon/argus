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

from argus.a2a.server import add_a2a_routes
from argus.agent.api import build_agent
from argus.agent.cost import CostRecorder
from argus.agent.graph import _build_model, build_graph
from argus.agent.titles import TitleWriter
from argus.agent.tracing import setup_tracing
from argus.api.auth import add_auth
from argus.api.chat import add_chat_routes
from argus.api.tokens import add_token_routes
from argus.config import ArgusConfig, load_config
from argus.db.a2a import A2ARepository, SqlA2A
from argus.db.admin import AdminRepository, SqlAdmin
from argus.db.breakglass import BreakGlassRepository, SqlBreakGlass
from argus.db.checkpointer import make_checkpointer
from argus.db.history import HistoryRepository, SqlHistory, make_engine
from argus.db.tokens import SqlTokens, TokenAuth, TokenRepository
from argus.mcp.auth import StaticTokenVerifier
from argus.mcp.server import build_http_app, build_server
from argus.mcp.webapp import add_breakglass_routes
from argus.policy import PolicyEngine
from argus.providers.breakglass_provider import BreakglassProvider
from argus.providers.registry import instantiate_providers
from argus.webauth import AdminAuth


def build_app(
    config: ArgusConfig,
    checkpointer: BaseCheckpointSaver,
    history: HistoryRepository | None = None,
    breakglass: BreakGlassRepository | None = None,
    admin: AdminRepository | None = None,
    *,
    frontend_dir: Path | None = None,
    tokens: TokenRepository | None = None,
    a2a: A2ARepository | None = None,
    resources: Callable[[], AbstractAsyncContextManager[None]] | None = None,
    generate_titles: bool = False,
) -> FastAPI:
    # FastMCP's app needs its own lifespan forwarded into the parent FastAPI
    # constructor (an internal task group otherwise never starts), so it must exist
    # before `FastAPI(...)` is built, even though it's mounted at the end (see above).
    admin_auth = AdminAuth(admin) if admin is not None else None
    token_auth = TokenAuth(tokens) if tokens is not None and admin_auth is not None else None
    if config.a2a.enabled and (token_auth is None or a2a is None or history is None):
        raise ValueError("A2A requires admin, token, task, and history repositories.")
    a2a_executor = None
    mcp_app = None
    mcp_token = os.environ.get("ARGUS_MCP_TOKEN")
    if mcp_token:
        mcp, _ = build_server(
            config,
            auth=StaticTokenVerifier(mcp_token),
            dependencies={"breakglass": {"repository": breakglass, "admin": admin_auth}},
        )
        mcp_app = build_http_app(mcp)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            if resources is not None:
                await stack.enter_async_context(resources())
            if mcp_app is not None:
                await stack.enter_async_context(mcp_app.lifespan(app))
            if config.a2a.enabled:
                await a2a.recover()
            try:
                yield
            finally:
                if a2a_executor is not None:
                    await a2a_executor.close()

    app = FastAPI(lifespan=lifespan)
    if admin_auth is not None:  # without an admin store (tests) the app stays open
        add_auth(app, admin_auth, initial_password=os.environ.get("ARGUS_ADMIN_PASSWORD"),
                 tokens=token_auth)
        if token_auth is not None:
            add_token_routes(app, admin_auth, token_auth)

    policy = PolicyEngine(
        None,
        default_mutate=config.policy.default_mutate,
        default_destructive=config.policy.default_destructive,
        overrides=config.policy.overrides,
    )
    # The agent may file a break-glass request (a READ tool that only records one); it never
    # approves or launches, which are human routes behind the login.
    breakglass_ready = breakglass is not None and admin_auth is not None
    providers = instantiate_providers(
        config,
        exclude=() if breakglass_ready else {"breakglass"},
        dependencies={"breakglass": {"repository": breakglass, "admin": admin_auth}},
    )
    provider_settings = {name: config.providers[name].settings() for name in providers}

    cost_recorder = CostRecorder(history.add_cost) if history is not None else None
    agui_agent = build_agent(
        config=config.agent,
        providers=providers,
        provider_settings=provider_settings,
        policy=policy,
        checkpointer=checkpointer,
        cost_recorder=cost_recorder,
    )
    # Off by default so tests never call a model for titles; `create_app` turns it on.
    titles = None
    if generate_titles and history is not None:
        # The planner model with as little reasoning as it allows: a label needs none, and
        # left to themselves models spent thousands of reasoning tokens (10-30 s) on one.
        # Some refuse to turn reasoning off (Gemini) and some ignore "minimal" (Qwen).
        title_model = _build_model(config.agent, config.agent.model, cost_recorder)
        titles = TitleWriter(
            title_model.bind(extra_body={"reasoning": {"enabled": False}}).with_fallbacks(
                [title_model.bind(extra_body={"reasoning": {"effort": "minimal"}})]
            ),
            history,
        )
    add_chat_routes(app, agui_agent, history, titles)
    if config.a2a.enabled:
        unattended_graph = build_graph(
            config=config.agent, providers=providers, provider_settings=provider_settings,
            policy=policy, checkpointer=checkpointer, unattended=True,
            cost_recorder=cost_recorder,
        )
        a2a_executor = add_a2a_routes(app, unattended_graph, a2a, history, config.a2a,
                                     config.agent.recursion_limit)

    # The break-glass pages are for the human, so they come with break-glass, not with /mcp.
    breakglass_provider = providers.get("breakglass")
    launch_enabled = False
    if isinstance(breakglass_provider, BreakglassProvider):
        add_breakglass_routes(
            app,
            breakglass_provider.repository,
            breakglass_provider.admin,
            breakglass_provider.launcher,
        )
        launch_enabled = breakglass_provider.launcher is not None

    @app.get("/agent/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/ui-config")
    async def ui_config():
        return {"launch_enabled": launch_enabled, "auth_enabled": admin_auth is not None}

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

    # One process, both interfaces: /mcp beside the agent's routes.
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

    return build_app(
        config,
        checkpointer,
        SqlHistory(engine),
        SqlBreakGlass(engine),
        SqlAdmin(engine),
        resources=resources,
        tokens=SqlTokens(engine),
        a2a=SqlA2A(engine),
        generate_titles=True,
    )

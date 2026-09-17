"""Builds the MCP server: loads config, wires the policy engine and approval backend,
and registers whichever providers are enabled (see `argus.providers.registry`).

Also builds the combined HTTP app: the MCP endpoint and the break-glass web view mounted
on one Starlette app, so a real deployment is one process/port, not two (see
`argus.cli` for stdio vs. this).
"""

from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from fastmcp.server.http import StarletteWithLifespan

from argus.config import ArgusConfig
from argus.mcp.elicit import ElicitApproval
from argus.mcp.webapp import add_breakglass_routes
from argus.policy import PolicyEngine
from argus.providers.base import Provider
from argus.providers.breakglass_provider import BreakglassProvider
from argus.providers.registry import instantiate_providers


def build_server(
    config: ArgusConfig, *, auth: AuthProvider | None = None
) -> tuple[FastMCP, dict[str, Provider]]:
    """Build the MCP server and return it along with the instantiated providers (keyed
    by config name), so callers can reach into e.g. the breakglass provider's store
    without re-reading the config or re-instantiating anything."""
    mcp: FastMCP = FastMCP("argus", auth=auth)
    policy = PolicyEngine(
        ElicitApproval(),
        default_mutate=config.policy.default_mutate,
        default_destructive=config.policy.default_destructive,
        overrides=config.policy.overrides,
    )

    providers = instantiate_providers(config)
    for provider_name, provider in providers.items():
        provider.register(mcp, policy, config.providers[provider_name].settings())

    return mcp, providers


def build_http_app(mcp: FastMCP, providers: dict[str, Provider]) -> StarletteWithLifespan:
    """The one-process deployment shape: `/mcp` plus (if the breakglass provider is
    enabled) the phone-facing `/breakglass` view — login-gated, see `argus.webauth` —
    mounted on the same app."""
    app = mcp.http_app(path="/mcp")

    breakglass = providers.get("breakglass")
    if isinstance(breakglass, BreakglassProvider):
        add_breakglass_routes(app, breakglass.store, breakglass.admin_store, breakglass.launcher)

    return app

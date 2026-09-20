"""Build the optional MCP interface mounted inside the Argus agent application.

Wire shared providers to policy and elicitation, and assemble the MCP/break-glass
sub-app. `argus.api.app` owns the application; this module never starts a process.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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
    config: ArgusConfig,
    *,
    auth: AuthProvider | None = None,
    dependencies: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[FastMCP, dict[str, Provider]]:
    """Build the FastMCP interface object and return it along with the instantiated providers (keyed
    by config name), so callers can reach into e.g. the breakglass provider's repository
    without re-reading the config or re-instantiating anything."""
    mcp: FastMCP = FastMCP("argus", auth=auth)
    policy = PolicyEngine(
        ElicitApproval(),
        default_mutate=config.policy.default_mutate,
        default_destructive=config.policy.default_destructive,
        overrides=config.policy.overrides,
    )

    providers = instantiate_providers(config, dependencies=dependencies)
    for provider_name, provider in providers.items():
        provider.register(mcp, policy, config.providers[provider_name].settings())

    return mcp, providers


def build_http_app(mcp: FastMCP, providers: dict[str, Provider]) -> StarletteWithLifespan:
    """Build the sub-app mounted by `argus.api.app`: `/mcp` and, when configured,
    the login-gated break-glass web routes."""
    app = mcp.http_app(path="/mcp")

    breakglass = providers.get("breakglass")
    if isinstance(breakglass, BreakglassProvider):
        add_breakglass_routes(app, breakglass.repository, breakglass.admin, breakglass.launcher)

    return app

"""Builds the MCP server: loads config, wires the policy engine and approval backend,
and registers whichever providers are enabled. This is the one place that knows about
every provider that exists — adding a new one is a single line in `PROVIDER_REGISTRY`.

Also builds the combined HTTP app: the MCP endpoint and the break-glass web view mounted
on one Starlette app, so a real deployment is one process/port, not two (see
`argus.cli` for stdio vs. this).
"""

from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from fastmcp.server.http import StarletteWithLifespan

from argus.approval.elicit import ElicitApproval
from argus.config import ArgusConfig
from argus.policy import PolicyEngine
from argus.providers.base import Provider
from argus.providers.breakglass_provider import BreakglassProvider
from argus.providers.docker_provider import DockerProvider
from argus.webapp import add_breakglass_routes

PROVIDER_REGISTRY: dict[str, type[Provider]] = {
    "docker": DockerProvider,
    "breakglass": BreakglassProvider,
}


def build_server(
    config: ArgusConfig, *, auth: AuthProvider | None = None
) -> tuple[FastMCP, dict[str, Provider]]:
    """Build the MCP server and return it along with the instantiated providers (keyed
    by config name), so callers can reach into e.g. the breakglass provider's store
    without re-reading the config or re-instantiating anything."""
    mcp: FastMCP = FastMCP("argus", auth=auth)
    approval = ElicitApproval()
    policy = PolicyEngine(
        approval,
        default_mutate=config.policy.default_mutate,
        default_destructive=config.policy.default_destructive,
        overrides=config.policy.overrides,
    )

    providers: dict[str, Provider] = {}
    for provider_name, entry in config.providers.items():
        if not entry.enabled:
            continue
        provider_cls = PROVIDER_REGISTRY.get(provider_name)
        if provider_cls is None:
            raise ValueError(
                f"Unknown provider '{provider_name}' in config. "
                f"Known providers: {sorted(PROVIDER_REGISTRY)}"
            )
        settings = entry.settings()
        provider = provider_cls(settings)
        provider.register(mcp, policy, settings)
        providers[provider_name] = provider

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

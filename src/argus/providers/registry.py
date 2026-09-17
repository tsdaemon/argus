"""Shared provider registry: maps a config provider name to its class. Both
`argus.mcp.server` (MCP-facing registration) and the agent harness (in-process
LangGraph binding) instantiate providers through `instantiate_providers` — one source
of truth for which providers exist and how their config resolves to an instance.
"""

from __future__ import annotations

from collections.abc import Collection

from argus.config import ArgusConfig
from argus.providers.base import Provider
from argus.providers.breakglass_provider import BreakglassProvider
from argus.providers.docker_provider import DockerProvider

PROVIDER_REGISTRY: dict[str, type[Provider]] = {
    "docker": DockerProvider,
    "breakglass": BreakglassProvider,
}


def instantiate_providers(
    config: ArgusConfig, *, exclude: Collection[str] = ()
) -> dict[str, Provider]:
    """Every enabled provider from `config.providers`, skipping any name in `exclude`
    (the agent harness excludes `breakglass` — see `argus.api.app`)."""
    providers: dict[str, Provider] = {}
    for provider_name, entry in config.providers.items():
        if not entry.enabled or provider_name in exclude:
            continue
        provider_cls = PROVIDER_REGISTRY.get(provider_name)
        if provider_cls is None:
            raise ValueError(
                f"Unknown provider '{provider_name}' in config. "
                f"Known providers: {sorted(PROVIDER_REGISTRY)}"
            )
        providers[provider_name] = provider_cls(entry.settings())
    return providers

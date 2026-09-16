"""The extension point: what a pluggable Argus backend looks like.

Adding a new provider means writing a class that satisfies this protocol and adding one
line to `PROVIDER_REGISTRY` in `argus.mcp.server` — no other changes needed.

`tool_specs()` is the transport-agnostic source of truth for a provider's tools.
`register()` is the MCP-facing entry point (`mcp_bind(mcp, policy, self.tool_specs(config))`
for most providers) — the split lets the agent harness bind the same tools directly via
`PolicyEngine.decide()`, with no MCP object involved.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from argus.policy import PolicyEngine, ToolClass


@dataclass(frozen=True)
class ToolSpec:
    """One tool's classification and implementation, independent of how it gets bound."""

    tool_id: str
    tool_class: ToolClass
    summary: str
    fn: Callable[..., Awaitable[Any]]
    # Forwarded to `mcp.tool(**kwargs)` when bound onto the MCP server — most tools need
    # none of this; it exists for the rare case a tool needs e.g. an explicit `name=`.
    mcp_kwargs: dict[str, Any] = field(default_factory=dict)


def mcp_bind(mcp: Any, policy: PolicyEngine, specs: list[ToolSpec]) -> None:
    """Register every spec onto `mcp`, gated through `policy` — the MCP-facing binding."""
    for spec in specs:
        policy.register(
            mcp,
            tool_id=spec.tool_id,
            tool_class=spec.tool_class,
            summary=spec.summary,
            **spec.mcp_kwargs,
        )(spec.fn)


class Provider(Protocol):
    """A pluggable backend exposing a set of classified tools."""

    name: str

    def tool_specs(self, config: dict[str, Any]) -> list[ToolSpec]:
        """This provider's tools, independent of any transport.

        `config` is this provider's own settings sub-tree from the loaded `ArgusConfig`
        (e.g. `{"socket": "unix:///var/run/docker.sock", "allowed_containers": [...]}` for
        the docker provider) — never the whole app config.
        """
        ...

    def register(self, mcp: Any, policy: PolicyEngine, config: dict[str, Any]) -> None:
        """Register this provider's tools on `mcp`, gated through `policy`."""
        ...

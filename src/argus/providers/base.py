"""The extension point: what a pluggable Argus backend looks like.

Adding a new provider (systemd, disk/SMART, network ping, ...) means writing a class that
satisfies this protocol and adding one line to `PROVIDER_REGISTRY` in `argus.server` — no
changes to the policy engine, approval backends, or any other provider.
"""

from __future__ import annotations

from typing import Any, Protocol


class Provider(Protocol):
    """A pluggable backend that registers a set of classified tools on an MCP server."""

    name: str

    def register(self, mcp: Any, policy: Any, config: dict[str, Any]) -> None:
        """Register this provider's tools on `mcp`, gated through `policy`.

        `config` is this provider's own settings sub-tree from the loaded
        `ArgusConfig` (e.g. `{"socket": "unix:///var/run/docker.sock", "allowed_containers": [...]}`
        for the docker provider) — never the whole app config.
        """
        ...

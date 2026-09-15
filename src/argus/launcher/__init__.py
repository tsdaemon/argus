"""Pluggable "start a privileged Claude Code session on the host" trigger.

Deliberately narrow, and deliberately not an MCP tool: nothing here is reachable by an
agent. It's only ever called from the human-facing break-glass web view (`argus.webapp`),
either when a human approves a specific request or when they trigger a session manually
on their own initiative.

Argus (network-reachable, agent-facing) never gains host access itself. It only ever
sends a fixed-shape payload — free text to write to a file, plus launch parameters that
come from *this deployment's own config*, never from an agent-supplied field — to
whatever `HostLauncher` is configured. What that launcher can actually do on the host is
entirely a property of how it's set up outside this code (e.g. a forced-command SSH key
that can run exactly one script and nothing else, as `SshHostLauncher` assumes) — this
interface has no opinion on that.
"""

from __future__ import annotations

from typing import Any, Protocol


class HostLauncher(Protocol):
    async def launch(self, *, session_label: str, context_markdown: str) -> None:
        """Trigger a host-side launch. Must raise on failure — callers surface that to
        the human rather than pretending a session exists when it doesn't."""
        ...


def build_launcher(config: dict[str, Any] | None) -> HostLauncher | None:
    if not config or not config.get("type"):
        return None

    launcher_type = config["type"]
    if launcher_type == "ssh":
        from argus.launcher.ssh import SshHostLauncher

        return SshHostLauncher(
            host=config["host"],
            user=config["user"],
            identity_file=config.get("identity_file"),
            port=config.get("port", 22),
            permission_mode=config.get("permission_mode", "manual"),
            ttl_seconds=config.get("ttl_seconds"),
        )

    raise ValueError(f"Unknown launcher type: {launcher_type!r}")

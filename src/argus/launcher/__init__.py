"""Pluggable "start a privileged Claude Code session on a host" trigger.

Deliberately narrow, and deliberately not an MCP tool: nothing here is reachable by an
agent. It's only ever called from the human-facing break-glass web view (`argus.mcp.webapp`),
either when a human approves a specific request or when they trigger a session manually
on their own initiative.

Argus (network-reachable, agent-facing) never gains host access itself. It only ever
sends a fixed-shape payload — free text to write to a file, plus launch parameters that
come from *this deployment's own config*, never from an agent-supplied field — to one of
the hosts named in that config. What the launcher can actually do on a host is entirely a
property of how it's set up outside this code (e.g. a forced-command SSH key that can run
exactly one script and nothing else, as `SshHostLauncher` assumes) — this interface has no
opinion on that.
"""

from __future__ import annotations

from typing import Any, Protocol


class HostLauncher(Protocol):
    @property
    def hosts(self) -> list[str]:
        """Names of the hosts a session can be launched on, as configured."""

    async def launch(self, *, host: str, session_label: str, context_markdown: str) -> None:
        """Trigger a host-side launch on one of `hosts`. Must raise on failure — callers
        surface that to the human rather than pretending a session exists when it doesn't."""
        ...


def build_launcher(config: dict[str, Any] | None) -> HostLauncher | None:
    if not config or not config.get("type"):
        return None

    launcher_type = config["type"]
    if launcher_type == "ssh":
        from argus.launcher.ssh import SshHostLauncher, SshTarget

        hosts = config.get("hosts") or {}
        if not hosts:
            raise ValueError("The ssh launcher needs at least one entry under `hosts`.")
        # Top-level settings are the defaults; a host entry overrides any of them.
        defaults = {k: v for k, v in config.items() if k not in ("type", "hosts")}
        return SshHostLauncher(
            {name: SshTarget(**{**defaults, **entry}) for name, entry in hosts.items()}
        )

    raise ValueError(f"Unknown launcher type: {launcher_type!r}")

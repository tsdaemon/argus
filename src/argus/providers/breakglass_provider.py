"""The break-glass escape hatch.

`request_break_glass` is the one tool an agent can call when it genuinely cannot resolve
something through the read/mutate tools it has. The agent and external MCP clients get
the same tool. It is classified READ, deliberately: it
cannot mutate anything on the target system, it can only record that a human's attention is
needed. There is no tool anywhere on this MCP server that approves or executes a break-glass
request — that happens entirely outside the MCP surface, via the phone-reachable web view in
`argus.mcp.webapp`. Approving a request
does not grant any access by itself; it only marks intent so the human can go open a scoped
session themselves.

The requests live in a `BreakGlassRepository` (Postgres, see `argus.db.breakglass`) that this
provider and `argus.mcp.webapp` share. Those pages sit behind the app's admin login (see
`argus.webauth.AdminAuth`), so no secret ever ends up in a bookmarked URL.
"""

from __future__ import annotations

from typing import Any

from argus.db.breakglass import (
    APPROVED,
    DENIED,
    PENDING,
    BreakGlassRepository,
    BreakGlassRequest,
)
from argus.launcher import HostLauncher, build_launcher
from argus.notify import Notifier, build_notifier
from argus.policy import PolicyEngine, ToolClass
from argus.providers.base import ToolSpec, mcp_bind
from argus.webauth import AdminAuth

__all__ = ["APPROVED", "DENIED", "PENDING", "BreakGlassRequest", "BreakglassProvider"]


class BreakglassProvider:
    name = "breakglass"

    def __init__(
        self,
        config: dict[str, Any],
        repository: BreakGlassRepository | None = None,
        admin: AdminAuth | None = None,
    ) -> None:
        if repository is None or admin is None:
            raise RuntimeError("The breakglass provider needs a BreakGlassRepository and AdminAuth.")
        self.repository = repository
        self.admin = admin
        self._approval_url = config.get("approval_url")
        self._notifier: Notifier = build_notifier(config.get("notify"))
        # Not agent-reachable: only used from the human-facing web view (argus.mcp.webapp),
        # never from the request_break_glass tool itself. See argus.launcher for why.
        self.launcher: HostLauncher | None = build_launcher(config.get("launcher"))

    def tool_specs(self, config: dict[str, Any]) -> list[ToolSpec]:
        repository = self.repository
        notifier = self._notifier
        approval_url = self._approval_url

        async def request_break_glass(
            reason: str, target_host: str, evidence: str, proposed_objective: str
        ) -> dict[str, Any]:
            """Record that this can't be resolved with the tools available and a human
            needs to open a scoped session themselves. Does not grant or perform anything.

            Args:
                reason: Why you can't resolve this yourself.
                target_host: The host/service that needs hands-on attention.
                evidence: What you've already gathered (logs, status, etc.).
                proposed_objective: What you'd want a privileged session to do.
            """
            request = await repository.create_request(
                reason=reason,
                target_host=target_host,
                evidence=evidence,
                proposed_objective=proposed_objective,
            )
            link = f"{approval_url.rstrip('/')}/breakglass" if approval_url else None
            await notifier.notify(
                title=f"Argus break-glass request: {target_host}",
                message=f"{reason}\n\nProposed: {proposed_objective}",
                url=link,
            )
            return {
                "id": request.id,
                "status": request.status,
                "note": (
                    "Filed. This does not grant any access. A human must review and "
                    "approve it out-of-band before anyone opens a privileged session."
                ),
            }

        return [
            ToolSpec(
                tool_id="breakglass.request_break_glass",
                tool_class=ToolClass.READ,
                summary="File a break-glass escalation request. Cannot grant any access by itself.",
                fn=request_break_glass,
            )
        ]

    def register(self, mcp: Any, policy: PolicyEngine, config: dict[str, Any]) -> None:
        mcp_bind(mcp, policy, self.tool_specs(config))

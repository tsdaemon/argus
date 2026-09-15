"""Pluggable approval channels for MUTATE-classified tools.

`PolicyEngine` (see `argus.policy`) calls whatever `ApprovalBackend` a deployment configures
before running a gated tool's real implementation. v1 ships one implementation
(`ElicitApproval`, using the MCP elicitation primitive for an interactive human-in-the-loop
round trip). The interface is deliberately narrow so an async approval-queue backend can be
added later for unattended runs without touching provider or policy code.
"""

from __future__ import annotations

from typing import Any, Protocol


class ApprovalBackend(Protocol):
    """Something that can ask a human whether a MUTATE action should proceed."""

    async def request(
        self,
        ctx: Any,
        *,
        action: str,
        summary: str,
        details: dict[str, Any],
    ) -> bool:
        """Return True only on an explicit, positive approval.

        Must fail closed: any ambiguity, timeout, decline, cancellation, or lack of
        client support for the underlying round-trip returns False, never raises past
        the caller as an implicit yes.
        """
        ...

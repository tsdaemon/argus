"""Interactive approval via the MCP elicitation primitive.

This is the enforcement point, not an advisory one: the gated tool's real function
(see `PolicyEngine._gate` in `argus.policy`) does not run until `request()` returns
`True`. `ctx.elicit()` blocks the tool call on a real client round-trip — the client
(e.g. Hermes, via the standard `elicitation/create` callback in the mcp Python SDK)
prompts a human and the answer comes back before we ever touch the real function.

Known gap: MCP's newer "2026-07-28 era" protocol (SEP-2322/2575) removed the
server-initiated back-channel `ctx.elicit()` depends on, replacing it with a
two-round-trip `InputRequiredResult` guard pattern instead. Hermes — the target client —
implements the older, stable `elicitation/create` callback, so this is the correct v1
mechanism against it. Against a client that has moved to the newer protocol,
`ctx.elicit()` raises `ToolError`; we catch that and fail closed (refuse the action)
rather than silently letting it through. Supporting the guard pattern is future work,
tracked in the README roadmap, not guessed at here.
"""

from __future__ import annotations

from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.elicitation import AcceptedElicitation

from argus.approval import ApprovalBackend


class ElicitApproval(ApprovalBackend):
    async def request(
        self,
        ctx: Any,
        *,
        action: str,
        summary: str,
        details: dict[str, Any],
    ) -> bool:
        message = f"Approve action `{action}`?\n\n{summary}"
        if details.get("args"):
            message += f"\n\nArguments: {details['args']}"

        try:
            result = await ctx.elicit(message, bool)
        except ToolError:
            # Most likely cause today: the client negotiated a protocol era that
            # removed the elicitation back-channel. Fail closed rather than guess.
            await _try_warn(ctx, action)
            return False

        if isinstance(result, AcceptedElicitation):
            return bool(result.data)
        # DeclinedElicitation or CancelledElicitation: fail closed.
        return False


async def _try_warn(ctx: Any, action: str) -> None:
    """Best-effort: tell the client why the action was refused, if it can log."""
    log = getattr(ctx, "warning", None)
    if log is None:
        return
    try:
        await log(
            f"Could not obtain approval for '{action}': this client's negotiated MCP "
            "protocol does not support elicitation. Refusing the action rather than "
            "proceeding without approval."
        )
    except Exception:  # noqa: BLE001, S110 — best-effort logging must never itself fail the refusal
        pass

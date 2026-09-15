"""The security boundary: tool classification and policy enforcement.

Every tool a provider exposes declares a `ToolClass`. What actually happens when an agent
calls it is decided *here*, from configuration, not by the provider and not by the model:

- READ tools are registered as-is.
- MUTATE tools are wrapped so the real function only runs after a positive approval
  round-trip (see `argus.approval`).
- DESTRUCTIVE tools are, by default, never registered at all — they don't exist in the
  tool list the agent sees, which is a stronger guarantee than refusing at call time.

This module has no knowledge of Docker, systemd, or any other backend. It only knows how
to decide and enforce.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any, TypeVar

from argus.approval import ApprovalBackend

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


class ToolClass(str, Enum):
    """How risky a tool's effect is, declared by the provider that implements it."""

    READ = "read"
    MUTATE = "mutate"
    DESTRUCTIVE = "destructive"


class PolicyDecision(str, Enum):
    """What the policy engine has decided to actually do about a tool."""

    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


# Sensible defaults if a deployment's config doesn't say otherwise: reads are free,
# mutations need a human, destructive actions don't exist until someone opts in.
_DEFAULT_DECISION_BY_CLASS: dict[ToolClass, PolicyDecision] = {
    ToolClass.READ: PolicyDecision.ALLOW,
    ToolClass.MUTATE: PolicyDecision.REQUIRE_APPROVAL,
    ToolClass.DESTRUCTIVE: PolicyDecision.DENY,
}


class PolicyEngine:
    """Resolves the effective `PolicyDecision` for a tool id and enforces it.

    `tool_id` is `"<provider>.<tool_name>"`, e.g. `"docker.restart_container"` — matching
    the vocabulary from the design discussion (`argus.get_service_status`, etc., but scoped
    per provider instead of a single flat namespace).
    """

    def __init__(
        self,
        approval_backend: ApprovalBackend,
        *,
        default_mutate: PolicyDecision = PolicyDecision.REQUIRE_APPROVAL,
        default_destructive: PolicyDecision = PolicyDecision.DENY,
        overrides: dict[str, PolicyDecision] | None = None,
    ) -> None:
        self._approval_backend = approval_backend
        self._defaults = {
            ToolClass.READ: PolicyDecision.ALLOW,
            ToolClass.MUTATE: default_mutate,
            ToolClass.DESTRUCTIVE: default_destructive,
        }
        self._overrides = dict(overrides or {})

    def decide(self, tool_id: str, tool_class: ToolClass) -> PolicyDecision:
        """What should happen when `tool_id` (classified `tool_class`) is registered."""
        if tool_id in self._overrides:
            return self._overrides[tool_id]
        return self._defaults[tool_class]

    def register(
        self,
        mcp: Any,
        *,
        tool_id: str,
        tool_class: ToolClass,
        summary: str,
        **tool_kwargs: Any,
    ) -> Callable[[F], F | None]:
        """Decorator a provider uses instead of `@mcp.tool` directly.

        Returns a decorator that, depending on the resolved decision:
        - ALLOW: registers `fn` unchanged.
        - REQUIRE_APPROVAL: registers a wrapper that gates `fn` behind
          `ApprovalBackend.request(...)` before calling it.
        - DENY: does not register anything — `fn` is never exposed as a tool.

        `summary` is the human-readable description shown in the approval prompt
        (e.g. "restart the qbittorrent container"); providers pass one per tool.
        """
        decision = self.decide(tool_id, tool_class)

        def decorator(fn: F) -> F | None:
            if decision is PolicyDecision.DENY:
                return None

            if decision is PolicyDecision.ALLOW:
                registered = mcp.tool(**tool_kwargs)(fn)
                return registered

            # REQUIRE_APPROVAL
            gated = self._gate(fn, tool_id=tool_id, summary=summary)
            registered = mcp.tool(**tool_kwargs)(gated)
            return registered

        return decorator

    def _gate(self, fn: F, *, tool_id: str, summary: str) -> F:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            ctx = _find_context(fn, args, kwargs)
            approved = await self._approval_backend.request(
                ctx,
                action=tool_id,
                summary=summary,
                details={"args": _redact(kwargs)},
            )
            if not approved:
                raise PermissionError(
                    f"'{tool_id}' was not approved. No action was taken."
                )
            return await fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]


def _find_context(fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    """Pull the `Context` argument FastMCP injects, by matching the wrapped fn's signature."""
    sig = inspect.signature(fn)
    bound = sig.bind_partial(*args, **kwargs)
    for name, value in bound.arguments.items():
        if name == "ctx" or type(value).__name__ == "Context":
            return value
    raise TypeError(
        f"Tool function {fn.__qualname__!r} must take a `ctx: Context` parameter to be "
        "registered as a MUTATE tool (needed to request approval)."
    )


def _redact(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Drop the injected context object from what gets shown in an approval prompt."""
    return {k: v for k, v in kwargs.items() if k != "ctx"}

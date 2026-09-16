"""Binds `argus.providers` `ToolSpec`s directly onto the LangGraph agent, in-process —
no MCP hop, no second implementation. `PolicyEngine.decide()` drives the same
classification the MCP surface uses; a REQUIRE_APPROVAL decision is wired into
deepagents'/LangChain's `interrupt_on` (`HumanInTheLoopMiddleware`) instead of MCP
elicitation. Pass both return values into `create_deep_agent(tools=, interrupt_on=)`.
"""

from __future__ import annotations

import inspect
from typing import Any

from langchain.agents.middleware.human_in_the_loop import InterruptOnConfig
from langchain_core.tools import StructuredTool
from pydantic import create_model

from argus.policy import PolicyDecision, PolicyEngine
from argus.providers.base import ToolSpec


def external_name(tool_id: str) -> str:
    """LangChain/Anthropic tool names can't contain '.'; `tool_id` (e.g.
    "docker.restart_container") is used everywhere else (policy, audit logging)."""
    return tool_id.replace(".", "_")


def _args_schema(fn: Any) -> type:
    """Build a pydantic model from `fn`'s signature, dropping `ctx` (only there for the
    MCP-facing gate to find, see `argus.policy`)."""
    sig = inspect.signature(fn)
    fields: dict[str, Any] = {}
    for name, param in sig.parameters.items():
        if name == "ctx":
            continue
        annotation = param.annotation if param.annotation is not inspect.Parameter.empty else Any
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[name] = (annotation, default)
    return create_model(f"{fn.__name__}_Args", **fields)


def _to_structured_tool(spec: ToolSpec) -> StructuredTool:
    has_ctx = "ctx" in inspect.signature(spec.fn).parameters

    async def call(**kwargs: Any) -> Any:
        if has_ctx:
            kwargs = {**kwargs, "ctx": None}
        return await spec.fn(**kwargs)

    return StructuredTool.from_function(
        coroutine=call,
        name=external_name(spec.tool_id),
        description=spec.summary,
        args_schema=_args_schema(spec.fn),
    )


def langchain_bind(
    policy: PolicyEngine, specs: list[ToolSpec]
) -> tuple[list[StructuredTool], dict[str, InterruptOnConfig]]:
    """Bind `specs` as LangChain tools, plus an `interrupt_on` map for anything that
    needs approval. DENY means the tool is skipped entirely, same invariant as MCP."""
    tools: list[StructuredTool] = []
    interrupt_on: dict[str, InterruptOnConfig] = {}
    for spec in specs:
        decision = policy.decide(spec.tool_id, spec.tool_class)
        if decision is PolicyDecision.DENY:
            continue

        tools.append(_to_structured_tool(spec))
        if decision is PolicyDecision.REQUIRE_APPROVAL:
            interrupt_on[external_name(spec.tool_id)] = InterruptOnConfig(
                allowed_decisions=["approve", "reject"],
                description=f"Approve `{spec.tool_id}`?\n\n{spec.summary}",
            )

    return tools, interrupt_on

"""Binds `argus.providers` `ToolSpec`s directly onto the LangGraph agent, in-process —
no MCP hop, no second implementation. `PolicyEngine.decide()` drives the same
classification the MCP surface uses; a REQUIRE_APPROVAL decision is wired into
deepagents'/LangChain's `interrupt_on` (`HumanInTheLoopMiddleware`) instead of MCP
elicitation. Pass both return values into `create_deep_agent(tools=, interrupt_on=)`.

A spec with `classify` is decided per call from the classification that
`CallClassificationMiddleware` stores on the model's message (see
`argus.agent.classification`): the interrupt's `when` predicate asks for approval only
when that call needs it, and the middleware refuses a denied call.
"""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from typing import Any

from langchain.agents.middleware.human_in_the_loop import InterruptOnConfig
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolCall
from langchain_core.tools import StructuredTool
from pydantic import create_model

from argus.agent.classification import (
    CallClassificationMiddleware,
    call_decision,
    current_risk_header,
)
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
        result = await spec.fn(**kwargs)
        header = current_risk_header.get() if spec.classify is not None else None
        return f"{header}\n{result}" if header and isinstance(result, str) else result

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
        if spec.classify is not None:
            interrupt_on[external_name(spec.tool_id)] = _per_call_interrupt(policy, spec)
        elif decision is PolicyDecision.REQUIRE_APPROVAL:
            interrupt_on[external_name(spec.tool_id)] = InterruptOnConfig(
                allowed_decisions=["approve", "reject"],
                description=f"Approve `{spec.tool_id}`?\n\n{spec.summary}",
            )

    return tools, interrupt_on



def classification_middleware(
    policy: PolicyEngine, specs: Sequence[ToolSpec]
) -> CallClassificationMiddleware | None:
    """The middleware for every bound spec that classifies its calls, or None if none do."""
    classified = {
        external_name(spec.tool_id): spec
        for spec in specs
        if spec.classify is not None
        and policy.decide(spec.tool_id, spec.tool_class) is not PolicyDecision.DENY
    }
    return CallClassificationMiddleware(policy, classified) if classified else None


def _per_call_interrupt(policy: PolicyEngine, spec: ToolSpec) -> InterruptOnConfig:
    # Both callbacks read the classification stored on the message: no I/O, and the same
    # answer when the HITL node runs again on resume.
    def when(request: ToolCallRequest) -> bool:
        decision, _ = call_decision(policy, spec, request.state, request.tool_call["id"])
        return decision is PolicyDecision.REQUIRE_APPROVAL

    def description(tool_call: ToolCall, state: Any, runtime: Any) -> str:
        _, classification = call_decision(policy, spec, state, tool_call["id"])
        # The summary is written for the model; the card already shows the arguments.
        return f"Approve `{spec.tool_id}`? {classification.note}".strip()

    return InterruptOnConfig(
        allowed_decisions=["approve", "reject"], description=description, when=when
    )

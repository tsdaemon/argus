"""Per-call classification for the agent: classify once, store it on the message.

A tool with `ToolSpec.classify` is decided per call. The HITL middleware's `when`
predicate is synchronous and runs again when an approval resumes, so it must neither do
I/O nor change its answer between the two passes. This middleware therefore classifies
every such call as soon as the model returns, asynchronously, and writes the result into
the `AIMessage`'s `response_metadata`, which is checkpointed with the conversation. The
predicate, the approval description, and the refusal of denied calls all read it from
there, and the call's result starts with it (`risk_header`). A call with no stored result
counts as MUTATE, so it is never run unseen.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ExtendedModelResponse, ModelRequest, ModelResponse
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.types import Command

from argus.policy import CallClassification, PolicyDecision, PolicyEngine, ToolClass
from argus.providers.base import ToolSpec

METADATA_KEY = "argus_call_classes"

# The risk header of the classified call being executed, for the bound tool to put at the
# top of its own output (`argus.agent.tools`). Adding it to the ToolMessage afterwards would
# miss the live result event, which the adapter takes from the tool's raw output.
current_risk_header: ContextVar[str | None] = ContextVar("current_risk_header", default=None)

_UNCLASSIFIED = CallClassification(ToolClass.MUTATE, "No stored risk classification for this call.")


def risk_header(classification: CallClassification) -> str:
    """The first line of a classified call's result: the model sees how its command was
    judged, and the UI (`frontend/src/ToolCall.tsx`) turns it into a badge. Part of the
    result, so it is kept in history like the rest of it."""
    return f"[risk: {classification.tool_class.value} · {classification.note}]"


def stored_classification(messages: Sequence[BaseMessage], tool_call_id: str) -> CallClassification:
    for message in reversed(messages):
        if isinstance(message, AIMessage) and any(c["id"] == tool_call_id for c in message.tool_calls):
            stored = message.response_metadata.get(METADATA_KEY, {}).get(tool_call_id)
            if stored is None:
                return _UNCLASSIFIED
            return CallClassification(ToolClass(stored["tool_class"]), stored["note"])
    return _UNCLASSIFIED


def call_decision(
    policy: PolicyEngine, spec: ToolSpec, state: Any, tool_call_id: str
) -> tuple[PolicyDecision, CallClassification]:
    classification = stored_classification(state["messages"], tool_call_id)
    return policy.decide(spec.tool_id, classification.tool_class), classification


class CallClassificationMiddleware(AgentMiddleware):
    """Classifies calls to `specs` (keyed by LangChain tool name) when the model returns
    them, and refuses those whose stored classification policy denies."""

    def __init__(self, policy: PolicyEngine, specs: dict[str, ToolSpec]) -> None:
        super().__init__()
        self._policy = policy
        self._specs = specs

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | AIMessage | ExtendedModelResponse:
        response = await handler(request)
        for message in response.result:
            if isinstance(message, AIMessage):
                await self._classify(message)
        return response

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        tool_call = request.tool_call
        spec = self._specs.get(tool_call["name"])
        if spec is None:
            return await handler(request)

        decision, classification = call_decision(self._policy, spec, request.state, tool_call["id"])
        header = risk_header(classification)
        if decision is PolicyDecision.DENY:
            return ToolMessage(
                content=f"{header}\nRefused: `{spec.tool_id}` does not run this.",
                name=tool_call["name"],
                tool_call_id=tool_call["id"],
                status="error",
            )
        token = current_risk_header.set(header)
        try:
            return await handler(request)
        finally:
            current_risk_header.reset(token)

    async def _classify(self, message: AIMessage) -> None:
        calls = [c for c in message.tool_calls if c["name"] in self._specs]
        if not calls:
            return
        results = await asyncio.gather(
            *(self._specs[c["name"]].classify(c["args"]) for c in calls),  # type: ignore[misc]
            return_exceptions=True,
        )
        stored = message.response_metadata.setdefault(METADATA_KEY, {})
        for tool_call, result in zip(calls, results, strict=True):
            # A failed classification stays unstored, which reads back as MUTATE.
            if isinstance(result, CallClassification):
                stored[tool_call["id"]] = {"tool_class": result.tool_class.value, "note": result.note}

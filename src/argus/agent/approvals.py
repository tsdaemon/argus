"""What a rejected or stopped approval does to the rest of the run.

`HumanInTheLoopMiddleware` answers a rejected call with an error `ToolMessage` and carries
on: the model gets its tools back at once, free to retry or work around the refusal. Here
the model's next turn after a rejection has no tools, so it can only answer. Stopping
(`STOP_REASON`, sent when the operator stops a run at its approval) runs nothing more in
the step and ends the run without another model call; a stop inside the worker ends the
planner's run too.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

# The reason `ArgusAgent` gives the HITL middleware for every call in a stopped approval.
STOP_REASON = "The operator stopped this run."
REJECT_REASON = (
    "Do not retry it, rephrase it, or work around it. Explain what you were trying to do and "
    "what is now blocked, and ask the operator how to proceed or propose a different approach."
)
STOPPED = "Stopped by the operator."
# How LangChain's HITL middleware opens a rejected call's tool message.
_REJECTED = "User rejected the tool call"


# Messages made here need their own IDs: the history archive is keyed by them.
def stopped_call(call: dict[str, Any]) -> ToolMessage:
    return ToolMessage(
        id=str(uuid4()), content=STOP_REASON, name=call["name"], tool_call_id=call["id"],
        status="error",
    )


def stopped_message() -> AIMessage:
    return AIMessage(id=str(uuid4()), content=STOPPED)


def _last_step(messages: list[BaseMessage]) -> tuple[AIMessage | None, list[ToolMessage]]:
    """The last AIMessage and the tool messages after it."""
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], AIMessage):
            return messages[index], [m for m in messages[index + 1 :] if isinstance(m, ToolMessage)]
    return None, []


class ApprovalOutcomeMiddleware(AgentMiddleware):
    """Placed before the HITL middleware, so its `after_model` runs after HITL's."""

    @hook_config(can_jump_to=["end"])
    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        ai, answered = _last_step(state["messages"])
        # Tools have not run yet, so an answer here is the HITL middleware's rejection.
        if not ai or not any(str(m.content).endswith(STOP_REASON) for m in answered):
            return None
        done = {m.tool_call_id for m in answered}
        skipped = [stopped_call(call) for call in ai.tool_calls if call["id"] not in done]
        return {"messages": [*skipped, stopped_message()], "jump_to": "end"}

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        # The worker's answer to a stopped run is its `STOPPED` message.
        last = state["messages"][-1] if state["messages"] else None
        if isinstance(last, ToolMessage) and last.content == STOPPED:
            return {"messages": [stopped_message()], "jump_to": "end"}
        return None

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        _, answered = _last_step(request.messages)
        if not any(str(m.content).startswith(_REJECTED) for m in answered):
            return await handler(request)
        response = await handler(request.override(tool_choice="none"))
        # Not every model honours `tool_choice`; a call it makes anyway is dropped.
        for message in response.result:
            if isinstance(message, AIMessage) and message.tool_calls:
                message.tool_calls = []
        return response


# The graph node `ArgusAgent.stop` writes its update as: the last after-model step.
STOP_NODE = f"{ApprovalOutcomeMiddleware.__name__}.after_model"

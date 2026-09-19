"""Wraps the LangGraph graph (`argus.agent.graph`) into an AG-UI-compatible agent
object. This is still "building the agent" — the boundary is with `argus.api.app`,
which takes the resulting object and only ever handles serving it over HTTP (FastAPI
routing, mounting MCP, ...), never how it was built.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ag_ui.core import (
    BaseEvent,
    EventType,
    Interrupt,
    ResumeEntry,
    RunAgentInput,
    RunErrorEvent,
    RunStartedEvent,
)
from ag_ui_langgraph import LangGraphAgent
from ag_ui_langgraph.utils import langchain_messages_to_agui
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command

from argus.agent.graph import build_graph
from argus.config import AgentConfig
from argus.policy import PolicyEngine
from argus.providers.base import Provider


class _InvalidApproval(ValueError):
    """A response cannot safely be applied to the thread's pending approvals."""


class ArgusAgent(LangGraphAgent):
    """Translate native AG-UI responses into ID-addressed LangGraph HITL decisions.

    The upstream single-response default drops the interrupt ID; its cancellation and
    multi-response sentinels are not LangChain HITL responses. Validate before creating
    a Command so an invalid answer cannot be persisted into the checkpoint.
    """

    async def thread_snapshot(self, thread_id: str) -> tuple[list, list[Interrupt]]:
        """Read a checkpoint without starting a run or executing pending tools."""
        state = await self.graph.aget_state({"configurable": {"thread_id": thread_id}})
        messages = self._filter_orphan_tool_messages((state.values or {}).get("messages", []))
        return (
            langchain_messages_to_agui(messages),
            self._interrupts_to_agui(self._collect_interrupts(state.tasks)),
        )

    async def delete_thread_state(self, thread_id: str) -> None:
        """Drop the thread's LangGraph checkpoints, including any pending approval."""
        await self.graph.checkpointer.adelete_thread(thread_id)

    async def run(self, input: RunAgentInput) -> AsyncIterator[BaseEvent]:
        try:
            if isinstance(input.forwarded_props, dict) and any(
                key.lower() == "command" for key in input.forwarded_props
            ):
                raise _InvalidApproval("Use resume[] with an interruptId to answer an approval.")
            async for event in super().run(input):
                yield event
        except _InvalidApproval as exc:
            # Resume translation happens before the upstream RUN_STARTED event.
            yield RunStartedEvent(
                type=EventType.RUN_STARTED, thread_id=input.thread_id, run_id=input.run_id
            )
            yield RunErrorEvent(type=EventType.RUN_ERROR, code="INVALID_APPROVAL", message=str(exc))

    def _build_command_from_agui_resume(
        self,
        entries: list[ResumeEntry],
        *,
        open_interrupts: list[Interrupt] | None = None,
    ) -> Command:
        pending = {interrupt.id: interrupt for interrupt in open_interrupts or []}
        responses: dict[str, dict[str, Any]] = {}
        for entry in entries:
            if entry.interrupt_id not in pending:
                raise _InvalidApproval("This approval is no longer pending on this thread.")
            if entry.interrupt_id in responses:
                raise _InvalidApproval("Answer each interrupt only once per request.")

            interrupt = pending[entry.interrupt_id]
            request = (interrupt.metadata or {}).get("langgraph", {}).get("raw", {})
            reviews = request.get("review_configs") if isinstance(request, dict) else None
            if not isinstance(reviews, list) or not reviews:
                raise _InvalidApproval("This interrupt does not contain tool approval requests.")

            if entry.status == "cancelled":
                decisions = [{"type": "reject", "message": "Approval cancelled."} for _ in reviews]
            else:
                payload = entry.payload
                decisions = payload.get("decisions") if isinstance(payload, dict) else None
            if not isinstance(decisions, list) or len(decisions) != len(reviews):
                raise _InvalidApproval("Provide one decision for each requested action, in order.")
            for decision, review in zip(decisions, reviews, strict=True):
                if (
                    not isinstance(decision, dict)
                    or decision.get("type") not in ("approve", "reject")
                    or decision["type"] not in review["allowed_decisions"]
                ):
                    raise _InvalidApproval(
                        "Use an allowed approve or reject decision for each action."
                    )
            responses[entry.interrupt_id] = {"decisions": decisions}

        return Command(resume=responses)


def build_agent(
    *,
    config: AgentConfig,
    providers: dict[str, Provider],
    provider_settings: dict[str, dict[str, Any]],
    policy: PolicyEngine,
    checkpointer: BaseCheckpointSaver,
) -> ArgusAgent:
    graph = build_graph(
        config=config,
        providers=providers,
        provider_settings=provider_settings,
        policy=policy,
        checkpointer=checkpointer,
    )
    return ArgusAgent(
        name="argus-agent",
        graph=graph,
        emit_interrupt_outcome=True,
        enable_legacy_on_interrupt_event=False,
    )

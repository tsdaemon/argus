"""Wraps the LangGraph graph (`argus.agent.graph`) into an AG-UI-compatible agent
object. This is still "building the agent" — the boundary is with `argus.api.app`,
which takes the resulting object and only ever handles serving it over HTTP (FastAPI
routing, mounting MCP, ...), never how it was built.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from types import MappingProxyType
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
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command

from argus.agent.classification import METADATA_KEY, stored_classification
from argus.agent.graph import build_graph
from argus.agent.tools import external_name
from argus.config import AgentConfig
from argus.policy import PolicyEngine, ToolClass
from argus.providers.base import Provider

logger = logging.getLogger(__name__)

# `forwardedProps` key of a run that continues a stalled thread from its checkpoint.
RESUME_PROP = "argus_resume"


class _InvalidApproval(ValueError):
    """A response cannot safely be applied to the thread's pending approvals."""


class ArgusAgent(LangGraphAgent):
    """Translate native AG-UI responses into ID-addressed LangGraph HITL decisions.

    The upstream single-response default drops the interrupt ID; its cancellation and
    multi-response sentinels are not LangChain HITL responses. Validate before creating
    a Command so an invalid answer cannot be persisted into the checkpoint.

    A run that died partway (a restart, a crash) leaves a checkpoint with a `next` step and
    no pending approval. `run_state` reports it, and a run with `forwardedProps.argus_resume`
    continues it from that checkpoint instead of starting over at the model.
    """

    # Provider tools' static classes by LangChain name, for reporting pending calls; set by
    # `build_agent` and carried across the per-request clone.
    tool_classes: Mapping[str, ToolClass] = MappingProxyType({})
    _resuming = False

    def get_state_snapshot(self, state: dict[str, Any]) -> dict[str, Any]:
        """STATE_SNAPSHOT without `messages`: the UI takes messages from MESSAGES_SNAPSHOT
        and reads no agent state, so each copy would only be another full transcript."""
        return {k: v for k, v in super().get_state_snapshot(state).items() if k != "messages"}

    def clone(self) -> ArgusAgent:
        copy = super().clone()
        copy.tool_classes = self.tool_classes
        return copy

    async def run_state(self, thread_id: str) -> dict[str, Any]:
        """Whether the thread's last run stopped partway, and the tool calls a resume would
        run, with their classes. Says nothing about whether a run is live right now."""
        state = await self.graph.aget_state({"configurable": {"thread_id": thread_id}})
        if not state.next or self._collect_interrupts(state.tasks):
            return {"stalled": False, "pending": []}
        messages = (state.values or {}).get("messages", [])
        answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
        last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        pending = []
        for call in last_ai.tool_calls if last_ai else []:
            if call["id"] in answered:
                continue
            if METADATA_KEY in last_ai.response_metadata:
                tool_class = stored_classification(messages, call["id"]).tool_class
            else:
                # Workspace tools and `task` are not provider tools; none needs approval.
                tool_class = self.tool_classes.get(call["name"], ToolClass.READ)
            pending.append({"name": call["name"], "args": call["args"], "class": tool_class.value})
        return {"stalled": True, "pending": pending}

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
            props = input.forwarded_props if isinstance(input.forwarded_props, dict) else {}
            if any(key.lower() == "command" for key in props):
                raise _InvalidApproval("Use resume[] with an interruptId to answer an approval.")
            if props.get(RESUME_PROP):
                if input.messages or input.resume:
                    raise _InvalidApproval("A resume run carries no messages or approvals.")
                if not (await self.run_state(input.thread_id))["stalled"]:
                    raise _InvalidApproval("This conversation has no interrupted run to resume.")
                self._resuming = True
            started = False
            async for event in super().run(input):
                started = started or event.type == EventType.RUN_STARTED
                yield event
        except _InvalidApproval as exc:
            # Resume translation happens before the upstream RUN_STARTED event.
            yield RunStartedEvent(
                type=EventType.RUN_STARTED, thread_id=input.thread_id, run_id=input.run_id
            )
            yield RunErrorEvent(type=EventType.RUN_ERROR, code="INVALID_APPROVAL", message=str(exc))
        except Exception as exc:
            # End the stream with an error the UI shows, instead of a dropped connection that
            # leaves the run's cards pending. The checkpoint keeps what finished, so a run
            # that failed partway reads as stalled and can be resumed.
            logger.exception("Agent run failed on thread %s", input.thread_id)
            if not started:
                yield RunStartedEvent(
                    type=EventType.RUN_STARTED, thread_id=input.thread_id, run_id=input.run_id
                )
            yield RunErrorEvent(type=EventType.RUN_ERROR, code=type(exc).__name__, message=str(exc))

    def get_stream_kwargs(self, input: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        # `None` continues from the checkpoint; the adapter would otherwise pass the message
        # state as new input and the graph would start over at the model. "sync" writes each
        # step's checkpoint before the next starts, so a crash cannot lose the last one.
        stream_kwargs = super().get_stream_kwargs(None if self._resuming else input, *args, **kwargs)
        return {**stream_kwargs, "durability": "sync"}

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
    agent = ArgusAgent(
        name="argus-agent",
        graph=graph,
        # The adapter builds each run's config with `ensure_config`, which fills in LangChain's
        # default limit of 25 steps and overrides the 9,999 deepagents sets on the graph.
        config={"recursion_limit": config.recursion_limit},
        emit_interrupt_outcome=True,
        enable_legacy_on_interrupt_event=False,
        # RAW re-sends every LangGraph event, each with the whole message state (attachments
        # included); in a long thread that was hundreds of MB per run. The UI never reads it.
        emit_raw_events=False,
    )
    agent.tool_classes = {
        external_name(spec.tool_id): spec.tool_class
        for name, provider in providers.items()
        for spec in provider.tool_specs(provider_settings.get(name, {}))
    }
    return agent

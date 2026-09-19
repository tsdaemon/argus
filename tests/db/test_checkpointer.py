from __future__ import annotations

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from argus.agent.graph import build_graph
from argus.config import AgentConfig
from argus.db.checkpointer import build_checkpointer
from argus.policy import PolicyEngine


class FakeToolCallingModel(BaseChatModel):
    """Minimal fake model that supports `bind_tools` (unlike `GenericFakeChatModel`),
    needed since a real invocation always binds the workspace/provider tools."""

    response: str = "ok"

    @property
    def _llm_type(self) -> str:
        return "fake-tool-calling-model"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(
        self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.response))])


class AlwaysApprove:
    async def request(self, ctx, *, action, summary, details) -> bool:
        return True


@pytest.mark.asyncio
async def test_thread_state_persists_across_separate_graph_instances(tmp_path, postgres_url):
    """The whole point of a Postgres checkpointer: a *new* graph object (as a fresh
    process restart would create) resumes a thread's prior message history rather
    than starting cold, as long as it points at the same database and thread_id."""
    config = AgentConfig(workspace_root=str(tmp_path / "workspace"))
    policy = PolicyEngine(AlwaysApprove())
    thread_config = {"configurable": {"thread_id": "test-thread-1"}}

    async with build_checkpointer(postgres_url) as checkpointer:
        graph = build_graph(
            config=config,
            providers={},
            provider_settings={},
            policy=policy,
            checkpointer=checkpointer,
            model=FakeToolCallingModel(response="hi there"),
            worker_model=FakeToolCallingModel(response="hi there"),
        )
        await graph.ainvoke({"messages": [("user", "hello")]}, config=thread_config)

    # A fresh checkpointer + a fresh graph, as a real process restart would produce.
    async with build_checkpointer(postgres_url) as checkpointer:
        graph = build_graph(
            config=config,
            providers={},
            provider_settings={},
            policy=policy,
            checkpointer=checkpointer,
            model=FakeToolCallingModel(response="hi again"),
            worker_model=FakeToolCallingModel(response="hi again"),
        )
        state = await graph.aget_state(thread_config)

    roles_and_content = [(m.type, m.content) for m in state.values["messages"]]
    assert ("human", "hello") in roles_and_content
    assert ("ai", "hi there") in roles_and_content

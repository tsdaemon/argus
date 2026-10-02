"""Real graph checks for the approval boundary in planner and worker execution."""

from __future__ import annotations

import pytest
from langchain_core.messages import ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from argus.agent.graph import build_graph
from argus.config import AgentConfig
from argus.policy import CallClassification, PolicyDecision, PolicyEngine, ToolClass
from argus.providers.base import ToolSpec
from tests.api.test_approvals import ToolCallingModel


@pytest.mark.parametrize("delegated", [False, True])
@pytest.mark.parametrize("classified", [False, True])
@pytest.mark.parametrize("risk", list(ToolClass))
async def test_unattended_calls_apply_policy_in_planner_and_worker(
    tmp_path, delegated, classified, risk
):
    executed = []

    async def run():
        executed.append(True)
        return "read result"

    async def classify(args):
        return CallClassification(risk, "test classification")

    class Provider:
        def tool_specs(self, settings):
            return [
                ToolSpec(
                    "test.run",
                    ToolClass.MUTATE if classified else risk,
                    "Test operation",
                    run,
                    classify=classify if classified else None,
                )
            ]

    worker = ToolCallingModel(tool_calls=[{"name": "test_run", "args": {}, "id": "operation"}])
    planner = (
        ToolCallingModel(
            tool_calls=[
                {
                    "name": "task",
                    "args": {"description": "Inspect a service.", "subagent_type": "worker"},
                    "id": "delegate",
                }
            ]
        )
        if delegated
        else worker
    )
    graph = build_graph(
        config=AgentConfig(workspace_root=str(tmp_path)),
        providers={"test": Provider()},
        provider_settings={},
        policy=PolicyEngine(),
        checkpointer=InMemorySaver(),
        model=planner,
        worker_model=worker,
        unattended=True,
    )
    config = {"configurable": {"thread_id": "test"}}
    result = await graph.ainvoke({"messages": [("user", "Inspect service")]}, config)
    assert executed == ([True] if risk is ToolClass.READ else [])
    assert not (await graph.aget_state(config)).interrupts
    if not delegated and risk is ToolClass.MUTATE:
        assert any(
            isinstance(m, ToolMessage) and "Refused" in m.content for m in result["messages"]
        )


async def test_unattended_honors_explicit_policy_allow(tmp_path):
    executed = []

    async def run():
        executed.append(True)
        return "result"

    class Provider:
        def tool_specs(self, settings):
            return [ToolSpec("test.run", ToolClass.MUTATE, "Test operation", run)]

    model = ToolCallingModel(tool_calls=[{"name": "test_run", "args": {}, "id": "operation"}])
    graph = build_graph(
        config=AgentConfig(workspace_root=str(tmp_path)),
        providers={"test": Provider()},
        provider_settings={},
        policy=PolicyEngine(overrides={"test.run": PolicyDecision.ALLOW}),
        model=model,
        worker_model=model,
        unattended=True,
    )
    await graph.ainvoke({"messages": [("user", "Inspect service")]})
    assert executed == [True]

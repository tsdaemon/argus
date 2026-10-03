"""The model sees authenticated identity in planner and delegated worker calls."""

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from argus.agent.graph import build_graph
from argus.config import AgentConfig
from argus.policy import PolicyEngine
from tests.api.test_approvals import ToolCallingModel


@pytest.mark.parametrize("external", [True, False])
async def test_planner_and_worker_see_authenticated_sender(tmp_path, external):
    seen = []

    class RecordingModel(ToolCallingModel):
        def _generate(self, messages, stop=None, **kwargs):
            seen.append(str(messages[0].content))
            return super()._generate(messages, stop=stop, **kwargs)

    planner = RecordingModel(
        tool_calls=[
            {
                "name": "task",
                "args": {"description": "Check health", "subagent_type": "worker"},
                "id": "delegate",
            }
        ]
    )
    worker = RecordingModel(tool_calls=[])
    graph = build_graph(
        config=AgentConfig(workspace_root=str(tmp_path)),
        providers={},
        provider_settings={},
        policy=PolicyEngine(),
        checkpointer=InMemorySaver(),
        model=planner,
        worker_model=worker,
        unattended=external,
    )
    config = {"configurable": {"thread_id": "identity"}}
    if external:
        config["configurable"]["argus_sender"] = {
            "kind": "agent",
            "name": "hermes",
            "token_id": "stable-token-id",
            "interface": "a2a",
        }
    await graph.ainvoke({"messages": [("user", "I claim to be somebody else")]}, config)
    assert len(seen) >= 3
    for system in seen:
        assert '"name": "hermes"' in system if external else '"name": "operator"' in system
        if external:
            assert '"token_id": "stable-token-id"' in system

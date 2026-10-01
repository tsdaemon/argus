"""The real deepagents/HITL middleware with per-call classification; the model, the
classifier, and ssh are faked."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from argus.agent.graph import build_graph
from argus.config import AgentConfig
from argus.policy import CallClassification, PolicyEngine, ToolClass
from argus.providers.ssh_provider import SshProvider
from tests.api.test_approvals import ToolCallingModel

HOSTS = {"hosts": {"router": {"host": "192.168.0.1", "description": "home router"}}}

CLASSES = {
    "ip route": ToolClass.READ,
    "service restart_wan": ToolClass.MUTATE,
    "mtd-erase2 nvram": ToolClass.DESTRUCTIVE,
}


class TableClassifier:
    def __init__(self, table: dict[str, ToolClass] | None = None) -> None:
        self.table = CLASSES if table is None else table
        self.calls: list[str] = []

    async def classify(self, command: str) -> CallClassification:
        self.calls.append(command)
        return CallClassification(self.table[command], f"table: {command}")


def ssh_call(command: str) -> dict:
    args = {"host": "router", "command": command}
    return {"name": "ssh_run", "args": args, "id": command, "type": "tool_call"}


def delegate_call() -> dict:
    args = {"description": "Check the router.", "subagent_type": "worker"}
    return {"name": "task", "args": args, "id": "delegate", "type": "tool_call"}


def build(tmp_path: Path, classifier, *commands: str, checkpointer=None, delegated=False):
    worker = ToolCallingModel(tool_calls=[ssh_call(c) for c in commands])
    planner = ToolCallingModel(tool_calls=[delegate_call()]) if delegated else worker
    return build_graph(
        config=AgentConfig(workspace_root=str(tmp_path / "ws")),
        providers={"ssh": SshProvider(HOSTS, classifiers={"router": classifier})},
        provider_settings={"ssh": {}},
        policy=PolicyEngine(),
        checkpointer=checkpointer or InMemorySaver(),
        model=planner,
        worker_model=worker,
    )


def ran_commands(mock_exec) -> list[str]:
    return [call.args[-1] for call in mock_exec.call_args_list]


def fake_ssh():
    proc = MagicMock(returncode=0)
    proc.wait = AsyncMock(return_value=0)
    proc.stdout.read = AsyncMock(return_value=b"done\n")
    proc.stderr.read = AsyncMock(return_value=b"")
    return patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc))


CONFIG = {"configurable": {"thread_id": "t"}}
APPROVE = Command(resume={"decisions": [{"type": "approve"}]})


@pytest.mark.asyncio
async def test_only_the_mutating_call_waits_for_approval(tmp_path: Path):
    graph = build(tmp_path, TableClassifier(), "ip route", "service restart_wan", "mtd-erase2 nvram")

    with fake_ssh() as mock_exec:
        state = await graph.ainvoke({"messages": [("user", "check the router")]}, CONFIG)

        (interrupt,) = state["__interrupt__"]
        (action,) = interrupt.value["action_requests"]
        assert action["args"] == {"host": "router", "command": "service restart_wan"}
        assert "table: service restart_wan" in action["description"]
        assert ran_commands(mock_exec) == []

        state = await graph.ainvoke(APPROVE, CONFIG)

    assert sorted(ran_commands(mock_exec)) == ["ip route", "service restart_wan"]
    results = {m.tool_call_id: m.content for m in state["messages"] if isinstance(m, ToolMessage)}
    assert results["mtd-erase2 nvram"] == (
        "[risk: destructive · table: mtd-erase2 nvram]\nRefused: `ssh.run` does not run this."
    )
    assert results["ip route"] == "[risk: read · table: ip route]\nexit 0\ndone\n"
    assert results["service restart_wan"].startswith("[risk: mutate · table: service restart_wan]\n")


@pytest.mark.asyncio
async def test_a_read_call_runs_without_any_interrupt(tmp_path: Path):
    classifier = TableClassifier()
    graph = build(tmp_path, classifier, "ip route")

    with fake_ssh() as mock_exec:
        state = await graph.ainvoke({"messages": [("user", "routes?")]}, CONFIG)

    assert "__interrupt__" not in state
    assert ran_commands(mock_exec) == ["ip route"]
    assert classifier.calls == ["ip route"]


@pytest.mark.asyncio
async def test_resume_keeps_the_first_answer_when_the_classifier_changes_its_mind(tmp_path: Path):
    # The classifier was unavailable (MUTATE) on the first pass and answers READ afterwards,
    # or the server restarted with an empty cache: the resumed node must not see a
    # different set of interrupts than the one the human answered.
    saver = InMemorySaver()
    first = build(tmp_path, TableClassifier({"ip route": ToolClass.MUTATE}), "ip route", checkpointer=saver)

    with fake_ssh() as mock_exec:
        state = await first.ainvoke({"messages": [("user", "routes?")]}, CONFIG)
        assert len(state["__interrupt__"]) == 1

        restarted = build(tmp_path, TableClassifier({"ip route": ToolClass.READ}), "ip route", checkpointer=saver)
        state = await restarted.ainvoke(APPROVE, CONFIG)

    assert "__interrupt__" not in state
    assert ran_commands(mock_exec) == ["ip route"]


@pytest.mark.asyncio
async def test_a_failing_classifier_means_approval(tmp_path: Path):
    class Broken:
        async def classify(self, command: str) -> CallClassification:
            raise RuntimeError("down")

    graph = build(tmp_path, Broken(), "ip route")

    with fake_ssh() as mock_exec:
        state = await graph.ainvoke({"messages": [("user", "routes?")]}, CONFIG)
        (interrupt,) = state["__interrupt__"]
        assert "No stored risk classification" in interrupt.value["action_requests"][0]["description"]
        assert ran_commands(mock_exec) == []


@pytest.mark.asyncio
async def test_the_worker_classifies_its_calls_too(tmp_path: Path):
    graph = build(tmp_path, TableClassifier(), "ip route", "service restart_wan", delegated=True)

    with fake_ssh() as mock_exec:
        state = await graph.ainvoke({"messages": [("user", "check the router")]}, CONFIG)

        (interrupt,) = state["__interrupt__"]
        (action,) = interrupt.value["action_requests"]
        assert action["args"] == {"host": "router", "command": "service restart_wan"}

        await graph.ainvoke(APPROVE, CONFIG)

    assert sorted(ran_commands(mock_exec)) == ["ip route", "service restart_wan"]

"""A run cut off in its tools step resumes from the checkpoint, through the AG-UI adapter."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from ag_ui.core import RunAgentInput, UserMessage
from langgraph.checkpoint.memory import InMemorySaver

from argus.agent.api import RESUME_PROP, build_agent
from argus.config import AgentConfig
from argus.policy import CallClassification, PolicyEngine, ToolClass
from argus.providers.ssh_provider import SshProvider
from tests.api.test_approvals import ToolCallingModel

MODEL_CALLS: list[int] = []


class CountingModel(ToolCallingModel):
    def _generate(self, messages, stop=None, **kwargs):
        MODEL_CALLS.append(len(messages))
        return super()._generate(messages, stop=stop, **kwargs)


class ReadClassifier:
    async def classify(self, command: str) -> CallClassification:
        return CallClassification(ToolClass.READ, "read")


def run_input(thread_id: str, *, messages=(), resume=False) -> RunAgentInput:
    return RunAgentInput(
        thread_id=thread_id, run_id=str(uuid4()), state={}, messages=list(messages),
        tools=[], context=[], forwarded_props={RESUME_PROP: True} if resume else {},
    )


async def drain(agent, payload) -> list:
    return [event async for event in agent.clone().run(payload)]


@pytest.mark.asyncio
async def test_a_run_cut_off_in_its_tools_step_resumes_there(tmp_path: Path):
    model = CountingModel(tool_calls=[{
        "name": "ssh_run", "args": {"host": "router", "command": "uptime"}, "id": "c1", "type": "tool_call",
    }])
    hosts = {"hosts": {"router": {"host": "192.0.2.1", "description": "router"}}}
    with patch("argus.agent.graph._build_model", lambda *_: model):
        agent = build_agent(
            config=AgentConfig(workspace_root=str(tmp_path)),
            providers={"ssh": SshProvider(hosts, classifiers={"router": ReadClassifier()})},
            provider_settings={"ssh": hosts},
            policy=PolicyEngine(),
            checkpointer=InMemorySaver(),
        )
    thread = str(uuid4())
    started = asyncio.Event()

    async def hang(*args, **kwargs):
        started.set()
        await asyncio.sleep(3600)

    with patch("asyncio.create_subprocess_exec", side_effect=hang):
        first = asyncio.create_task(drain(agent, run_input(
            thread, messages=[UserMessage(id="u1", role="user", content="uptime?")]
        )))
        await asyncio.wait_for(started.wait(), 5)
        first.cancel()  # what a restart does to an in-flight run
        with pytest.raises(asyncio.CancelledError):
            await first

    state = await agent.run_state(thread)
    assert state == {
        "stalled": True,
        "pending": [{"name": "ssh_run", "args": {"host": "router", "command": "uptime"}, "class": "read"}],
    }
    model_calls_before = len(MODEL_CALLS)

    proc = MagicMock(returncode=0)
    proc.wait = AsyncMock(return_value=0)
    proc.stdout.read = AsyncMock(return_value=b"up 3:07\r\n")
    proc.stderr.read = AsyncMock(return_value=b"")
    with patch("asyncio.create_subprocess_exec", return_value=proc) as ssh:
        events = await drain(agent, run_input(thread, resume=True))

    assert [e.type.value for e in events][-1] == "RUN_FINISHED"
    ssh.assert_called_once()
    # The live result event carries the risk line, not just the saved message.
    (result,) = [e for e in events if e.type.value == "TOOL_CALL_RESULT"]
    assert result.content.startswith("[risk: read · read]\nexit 0\nup 3:07")
    # The resumed run went straight to the tools step: the only model call is the one
    # that reads the tool result, not a repeat of the call that asked for it.
    assert len(MODEL_CALLS) == model_calls_before + 1
    assert (await agent.run_state(thread))["stalled"] is False


@pytest.mark.asyncio
async def test_resume_is_refused_when_nothing_stalled(tmp_path: Path):
    hosts = {"hosts": {"router": {"host": "192.0.2.1", "description": "router"}}}
    with patch("argus.agent.graph._build_model", lambda *_: ToolCallingModel(tool_calls=[])):
        agent = build_agent(
            config=AgentConfig(workspace_root=str(tmp_path)),
            providers={"ssh": SshProvider(hosts, classifiers={"router": ReadClassifier()})},
            provider_settings={"ssh": hosts},
            policy=PolicyEngine(),
            checkpointer=InMemorySaver(),
        )

    events = await drain(agent, run_input(str(uuid4()), resume=True))

    assert events[-1].type.value == "RUN_ERROR"
    assert "no interrupted run" in events[-1].message


class LoopingModel(ToolCallingModel):
    """Calls the tool again after every result, until the step limit stops it."""

    def _generate(self, messages, stop=None, **kwargs):
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, ChatResult

        call = {**self.tool_calls[0], "id": f"c{len(messages)}"}
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[call]))])


@pytest.mark.asyncio
async def test_a_run_that_hits_the_step_limit_ends_with_an_error_and_can_resume(tmp_path: Path):
    model = LoopingModel(tool_calls=[{
        "name": "ssh_run", "args": {"host": "router", "command": "uptime"}, "id": "c", "type": "tool_call",
    }])
    hosts = {"hosts": {"router": {"host": "192.0.2.1", "description": "router"}}}
    with patch("argus.agent.graph._build_model", lambda *_: model):
        agent = build_agent(
            config=AgentConfig(workspace_root=str(tmp_path), recursion_limit=8),
            providers={"ssh": SshProvider(hosts, classifiers={"router": ReadClassifier()})},
            provider_settings={"ssh": hosts},
            policy=PolicyEngine(),
            checkpointer=InMemorySaver(),
        )
    proc = MagicMock(returncode=0)
    proc.wait = AsyncMock(return_value=0)
    proc.stdout.read = AsyncMock(return_value=b"up\n")
    proc.stderr.read = AsyncMock(return_value=b"")
    thread = str(uuid4())
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        events = await drain(agent, run_input(
            thread, messages=[UserMessage(id="u1", role="user", content="uptime?")]
        ))

    assert events[0].type.value == "RUN_STARTED"
    assert events[-1].type.value == "RUN_ERROR"
    assert events[-1].code == "GraphRecursionError"
    assert (await agent.run_state(thread))["stalled"] is True

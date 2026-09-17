"""Exercise the real HTTP/graph approval path; only the model and Docker SDK are faked."""

from __future__ import annotations

import asyncio
import json
import socket
from contextlib import asynccontextmanager
from copy import deepcopy
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest
import uvicorn
from deepagents import GeneralPurposeSubagentProfile, HarnessProfile, register_harness_profile
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from argus.api.app import build_app
from argus.config import AgentConfig, ArgusConfig, ProviderEntry


class ToolCallingModel(BaseChatModel):
    """Request the configured tools once, then finish after receiving their results."""

    tool_calls: list[dict]

    @property
    def _llm_type(self) -> str:
        return "argus-approval-test"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages: list[BaseMessage], stop=None, **kwargs) -> ChatResult:
        response = (
            AIMessage(content="Finished reviewing the operation.")
            if any(isinstance(message, ToolMessage) for message in messages)
            else AIMessage(content="", tool_calls=deepcopy(self.tool_calls))
        )
        return ChatResult(generations=[ChatGeneration(message=response)])

    async def _agenerate(self, messages, stop=None, **kwargs) -> ChatResult:
        return self._generate(messages, stop=stop, **kwargs)


register_harness_profile(
    "toolcallingmodel",
    HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)),
)


def restart_call(name: str = "example-service") -> dict:
    return {
        "name": "docker_restart_container",
        "args": {"name": name},
        "id": f"restart-{name}",
        "type": "tool_call",
    }


@pytest.fixture
def approval_app(tmp_path, monkeypatch):
    def create(*, delegated=False, mcp=False, calls=None, worker_count=1, checkpointer=None):
        if mcp:
            monkeypatch.setenv("ARGUS_MCP_TOKEN", "test-token")
        else:
            monkeypatch.delenv("ARGUS_MCP_TOKEN", raising=False)
        calls = calls or [restart_call()]
        worker = ToolCallingModel(tool_calls=calls)
        planner = (
            ToolCallingModel(
                tool_calls=[
                    {
                        "name": "task",
                        "args": {
                            "description": "Restart example-service.",
                            "subagent_type": "worker",
                        },
                        "id": f"delegate-{i}",
                        "type": "tool_call",
                    }
                    for i in range(worker_count)
                ]
            )
            if delegated
            else worker
        )
        monkeypatch.setattr(
            "argus.agent.graph._build_model",
            lambda config, name: planner if name == config.model else worker,
        )
        docker_client = MagicMock()
        monkeypatch.setattr(
            "argus.providers.docker_provider.docker.DockerClient", lambda **_: docker_client
        )
        config = ArgusConfig(
            providers={
                "docker": ProviderEntry(
                    enabled=True, allowed_containers=["example-service", "second-service"]
                )
            },
            agent=AgentConfig(workspace_root=str(tmp_path / "workspace")),
        )
        return build_app(config, checkpointer or InMemorySaver()), docker_client

    return create


@asynccontextmanager
async def client_for(app):
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://argus.test"
        ) as client,
    ):
        yield client


def run_input(thread_id: str, *, resume: list[dict] | None = None) -> dict:
    payload = {
        "threadId": thread_id,
        "runId": str(uuid4()),
        "messages": [{"id": "request", "role": "user", "content": "Restart example-service."}],
        "state": {},
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    if resume is not None:
        payload["resume"] = resume
    return payload


def answer(interrupt: dict, decision="approve", *, status="resolved") -> dict:
    return {
        "interruptId": interrupt["id"],
        "status": status,
        "payload": {"decisions": [{"type": decision}]},
    }


async def run_agent(client: httpx.AsyncClient, payload: dict, *, expect_error=False) -> list[dict]:
    response = await client.post("/agent", json=payload, headers={"Accept": "text/event-stream"})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert events
    if expect_error:
        assert [event["type"] for event in events] == ["RUN_STARTED", "RUN_ERROR"]
        assert events[-1]["code"] == "INVALID_APPROVAL"
    else:
        assert not [event for event in events if event["type"] == "RUN_ERROR"], events
    return events


def pending_interrupts(events: list[dict]) -> list[dict]:
    finished = [event for event in events if event["type"] == "RUN_FINISHED"]
    assert len(finished) == 1
    assert finished[0]["outcome"]["type"] == "interrupt"
    assert not [event for event in events if event.get("name") == "on_interrupt"]
    return finished[0]["outcome"]["interrupts"]


def assert_finished(events: list[dict]):
    assert events[-1]["type"] == "RUN_FINISHED"
    assert events[-1].get("outcome", {}).get("type") != "interrupt"


@pytest.mark.parametrize("decision", ["approve", "reject"])
@pytest.mark.parametrize("delegated", [False, True], ids=["planner", "worker"])
@pytest.mark.parametrize("mcp", [False, True], ids=["agent-only", "with-mcp"])
async def test_restart_pauses_then_resumes_through_http(approval_app, decision, delegated, mcp):
    app, docker_client = approval_app(delegated=delegated, mcp=mcp)
    thread_id = str(uuid4())
    async with client_for(app) as client:
        events = await run_agent(client, run_input(thread_id))
        docker_client.containers.get.assert_not_called()
        (interrupt,) = pending_interrupts(events)
        request = interrupt["metadata"]["langgraph"]["raw"]
        assert request["action_requests"][0]["name"] == "docker_restart_container"
        assert request["action_requests"][0]["args"] == {"name": "example-service"}
        assert request["review_configs"][0]["allowed_decisions"] == ["approve", "reject"]

        # Refreshing/reconnecting must re-emit the same pause without executing anything.
        events = await run_agent(client, run_input(thread_id))
        assert pending_interrupts(events)[0]["id"] == interrupt["id"]
        docker_client.containers.get.assert_not_called()

        events = await run_agent(client, run_input(thread_id, resume=[answer(interrupt, decision)]))
        assert_finished(events)
        if decision == "approve":
            docker_client.containers.get.assert_called_once_with("example-service")
            docker_client.containers.get.return_value.restart.assert_called_once_with(timeout=30)
        else:
            docker_client.containers.get.assert_not_called()

        # Retrying an already-consumed approval must not execute the operation again.
        await run_agent(
            client, run_input(thread_id, resume=[answer(interrupt, decision)]), expect_error=True
        )
        assert docker_client.containers.get.return_value.restart.call_count == (
            decision == "approve"
        )


@pytest.mark.parametrize(
    "invalid", ["wrong-id", "duplicate", "missing-decision", "edit", "legacy", "legacy-camel"]
)
async def test_invalid_approval_preserves_pending_action(approval_app, invalid):
    app, docker_client = approval_app()
    thread_id = str(uuid4())
    async with client_for(app) as client:
        (interrupt,) = pending_interrupts(await run_agent(client, run_input(thread_id)))
        entry = answer(interrupt)
        if invalid == "wrong-id":
            entry["interruptId"] = "stale-interrupt"
        elif invalid == "missing-decision":
            entry["payload"] = {"decisions": []}
        elif invalid == "edit":
            entry["payload"] = {
                "decisions": [
                    {
                        "type": "edit",
                        "edited_action": {
                            "name": "docker_restart_container",
                            "args": {"name": "second-service"},
                        },
                    }
                ]
            }
        entries = [entry, entry] if invalid == "duplicate" else [entry]
        payload = run_input(thread_id, resume=entries)
        if invalid.startswith("legacy"):
            del payload["resume"]
            key = "Command" if invalid == "legacy-camel" else "command"
            payload["forwardedProps"] = {key: {"resume": entry["payload"]}}
        await run_agent(client, payload, expect_error=True)
        docker_client.containers.get.assert_not_called()
        (still_pending,) = pending_interrupts(await run_agent(client, run_input(thread_id)))
        assert still_pending["id"] == interrupt["id"]
        assert_finished(await run_agent(client, run_input(thread_id, resume=[answer(interrupt)])))
        docker_client.containers.get.return_value.restart.assert_called_once_with(timeout=30)


async def test_cancel_rejects_every_action_in_the_interrupt(approval_app):
    app, docker_client = approval_app(calls=[restart_call(), restart_call("second-service")])
    thread_id = str(uuid4())
    async with client_for(app) as client:
        (interrupt,) = pending_interrupts(await run_agent(client, run_input(thread_id)))
        # Even an attached approve payload cannot override cancellation.
        events = await run_agent(
            client, run_input(thread_id, resume=[answer(interrupt, status="cancelled")])
        )
        assert_finished(events)
        docker_client.containers.get.assert_not_called()


async def test_batched_decisions_apply_to_the_matching_actions(approval_app):
    app, docker_client = approval_app(calls=[restart_call(), restart_call("second-service")])
    thread_id = str(uuid4())
    async with client_for(app) as client:
        (interrupt,) = pending_interrupts(await run_agent(client, run_input(thread_id)))
        entry = answer(interrupt)
        entry["payload"] = {"decisions": [{"type": "reject"}, {"type": "approve"}]}
        assert_finished(await run_agent(client, run_input(thread_id, resume=[entry])))
        docker_client.containers.get.assert_called_once_with("second-service")
        docker_client.containers.get.return_value.restart.assert_called_once_with(timeout=30)


async def test_parallel_worker_interrupts_resume_by_id(approval_app):
    app, docker_client = approval_app(delegated=True, worker_count=2)
    thread_id = str(uuid4())
    async with client_for(app) as client:
        interrupts = pending_interrupts(await run_agent(client, run_input(thread_id)))
        assert len(interrupts) == 2
        docker_client.containers.get.assert_not_called()
        # Reverse the wire order: matching must use IDs, not list position.
        entries = [answer(interrupts[1], "reject"), answer(interrupts[0], "approve")]
        assert_finished(await run_agent(client, run_input(thread_id, resume=entries)))
        docker_client.containers.get.return_value.restart.assert_called_once_with(timeout=30)


async def test_approval_round_trip_over_tcp(approval_app):
    """Verify SSE over a listening Uvicorn server as well as the in-process ASGI tests."""
    app, docker_client = approval_app(mcp=True, delegated=True)
    thread_id = str(uuid4())
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if serving.done():
                        await serving
                        pytest.fail("Uvicorn exited before starting")
                    await asyncio.sleep(0.01)
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                (interrupt,) = pending_interrupts(await run_agent(client, run_input(thread_id)))
                docker_client.containers.get.assert_not_called()
                assert_finished(
                    await run_agent(client, run_input(thread_id, resume=[answer(interrupt)]))
                )
                docker_client.containers.get.return_value.restart.assert_called_once_with(
                    timeout=30
                )
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, timeout=10)

"""Model spend recorded per thread, from the gateway's reported cost, without any network."""

import re
from typing import TypedDict
from uuid import uuid4

import httpx
import pytest
from langchain_core.outputs import ChatResult
from langgraph.graph import END, START, StateGraph

from argus.agent.cost import CostRecorder
from argus.agent.graph import build_graph
from argus.agent.phoenix_costs import backfill, phoenix_url
from argus.config import AgentConfig
from argus.policy import PolicyEngine
from tests.agent.test_reported_cost import model
from tests.api.test_approvals import ToolCallingModel
from tests.fakes import InMemoryHistory


class State(TypedDict):
    reply: str


def graph(chat_model, *, stream: bool):
    async def call(state: State) -> State:
        if stream:
            text = ""
            async for chunk in chat_model.astream("hi"):
                text += chunk.content
            return {"reply": text}
        return {"reply": (await chat_model.ainvoke("hi")).content}

    builder = StateGraph(State)
    builder.add_node("call", call)
    builder.add_edge(START, "call")
    builder.add_edge("call", END)
    return builder.compile()


async def test_calls_in_a_thread_run_add_to_that_thread():
    history = InMemoryHistory()
    thread_id = await history.create_thread()
    recorder = CostRecorder(history.add_cost)
    config = {"configurable": {"thread_id": str(thread_id)}}

    await graph(model(callbacks=[recorder]), stream=True).ainvoke({}, config)
    await graph(model(cost=0.5, callbacks=[recorder]), stream=False).ainvoke({}, config)

    assert (await history.get_thread(thread_id))["cost_usd"] == 0.5123


async def test_a_call_outside_a_thread_or_without_a_cost_records_nothing():
    recorded = []

    async def record(thread_id, usd):
        recorded.append((thread_id, usd))

    recorder = CostRecorder(record)
    await model(callbacks=[recorder]).ainvoke("hi")
    config = {"configurable": {"thread_id": str(uuid4())}}
    await graph(model(cost=None, callbacks=[recorder]), stream=True).ainvoke({}, config)
    await graph(model(callbacks=[recorder]), stream=True).ainvoke(
        {}, {"configurable": {"thread_id": "not-a-uuid"}}
    )

    assert recorded == []


async def test_a_failed_write_does_not_fail_the_run():
    async def record(thread_id, usd):
        raise RuntimeError("database down")

    config = {"configurable": {"thread_id": str(uuid4())}}
    result = await graph(model(callbacks=[CostRecorder(record)]), stream=True).ainvoke({}, config)

    assert result["reply"] == "hi"


def phoenix(costs: dict[str, float | None]):
    """A fake Phoenix GraphQL answering `getProjectSessionById` aliases from `costs`."""
    queries = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.read().decode()
        queries.append(query)
        data = {}
        for alias, sid in _aliases(query):
            cost = costs.get(sid, "absent")
            data[alias] = None if cost == "absent" else {"costSummary": {"total": {"cost": cost}}}
        return httpx.Response(200, json={"data": data})

    client = httpx.AsyncClient(base_url="http://phoenix.test", transport=httpx.MockTransport(handler))
    return client, queries


def _aliases(query: str):
    return re.findall(r'(s\d+): getProjectSessionById\(sessionId: \\"([0-9a-f-]+)\\"\)', query)


async def test_backfill_raises_threads_to_their_phoenix_total_once():
    history = InMemoryHistory()
    behind, ahead, untraced, free = [await history.create_thread() for _ in range(4)]
    await history.add_cost(behind, 0.25)
    await history.add_cost(ahead, 2.0)
    client, queries = phoenix({str(behind): 1.0, str(ahead): 1.5, str(free): None})

    changes = await backfill(history, client)
    again = await backfill(history, client)

    assert [(t["id"], before, after) for t, before, after in changes] == [(behind, 0.25, 1.0)]
    assert again == []
    assert (await history.get_thread(behind))["cost_usd"] == 1.0
    assert (await history.get_thread(ahead))["cost_usd"] == 2.0
    assert (await history.get_thread(untraced))["cost_usd"] == 0.0
    assert len(_aliases(queries[0])) == 4


def test_phoenix_url_comes_from_the_otlp_http_endpoint():
    assert phoenix_url("http://phoenix:6006/v1/traces") == "http://phoenix:6006"
    with pytest.raises(ValueError):
        phoenix_url("http://phoenix:4317")


class PricedModel(ToolCallingModel):
    """`ToolCallingModel` whose every call reports a cost, as OpenRouter does."""

    async def _agenerate(self, messages, stop=None, **kwargs) -> ChatResult:
        result = self._generate(messages, stop=stop, **kwargs)
        return ChatResult(generations=result.generations, llm_output={"token_usage": {"cost": 0.01}})


async def test_the_planner_and_its_worker_both_add_to_the_thread(tmp_path):
    history = InMemoryHistory()
    thread_id = await history.create_thread()
    recorder = CostRecorder(history.add_cost)
    delegate = {
        "name": "task",
        "args": {"description": "Look around.", "subagent_type": "worker"},
        "id": "delegate",
        "type": "tool_call",
    }
    agent = build_graph(
        config=AgentConfig(workspace_root=str(tmp_path)),
        providers={},
        provider_settings={},
        policy=PolicyEngine(None),
        model=PricedModel(tool_calls=[delegate], callbacks=[recorder]),
        worker_model=PricedModel(tool_calls=[], callbacks=[recorder]),
    )

    await agent.ainvoke(
        {"messages": [("user", "check")]}, {"configurable": {"thread_id": str(thread_id)}}
    )

    # Planner delegates, worker answers, planner finishes.
    assert (await history.get_thread(thread_id))["cost_usd"] == pytest.approx(0.03)

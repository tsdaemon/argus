import asyncio
import json
from uuid import UUID, uuid4

import pytest
from langchain_core.language_models import FakeListChatModel
from langgraph.checkpoint.memory import InMemorySaver

from tests.api.test_approvals import (
    ToolCallingModel,
    answer,
    client_for,
    pending_interrupts,
    run_agent,
    run_input,
)
from tests.fakes import InMemoryHistory


def events(response):
    assert response.status_code == 200, response.text
    return [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]


@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_history_and_pending_approval_survive_rebuilding_app(approval_app, decision):
    # The stores outlive each app instance, as Postgres does across restarts.
    checkpointer, history = InMemorySaver(), InMemoryHistory()
    app, docker = approval_app(checkpointer=checkpointer, history=history, mcp=True)
    async with client_for(app) as client:
        created = await client.post("/api/threads")
        assert created.status_code == 201
        thread_id = created.json()["id"]
        paused = await run_agent(client, run_input(thread_id.upper()))
        (interrupt,) = pending_interrupts(paused)
        rows = (await client.get("/api/threads")).json()
        assert rows[0]["id"] == thread_id
        assert rows[0]["title"] == "Restart example-service."
        docker.containers.get.assert_not_called()

    # A new graph/API instance must restore both messages and the actual pending ID;
    # replay is a GET, never a fresh agent invocation.
    app, docker = approval_app(checkpointer=checkpointer, history=history)
    async with client_for(app) as client:
        restored = events(await client.get(f"/api/threads/{thread_id}/connect"))
        assert pending_interrupts(restored)[0]["id"] == interrupt["id"]
        messages = restored[1]["messages"]
        assert messages[0]["content"] == "Restart example-service."
        docker.containers.get.assert_not_called()
        payload = run_input(thread_id, resume=[answer(interrupt, decision)])
        payload["messages"] = []
        await run_agent(client, payload)
        restored = events(await client.get(f"/api/threads/{thread_id}/connect"))
        assert not restored[-1].get("outcome")
        # Every message, including those read back from the checkpoint, says when it was sent.
        assert all("argus_time" in m["metadata"] for m in restored[1]["messages"])
        assert restored[1]["messages"][-1]["content"] == "Finished reviewing the operation."
        assert docker.containers.get.return_value.restart.call_count == (decision == "approve")

        assert (await client.get(f"/api/threads/{uuid4()}/connect")).status_code == 404
        assert (await client.get("/api/threads/not-a-uuid/connect")).status_code == 422
        assert (await client.get("/api/threads?limit=0")).status_code == 422


async def test_live_stream_preserves_archive_outside_model_context(approval_app, monkeypatch):
    prompts = []
    original = ToolCallingModel._generate

    def generate(self, messages, *args, **kwargs):
        prompts.append(list(messages))
        return original(self, messages, *args, **kwargs)

    monkeypatch.setattr(ToolCallingModel, "_generate", generate)
    history = InMemoryHistory()
    app, _docker = approval_app(history=history)
    async with client_for(app) as client:
        thread_id = (await client.post("/api/threads")).json()["id"]
        await history.save_chat_messages(
            UUID(thread_id),
            [{"id": "archived", "role": "user", "content": "Old, summarized context"}],
        )
        stream = await run_agent(client, run_input(thread_id))
        snapshots = [event for event in stream if event["type"] == "MESSAGES_SNAPSHOT"]
        assert snapshots
        assert all(event["messages"][0]["id"] == "archived" for event in snapshots)
        assert prompts
        assert all(message.id != "archived" for prompt in prompts for message in prompt)


async def test_delete_thread_removes_history_and_checkpoints(approval_app):
    checkpointer, history = InMemorySaver(), InMemoryHistory()
    app, _docker = approval_app(checkpointer=checkpointer, history=history)
    async with client_for(app) as client:
        thread_id = (await client.post("/api/threads")).json()["id"]
        other_id = (await client.post("/api/threads")).json()["id"]
        assert pending_interrupts(await run_agent(client, run_input(thread_id)))
        config = {"configurable": {"thread_id": thread_id}}
        assert await checkpointer.aget_tuple(config) is not None

        assert (await client.delete(f"/api/threads/{thread_id}")).status_code == 204

        assert (await client.get(f"/api/threads/{thread_id}")).status_code == 404
        assert (await client.get(f"/api/threads/{thread_id}/connect")).status_code == 404
        ids = [t["id"] for t in (await client.get("/api/threads")).json()]
        assert thread_id not in ids and other_id in ids
        assert await history.chat_messages(UUID(thread_id)) == []
        assert await checkpointer.aget_tuple(config) is None
        # Deleting again, or a malformed id, is a client error rather than a crash.
        assert (await client.delete(f"/api/threads/{thread_id}")).status_code == 404
        assert (await client.delete("/api/threads/not-a-uuid")).status_code == 422


async def test_history_routes_report_unavailable_without_a_store(approval_app):
    app, _docker = approval_app(history=None)
    async with client_for(app) as client:
        assert (await client.get("/api/threads")).status_code == 503
        assert (await client.get("/api/costs")).status_code == 503
        assert (await client.delete(f"/api/threads/{uuid4()}")).status_code == 503


async def test_threads_and_total_report_model_spend(approval_app):
    history = InMemoryHistory()
    app, _docker = approval_app(history=history)
    async with client_for(app) as client:
        thread_id = (await client.post("/api/threads")).json()["id"]
        await history.add_cost(UUID(thread_id), 0.25)

        assert (await client.get(f"/api/threads/{thread_id}")).json()["cost_usd"] == 0.25
        assert (await client.get("/api/threads")).json()[0]["cost_usd"] == 0.25
        assert (await client.get("/api/costs")).json() == {"total_usd": 0.25}


async def test_operator_renames_a_conversation(approval_app):
    app, _docker = approval_app(history=InMemoryHistory())
    async with client_for(app) as client:
        thread_id = (await client.post("/api/threads")).json()["id"]
        renamed = await client.patch(f"/api/threads/{thread_id}", json={"title": "  NAS   disks "})
        assert renamed.status_code == 200
        assert (renamed.json()["title"], renamed.json()["title_source"]) == ("NAS disks", "user")
        await run_agent(client, run_input(thread_id))
        assert (await client.get(f"/api/threads/{thread_id}")).json()["title"] == "NAS disks"

        for blank in ["", "   "]:
            response = await client.patch(f"/api/threads/{thread_id}", json={"title": blank})
            assert response.status_code == 422
        too_long = await client.patch(f"/api/threads/{thread_id}", json={"title": "x" * 101})
        assert too_long.status_code == 422
        missing = await client.patch(f"/api/threads/{uuid4()}", json={"title": "Gone"})
        assert missing.status_code == 404


async def test_title_names_the_first_exchange_once_it_has_a_reply(approval_app, monkeypatch):
    prompts = []
    titler = FakeListChatModel(responses=['"Example service restart."'])
    original = FakeListChatModel._call

    def call(self, messages, *args, **kwargs):
        prompts.append(messages[-1].content)
        return original(self, messages, *args, **kwargs)

    async def title_of(client, thread_id):
        for _ in range(100):
            row = (await client.get(f"/api/threads/{thread_id}")).json()
            if row["title_source"]:
                break
            await asyncio.sleep(0.01)
        return row["title"], row["title_source"]

    monkeypatch.setattr(FakeListChatModel, "_call", call)
    monkeypatch.setattr("argus.api.app._build_model", lambda *_: titler)
    history = InMemoryHistory()
    app, _docker = approval_app(history=history, generate_titles=True)
    async with client_for(app) as client:
        thread_id = (await client.post("/api/threads")).json()["id"]
        # The run stops at the approval with no reply yet, so there is nothing to name.
        (interrupt,) = pending_interrupts(await run_agent(client, run_input(thread_id)))
        assert await title_of(client, thread_id) == ("Restart example-service.", None)
        assert prompts == []

        payload = run_input(thread_id, resume=[answer(interrupt, "approve")])
        payload["messages"] = []
        await run_agent(client, payload)
        assert await title_of(client, thread_id) == ("Example service restart", "generated")
        (prompt,) = prompts
        assert "Restart example-service." in prompt
        assert "Finished reviewing the operation." in prompt

        # Later runs keep it, without asking the model again.
        await run_agent(client, run_input(thread_id))
        await asyncio.sleep(0.05)
        assert len(prompts) == 1


async def test_thread_list_defaults_to_human_and_a2a_is_separate(approval_app):
    history = InMemoryHistory()
    app, _ = approval_app(history=history)
    human = await history.create_thread(title="mine")
    agent = await history.create_thread(title="theirs", origin="a2a")
    async with client_for(app) as client:
        default = [r["id"] for r in (await client.get("/api/threads")).json()]
        assert str(human) in default and str(agent) not in default
        a2a = (await client.get("/api/threads?origin=a2a")).json()
        assert [r["id"] for r in a2a] == [str(agent)] and a2a[0]["origin"] == "a2a"
        assert (await client.get(f"/api/threads/{agent}")).json()["origin"] == "a2a"
        assert (await client.get("/api/threads?origin=bogus")).status_code == 422
        stale = await client.get("/api/threads?source=agent")
        assert stale.status_code == 422 and "origin" in stale.json()["detail"]
        made = (await client.post("/api/threads")).json()
        assert made["origin"] == "human"

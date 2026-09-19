import json
from uuid import UUID, uuid4

import pytest
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
        assert (await client.delete(f"/api/threads/{uuid4()}")).status_code == 503

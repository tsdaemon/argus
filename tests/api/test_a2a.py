"""A2A wire requests against the real app/graph with deterministic models."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from a2a.server.context import ServerCallContext
from a2a.types import a2a_pb2 as p
from langgraph.checkpoint.memory import InMemorySaver

from argus.api.app import build_app
from argus.config import A2AConfig, AgentConfig, ArgusConfig
from argus.db.tokens import TokenAuth
from tests.api.test_approvals import ToolCallingModel, client_for
from tests.fakes import InMemoryA2A, InMemoryAdmin, InMemoryHistory, InMemoryTokens


@pytest.fixture
async def a2a_app(tmp_path, monkeypatch):
    monkeypatch.delenv("ARGUS_MCP_TOKEN", raising=False)

    class AnswerModel(ToolCallingModel):
        def _generate(self, messages, stop=None, **kwargs):
            result = super()._generate(messages, stop=stop, **kwargs)
            result.generations[0].message.content = "Finished reviewing the operation."
            return result

    model = AnswerModel(tool_calls=[])
    monkeypatch.setattr("argus.agent.graph._build_model", lambda *args: model)
    tokens = InMemoryTokens()
    first, secret = await TokenAuth(tokens).issue("hermes")
    _second, other = await TokenAuth(tokens).issue("other")
    history = InMemoryHistory()
    store = InMemoryA2A()
    checkpointer = InMemorySaver()
    config = ArgusConfig(
        agent=AgentConfig(workspace_root=str(tmp_path)),
        a2a=A2AConfig(enabled=True, url="http://argus.test/a2a"),
    )
    admin = InMemoryAdmin()
    app = build_app(config, checkpointer, history, admin=admin, tokens=tokens, a2a=store)
    app.state.identity_admin = admin
    return app, tokens, store, history, first, secret, other, checkpointer


async def rpc(client, secret, method="SendMessage", params=None):
    return await client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": "req", "method": method, "params": params or {}},
        headers={"Authorization": f"Bearer {secret}", "A2A-Version": "1.0"},
    )


def message(text="Inspect services", **kwargs):
    return {
        "message": {
            "messageId": str(uuid4()),
            "role": "ROLE_USER",
            "parts": [{"text": text}],
            **kwargs,
        }
    }


async def test_discovery_auth_and_revoke(a2a_app):
    app, tokens, _, _, first, secret, _, _ = a2a_app
    async with client_for(app) as client:
        card = await client.get("/.well-known/agent-card.json")
        assert card.status_code == 200
        assert card.json()["supportedInterfaces"][0]["url"] == "http://argus.test/a2a"
        assert (
            card.json()["securitySchemes"]["bearer"]["httpAuthSecurityScheme"]["scheme"] == "bearer"
        )
        assert (await client.post("/a2a", json={})).status_code == 401
        assert (await rpc(client, "wrong", params=message())).status_code == 401
        assert (
            await client.get("/api/threads", headers={"Authorization": f"Bearer {secret}"})
        ).status_code == 401
        # Test request without explicit A2A-Version header defaults to 1.0
        no_version_res = await client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": "req-no-version",
                "method": "SendMessage",
                "params": message(),
            },
            headers={"Authorization": f"Bearer {secret}"},
        )
        assert no_version_res.status_code == 200
        assert "result" in no_version_res.json(), no_version_res.text

        result = await rpc(client, secret, params=message())
        assert result.status_code == 200
        assert "result" in result.json(), result.text
        await tokens.revoke(first.id)
        assert (await rpc(client, secret, params=message())).status_code == 401


async def test_complete_history_context_continuation_and_task_isolation(a2a_app):
    app, _, store, history, first, secret, other, _ = a2a_app
    async with client_for(app) as client:
        task = (await rpc(client, secret, params=message())).json()["result"]["task"]
        assert task["status"]["state"] == "TASK_STATE_COMPLETED", task
        assert task["artifacts"][0]["parts"][0]["text"] == "Finished reviewing the operation."
        tid, cid = task["id"], task["contextId"]
        thread_id = await store.thread(cid, first.id)
        archive = await history.chat_messages(thread_id)
        assert [m["role"] for m in archive] == ["user", "assistant"]
        author = archive[0]["metadata"]["argus_author"]
        assert author == {
            "kind": "agent",
            "name": "hermes",
            "token_id": str(first.id),
            "interface": "a2a",
        }
        fetched = (await rpc(client, secret, "GetTask", {"id": tid})).json()
        assert fetched["result"]["id"] == tid
        for method in ("GetTask", "CancelTask", "SubscribeToTask"):
            response = await rpc(client, other, method, {"id": tid})
            assert "error" in response.json(), response.text
        assert (await rpc(client, other, "ListTasks")).json()["result"]["tasks"] == []
        # Other cannot access or hijack secret's existing context
        assert "error" in (await rpc(client, other, params=message(contextId=cid))).json()
        assert "error" in (await rpc(client, other, params=message(taskId=tid))).json()
        another = (await rpc(client, secret, params=message("Continue", contextId=cid))).json()
        assert another["result"]["task"]["contextId"] == cid
        assert another["result"]["task"]["id"] != tid
        assert len(await history.chat_messages(thread_id)) == 4
        assert (await history.chat_messages(thread_id))[0]["metadata"]["argus_author"] == author
        # Browser replay keeps the server-authenticated author after a reload.
        from argus.webauth import SESSION_COOKIE, AdminAuth, sign_session

        account, _ = await AdminAuth(app.state.identity_admin).get_or_create()
        client.cookies.set(SESSION_COOKIE, sign_session(account.session_secret, account.username))
        replay = await client.get(f"/api/threads/{thread_id}/connect")
        snapshots = [
            json.loads(line.removeprefix("data: "))
            for line in replay.text.splitlines()
            if line.startswith("data: ")
        ]
        snapshot = next(e for e in snapshots if e["type"] == "MESSAGES_SNAPSHOT")
        assert snapshot["messages"][0]["metadata"]["argus_author"] == author
        from tests.api.test_approvals import run_input

        forged = run_input(str(thread_id))
        forged["messages"][0].update({"name": "hermes", "metadata": {"argus_author": author}})
        response = await client.post("/agent", json=forged)
        assert response.status_code == 200
        updated = await history.chat_messages(thread_id)
        assert updated[0]["metadata"]["argus_author"] == author
        newest = next(m for m in reversed(updated) if m["role"] == "user")
        assert "argus_author" not in (newest.get("metadata") or {})
        assert not newest.get("name")


async def test_client_provided_context_id_creation_and_continuation(a2a_app):
    app, _, store, history, first, secret, other, _ = a2a_app
    custom_cid = "ctx-oc-argus"
    async with client_for(app) as client:
        # First message with a brand new client-provided contextId creates a new context
        res1 = (
            await rpc(client, secret, params=message("First message", contextId=custom_cid))
        ).json()
        assert "result" in res1, res1
        task1 = res1["result"]["task"]
        assert task1["contextId"] == custom_cid
        assert task1["status"]["state"] == "TASK_STATE_COMPLETED"

        thread_id = await store.thread(custom_cid, first.id)
        assert thread_id is not None
        messages = await history.chat_messages(thread_id)
        assert len(messages) == 2
        assert [m["role"] for m in messages] == ["user", "assistant"]

        # Subsequent message with the same contextId continues the thread
        res2 = (
            await rpc(client, secret, params=message("Second message", contextId=custom_cid))
        ).json()
        assert "result" in res2, res2
        task2 = res2["result"]["task"]
        assert task2["contextId"] == custom_cid
        assert task2["id"] != task1["id"]

        messages2 = await history.chat_messages(thread_id)
        assert len(messages2) == 4

        # Another token cannot reuse or hijack the same contextId
        other_res = (
            await rpc(client, other, params=message("Hijack attempt", contextId=custom_cid))
        ).json()
        assert "error" in other_res, other_res


async def test_streaming_and_unsupported_inputs(a2a_app):
    app, _, _, _, _, secret, _, _ = a2a_app
    async with client_for(app) as client:
        response = await rpc(client, secret, "SendStreamingMessage", message())
        assert response.headers["content-type"].startswith("text/event-stream"), response.text
        events = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        assert not any("error" in e for e in events), events
        assert events[-1]["result"]["statusUpdate"]["status"]["state"] == "TASK_STATE_COMPLETED"
        bad = message()
        bad["message"]["parts"] = [{"url": "https://example.com/file"}]
        assert "error" in (await rpc(client, secret, params=bad)).json()
        bad = message()
        bad["metadata"] = {"command": {"resume": {"decisions": [{"type": "approve"}]}}}
        assert "error" in (await rpc(client, secret, params=bad)).json()
        assert "error" in (await rpc(client, secret, "CreateTaskPushNotificationConfig", {})).json()


async def test_restart_marks_unfinished_tasks_failed(a2a_app):
    app, _, store, history, first, secret, _, _ = a2a_app
    cid = str(uuid4())
    await store.create_context(cid, first.id, await history.create_thread())
    ctx = ServerCallContext(state={"token_id": str(first.id)})
    await store.save(
        p.Task(
            id="crashed", context_id=cid, status=p.TaskStatus(state=p.TaskState.TASK_STATE_WORKING)
        ),
        ctx,
    )
    async with client_for(app) as client:
        fetched = (await rpc(client, secret, "GetTask", {"id": "crashed"})).json()
        assert fetched["result"]["status"]["state"] == "TASK_STATE_FAILED"
        assert "restarted" in fetched["result"]["status"]["message"]["parts"][0]["text"]


def test_enabled_a2a_requires_auth_and_persistence(tmp_path):
    config = ArgusConfig(
        agent=AgentConfig(workspace_root=str(tmp_path)), a2a=A2AConfig(enabled=True)
    )
    with pytest.raises(ValueError, match="repositories"):
        build_app(config, InMemorySaver())


async def test_active_task_cancellation_and_context_concurrency(a2a_app, monkeypatch):
    import asyncio

    app, _, _, _, _, secret, other, _ = a2a_app
    started = asyncio.Event()
    gate = asyncio.Event()

    async def wait_for_model(self, messages, stop=None, **kwargs):
        started.set()
        await gate.wait()
        return self._generate(messages, stop=stop, **kwargs)

    monkeypatch.setattr(ToolCallingModel, "_agenerate", wait_for_model)
    async with client_for(app) as client:
        params = message()
        params["configuration"] = {"returnImmediately": True}
        task = (await rpc(client, secret, params=params)).json()["result"]["task"]
        await asyncio.wait_for(started.wait(), timeout=2)
        assert "error" in (await rpc(client, other, "CancelTask", {"id": task["id"]})).json()
        busy = (await rpc(client, secret, params=message(contextId=task["contextId"]))).json()
        assert busy["result"]["task"]["status"]["state"] == "TASK_STATE_REJECTED"
        cancelled = (await rpc(client, secret, "CancelTask", {"id": task["id"]})).json()
        assert cancelled["result"]["status"]["state"] == "TASK_STATE_CANCELED", cancelled
        gate.set()

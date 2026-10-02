"""One contract for in-memory stores and migrated Postgres repositories."""

from uuid import uuid4

import pytest
from a2a.server.context import ServerCallContext
from a2a.types import a2a_pb2 as p
from a2a.utils.errors import InvalidParamsError

from argus.db.a2a import SqlA2A
from argus.db.history import SqlHistory
from argus.db.tokens import SqlTokens, TokenAuth
from tests.fakes import InMemoryA2A, InMemoryHistory, InMemoryTokens


@pytest.fixture(params=["memory", "sql"])
def stores(request):
    if request.param == "memory":
        return InMemoryTokens(), InMemoryA2A(), InMemoryHistory()
    engine = request.getfixturevalue("sql_engine")
    return SqlTokens(engine), SqlA2A(engine), SqlHistory(engine)


async def test_token_lifecycle(stores):
    tokens, _, _ = stores
    auth = TokenAuth(tokens)
    token, secret = await auth.issue("hermes")
    assert (await tokens.get(token.id)).secret_hash == token.secret_hash
    assert await auth.verify(secret)
    assert (await tokens.get(token.id)).last_used_at
    assert any(t.id == token.id for t in await tokens.list())
    assert await tokens.revoke(token.id)
    assert await auth.verify(secret) is None
    assert not await tokens.revoke(uuid4())


async def test_owned_task_persistence_and_pagination(stores):
    tokens, tasks, history = stores
    first, _ = await TokenAuth(tokens).issue("first")
    second, _ = await TokenAuth(tokens).issue("second")
    context_id = str(uuid4())
    thread_id = await history.create_thread()
    await tasks.create_context(context_id, first.id, thread_id)
    ctx = ServerCallContext(state={"token_id": str(first.id)})
    other = ServerCallContext(state={"token_id": str(second.id)})
    assert await tasks.thread(context_id, first.id) == thread_id
    assert await tasks.thread(context_id, second.id) is None
    ids = []
    for _ in range(3):
        task = p.Task(
            id=str(uuid4()),
            context_id=context_id,
            status=p.TaskStatus(state=p.TaskState.TASK_STATE_WORKING),
        )
        task.status.timestamp.GetCurrentTime()
        await tasks.save(task, ctx)
        ids.append(task.id)
    assert await tasks.get(ids[0], other) is None
    await tasks.delete(ids[0], other)
    assert await tasks.get(ids[0], ctx)
    page = await tasks.list(p.ListTasksRequest(context_id=context_id, page_size=2), ctx)
    assert len(page.tasks) == 2 and page.next_page_token
    next_page = await tasks.list(
        p.ListTasksRequest(context_id=context_id, page_size=2, page_token=page.next_page_token), ctx
    )
    assert len(next_page.tasks) == 1
    assert not {t.id for t in page.tasks} & {t.id for t in next_page.tasks}
    assert not (await tasks.list(p.ListTasksRequest(), other)).tasks
    fetched = await tasks.get(ids[0], ctx)
    fetched.status.state = p.TaskState.TASK_STATE_COMPLETED
    assert (await tasks.get(ids[0], ctx)).status.state == p.TaskState.TASK_STATE_WORKING
    await tasks.save(fetched, ctx)
    assert (await tasks.get(ids[0], ctx)).status.state == p.TaskState.TASK_STATE_COMPLETED
    with pytest.raises(InvalidParamsError):
        await tasks.save(fetched, other)
    await tasks.delete(ids[0], ctx)
    assert await tasks.get(ids[0], ctx) is None


async def test_restart_recovery_only_changes_unfinished_tasks(stores):
    tokens, tasks, history = stores
    token, _ = await TokenAuth(tokens).issue("caller")
    ctx = ServerCallContext(state={"token_id": str(token.id)})
    cid = str(uuid4())
    await tasks.create_context(cid, token.id, await history.create_thread())
    working = p.Task(
        id=str(uuid4()), context_id=cid, status=p.TaskStatus(state=p.TaskState.TASK_STATE_WORKING)
    )
    complete = p.Task(
        id=str(uuid4()), context_id=cid, status=p.TaskStatus(state=p.TaskState.TASK_STATE_COMPLETED)
    )
    await tasks.save(working, ctx)
    await tasks.save(complete, ctx)
    await tasks.recover()
    assert (await tasks.get(working.id, ctx)).status.state == p.TaskState.TASK_STATE_FAILED
    assert (await tasks.get(complete.id, ctx)).status.state == p.TaskState.TASK_STATE_COMPLETED

from __future__ import annotations

import pytest

from argus.db import repo


async def _fresh_thread(pool):
    return await repo.create_thread(pool, title="test thread")


@pytest.mark.asyncio
async def test_create_thread(db_pool):
    thread_id = await repo.create_thread(db_pool, title="diagnose theseus")
    assert thread_id is not None


@pytest.mark.asyncio
async def test_run_lifecycle(db_pool):
    thread_id = await _fresh_thread(db_pool)

    run_id = await repo.start_run(db_pool, thread_id=thread_id)
    await repo.finish_run(db_pool, run_id, status="completed")

    async with db_pool.connection() as conn:
        row = await (await conn.execute("SELECT status, error FROM runs WHERE id = %s", (run_id,))).fetchone()
    assert row == ("completed", None)


@pytest.mark.asyncio
async def test_run_failure_records_error(db_pool):
    thread_id = await _fresh_thread(db_pool)
    run_id = await repo.start_run(db_pool, thread_id=thread_id)

    await repo.finish_run(db_pool, run_id, status="failed", error="boom")

    async with db_pool.connection() as conn:
        row = await (await conn.execute("SELECT status, error FROM runs WHERE id = %s", (run_id,))).fetchone()
    assert row == ("failed", "boom")


@pytest.mark.asyncio
async def test_record_message_stores_jsonb_content(db_pool):
    thread_id = await _fresh_thread(db_pool)
    run_id = await repo.start_run(db_pool, thread_id=thread_id)

    message_id = await repo.record_message(
        db_pool, thread_id=thread_id, run_id=run_id, role="user", content={"text": "restart qbittorrent"}
    )

    async with db_pool.connection() as conn:
        row = await (
            await conn.execute("SELECT role, content FROM messages WHERE id = %s", (message_id,))
        ).fetchone()
    assert row == ("user", {"text": "restart qbittorrent"})


@pytest.mark.asyncio
async def test_tool_call_lifecycle(db_pool):
    thread_id = await _fresh_thread(db_pool)
    run_id = await repo.start_run(db_pool, thread_id=thread_id)

    tool_call_id = await repo.record_tool_call_start(
        db_pool,
        thread_id=thread_id,
        run_id=run_id,
        tool_id="docker.restart_container",
        tool_class="mutate",
        args={"name": "qbittorrent"},
    )
    await repo.record_tool_call_finish(db_pool, tool_call_id, status="success", result={"restarted": True})

    async with db_pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT status, result, args FROM tool_calls WHERE id = %s", (tool_call_id,)
            )
        ).fetchone()
    assert row == ("success", {"restarted": True}, {"name": "qbittorrent"})


@pytest.mark.asyncio
async def test_approval_lifecycle(db_pool):
    thread_id = await _fresh_thread(db_pool)
    run_id = await repo.start_run(db_pool, thread_id=thread_id)
    tool_call_id = await repo.record_tool_call_start(
        db_pool,
        thread_id=thread_id,
        run_id=run_id,
        tool_id="docker.restart_container",
        tool_class="mutate",
        args={"name": "qbittorrent"},
    )

    approval_id = await repo.record_approval(
        db_pool, tool_call_id=tool_call_id, tool_id="docker.restart_container", summary="Restart it?"
    )
    async with db_pool.connection() as conn:
        pending = await (
            await conn.execute("SELECT decision, decided_at FROM approvals WHERE id = %s", (approval_id,))
        ).fetchone()
    assert pending == (None, None)

    await repo.set_approval_decision(db_pool, approval_id, decision="approve", decided_by="anatolii")

    async with db_pool.connection() as conn:
        decided = await (
            await conn.execute(
                "SELECT decision, decided_by FROM approvals WHERE id = %s", (approval_id,)
            )
        ).fetchone()
    assert decided == ("approve", "anatolii")

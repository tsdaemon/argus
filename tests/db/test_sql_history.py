"""Postgres-only behaviour: audit records and foreign-key cascades (skips without a database)."""

from sqlalchemy import text


async def _scalar(engine, query, **params):
    async with engine.connect() as conn:
        return (await conn.execute(text(query), params)).one()


async def _seed(history, thread_id):
    run = await history.start_run(thread_id=thread_id)
    await history.record_message(thread_id=thread_id, run_id=run, role="user", content={"t": "hi"})
    call = await history.record_tool_call_start(
        thread_id=thread_id,
        run_id=run,
        tool_id="docker.restart_container",
        tool_class="mutate",
        args={"name": "qbittorrent"},
    )
    approval = await history.record_approval(
        tool_call_id=call, tool_id="docker.restart_container", summary="Restart it?"
    )
    return run, call, approval


async def test_run_lifecycle(sql_history, sql_engine):
    run = await sql_history.start_run(thread_id=await sql_history.create_thread())
    await sql_history.finish_run(run, status="failed", error="boom")

    row = await _scalar(sql_engine, "SELECT status, error FROM runs WHERE id = :i", i=run)
    assert tuple(row) == ("failed", "boom")


async def test_tool_call_and_approval_lifecycle(sql_history, sql_engine):
    thread = await sql_history.create_thread()
    _run, call, approval = await _seed(sql_history, thread)

    pending = await _scalar(
        sql_engine, "SELECT decision, decided_at FROM approvals WHERE id = :i", i=approval
    )
    assert tuple(pending) == (None, None)

    await sql_history.record_tool_call_finish(call, status="success", result={"ok": True})
    await sql_history.set_approval_decision(approval, decision="approve", decided_by="anatolii")

    done = await _scalar(
        sql_engine, "SELECT status, result, args FROM tool_calls WHERE id = :i", i=call
    )
    assert tuple(done) == ("success", {"ok": True}, {"name": "qbittorrent"})
    decided = await _scalar(
        sql_engine, "SELECT decision, decided_by FROM approvals WHERE id = :i", i=approval
    )
    assert tuple(decided) == ("approve", "anatolii")


async def test_deleting_a_thread_cascades_to_every_dependent_row(sql_history, sql_engine):
    doomed, kept = await sql_history.create_thread(), await sql_history.create_thread()
    await _seed(sql_history, doomed)
    await _seed(sql_history, kept)

    assert await sql_history.delete_thread(doomed) is True

    counts = """SELECT
        (SELECT count(*) FROM runs WHERE thread_id = :t),
        (SELECT count(*) FROM messages WHERE thread_id = :t),
        (SELECT count(*) FROM tool_calls WHERE thread_id = :t),
        (SELECT count(*) FROM approvals a JOIN tool_calls c ON c.id = a.tool_call_id
            WHERE c.thread_id = :t)"""
    assert tuple(await _scalar(sql_engine, counts, t=doomed)) == (0, 0, 0, 0)
    assert tuple(await _scalar(sql_engine, counts, t=kept)) == (1, 1, 1, 1)

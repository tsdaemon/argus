"""Raw functions over the Argus-owned Postgres tables (see `argus.db`'s docstring). No
ORM, mirroring `argus.providers.breakglass_provider`'s plain-SQL sqlite store. Schema is
managed by Alembic (`alembic upgrade head`, `src/argus/db/migrations/`) — this module
only ever reads/writes rows, never creates tables.
"""

from __future__ import annotations

import uuid
from typing import Any

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool


async def create_thread(pool: AsyncConnectionPool, *, title: str | None = None) -> uuid.UUID:
    thread_id = uuid.uuid4()
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO threads (id, title) VALUES (%s, %s)", (thread_id, title))
    return thread_id


async def start_run(pool: AsyncConnectionPool, *, thread_id: uuid.UUID) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO runs (id, thread_id) VALUES (%s, %s)", (run_id, thread_id))
    return run_id


async def finish_run(
    pool: AsyncConnectionPool, run_id: uuid.UUID, *, status: str, error: str | None = None
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE runs SET status = %s, error = %s, finished_at = now() WHERE id = %s",
            (status, error, run_id),
        )


async def record_message(
    pool: AsyncConnectionPool,
    *,
    thread_id: uuid.UUID,
    run_id: uuid.UUID | None,
    role: str,
    content: Any,
) -> uuid.UUID:
    message_id = uuid.uuid4()
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO messages (id, thread_id, run_id, role, content) VALUES (%s, %s, %s, %s, %s)",
            (message_id, thread_id, run_id, role, Jsonb(content)),
        )
    return message_id


async def record_tool_call_start(
    pool: AsyncConnectionPool,
    *,
    thread_id: uuid.UUID,
    run_id: uuid.UUID,
    tool_id: str,
    tool_class: str,
    args: dict[str, Any],
) -> uuid.UUID:
    tool_call_id = uuid.uuid4()
    async with pool.connection() as conn:
        await conn.execute(
            """INSERT INTO tool_calls (id, thread_id, run_id, tool_id, tool_class, args)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (tool_call_id, thread_id, run_id, tool_id, tool_class, Jsonb(args)),
        )
    return tool_call_id


async def record_tool_call_finish(
    pool: AsyncConnectionPool, tool_call_id: uuid.UUID, *, status: str, result: Any = None
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE tool_calls SET status = %s, result = %s, completed_at = now() WHERE id = %s",
            (status, Jsonb(result) if result is not None else None, tool_call_id),
        )


async def record_approval(
    pool: AsyncConnectionPool, *, tool_call_id: uuid.UUID, tool_id: str, summary: str
) -> uuid.UUID:
    """Create a pending approval record — call `set_approval_decision` once resolved."""
    approval_id = uuid.uuid4()
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO approvals (id, tool_call_id, tool_id, summary) VALUES (%s, %s, %s, %s)",
            (approval_id, tool_call_id, tool_id, summary),
        )
    return approval_id


async def set_approval_decision(
    pool: AsyncConnectionPool,
    approval_id: uuid.UUID,
    *,
    decision: str,
    decided_by: str | None = None,
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE approvals SET decision = %s, decided_by = %s, decided_at = now() WHERE id = %s",
            (decision, decided_by, approval_id),
        )

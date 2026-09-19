"""LangGraph's own State store — Postgres-backed checkpoints, entirely managed by
`langgraph-checkpoint-postgres` (never hand-edited; see `argus.db.history` for the
separate, Argus-owned History tables).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


def make_checkpointer(database_url: str) -> tuple[AsyncPostgresSaver, AsyncConnectionPool]:
    """A saver over a not-yet-open pool, so an app can be built before its event loop runs.

    Open the pool (`async with pool:`) and call `await saver.setup()` before use. The saver
    captures the running event loop, so call this from inside one (uvicorn calls an app factory
    from within its serving loop).
    """
    pool = AsyncConnectionPool(
        database_url,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    return AsyncPostgresSaver(pool), pool


@asynccontextmanager
async def build_checkpointer(database_url: str) -> AsyncGenerator[AsyncPostgresSaver, None]:
    saver, pool = make_checkpointer(database_url)
    async with pool:
        await saver.setup()  # idempotent, safe on every boot
        yield saver

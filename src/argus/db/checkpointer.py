"""LangGraph's own State store — Postgres-backed checkpoints, entirely managed by
`langgraph-checkpoint-postgres` (never hand-edited; see `argus.db.repo` for the
separate, Argus-owned History tables).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver


@asynccontextmanager
async def build_checkpointer(database_url: str) -> AsyncGenerator[AsyncPostgresSaver, None]:
    async with AsyncPostgresSaver.from_conn_string(database_url) as checkpointer:
        await checkpointer.setup()
        yield checkpointer

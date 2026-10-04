"""The History store behind a repository interface.

Routes depend on `HistoryRepository`, never on a database: `SqlHistory` implements it with
SQLAlchemy over Postgres, and tests use an in-memory fake. The `runs`, `tool_calls`, and
`approvals` audit methods live on `SqlHistory` only until the agent writes those records.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from argus.db.models import Approval, Message, Run, Thread, ToolCall


class HistoryRepository(Protocol):
    async def create_thread(
        self, *, title: str | None = None, origin: str = "human"
    ) -> uuid.UUID: ...

    async def touch_thread(self, thread_id: uuid.UUID, title: str | None) -> None:
        """Index a conversation, bumping `updated_at`; keep the first title it got."""

    async def list_threads(
        self, *, limit: int = 100, offset: int = 0, origin: str | None = None
    ) -> list[dict]:
        """Newest first; `origin` ('human' or 'a2a') restricts to one kind of thread."""

    async def get_thread(self, thread_id: uuid.UUID) -> dict | None: ...

    async def rename_thread(self, thread_id: uuid.UUID, title: str) -> bool:
        """The operator's title, which nothing replaces; False if the thread does not exist."""

    async def set_generated_title(self, thread_id: uuid.UUID, title: str) -> bool:
        """A model's title, applied only over the first-message placeholder."""

    async def delete_thread(self, thread_id: uuid.UUID) -> bool:
        """Delete a conversation and everything under it; False if it does not exist."""

    async def save_chat_messages(self, thread_id: uuid.UUID, messages: list) -> None:
        """Upsert AG-UI messages by their stable ID, never deleting earlier ones."""

    async def chat_messages(self, thread_id: uuid.UUID) -> list:
        """AG-UI messages in first-seen order, each with `metadata.argus_time` (first seen)."""

    async def add_cost(self, thread_id: uuid.UUID, usd: float) -> None:
        """Add model spend to a thread's total; an unknown thread is ignored."""

    async def total_cost(self) -> float:
        """Model spend across all threads, in USD."""


TIME_KEY = "argus_time"


def with_time(message: dict, at: datetime) -> dict:
    """Stamp a message for display; the stored content never carries it."""
    return {**message, "metadata": {**message.get("metadata", {}), TIME_KEY: at.isoformat()}}


def make_engine(database_url: str) -> AsyncEngine:
    """An async engine on the psycopg3 driver, from the same URL the agent uses."""
    return create_async_engine(database_url.replace("postgresql://", "postgresql+psycopg://"))


def _thread_row(thread: Thread, author: dict | None = None) -> dict:
    return {
        "id": thread.id,
        "title": thread.title,
        "title_source": thread.title_source,
        "created_at": thread.created_at,
        "updated_at": thread.updated_at,
        "cost_usd": float(thread.cost_usd or 0),
        "origin": thread.origin,
        "author": author if author and author.get("kind") == "agent" else None,
    }


class SqlHistory:
    def __init__(self, engine: AsyncEngine) -> None:
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def create_thread(
        self, *, title: str | None = None, origin: str = "human"
    ) -> uuid.UUID:
        thread = Thread(id=uuid.uuid4(), title=title, origin=origin)
        async with self._session.begin() as session:
            session.add(thread)
        return thread.id

    async def touch_thread(self, thread_id: uuid.UUID, title: str | None) -> None:
        statement = insert(Thread).values(id=thread_id, title=title)
        statement = statement.on_conflict_do_update(
            index_elements=[Thread.id],
            set_={
                "updated_at": func.now(),
                "title": func.coalesce(Thread.title, statement.excluded.title),
            },
        )
        async with self._session.begin() as session:
            await session.execute(statement)

    def _author(self):
        # Origin is the first user turn, so an operator reply does not move an agent thread.
        return (
            select(Message.content["metadata"]["argus_author"])
            .where(Message.thread_id == Thread.id, Message.role == "user")
            .order_by(Message.chat_order)
            .limit(1)
            .correlate(Thread)
            .scalar_subquery()
        )

    async def list_threads(
        self, *, limit: int = 100, offset: int = 0, origin: str | None = None
    ) -> list[dict]:
        author = self._author()
        query = select(Thread, author.label("author"))
        if origin is not None:
            query = query.where(Thread.origin == origin)
        query = (
            query.order_by(Thread.updated_at.desc(), Thread.id.desc()).limit(limit).offset(offset)
        )
        async with self._session() as session:
            return [_thread_row(thread, sender) for thread, sender in await session.execute(query)]

    async def get_thread(self, thread_id: uuid.UUID) -> dict | None:
        async with self._session() as session:
            row = (
                await session.execute(select(Thread, self._author()).where(Thread.id == thread_id))
            ).first()
            return _thread_row(*row) if row else None

    async def rename_thread(self, thread_id: uuid.UUID, title: str) -> bool:
        return await self._set_title(
            update(Thread).where(Thread.id == thread_id), title, "user"
        )

    async def set_generated_title(self, thread_id: uuid.UUID, title: str) -> bool:
        return await self._set_title(
            update(Thread).where(Thread.id == thread_id, Thread.title_source.is_(None)),
            title,
            "generated",
        )

    async def _set_title(self, statement, title: str, source: str) -> bool:
        async with self._session.begin() as session:
            result = await session.execute(statement.values(title=title, title_source=source))
            return result.rowcount > 0

    async def delete_thread(self, thread_id: uuid.UUID) -> bool:
        # The relationships use passive_deletes, so this issues one DELETE and the
        # ON DELETE CASCADE foreign keys remove every dependent row.
        async with self._session.begin() as session:
            thread = await session.get(Thread, thread_id)
            if thread is None:
                return False
            await session.delete(thread)
            return True

    async def save_chat_messages(self, thread_id: uuid.UUID, messages: list) -> None:
        async with self._session.begin() as session:
            for message in messages:
                statement = insert(Message).values(
                    id=uuid.uuid4(),
                    thread_id=thread_id,
                    role=message["role"],
                    content=message,
                    agui_id=message["id"],
                )
                await session.execute(
                    statement.on_conflict_do_update(
                        constraint="messages_thread_agui_key",
                        set_={"content": statement.excluded.content},
                    )
                )

    async def chat_messages(self, thread_id: uuid.UUID) -> list:
        query = (
            select(Message.content, Message.created_at)
            .where(Message.thread_id == thread_id, Message.agui_id.is_not(None))
            .order_by(Message.chat_order)
        )
        async with self._session() as session:
            return [with_time(content, at) for content, at in await session.execute(query)]

    async def add_cost(self, thread_id: uuid.UUID, usd: float) -> None:
        # Not `updated_at`: spending is not activity that should reorder the list.
        async with self._session.begin() as session:
            await session.execute(
                update(Thread)
                .where(Thread.id == thread_id)
                .values(cost_usd=Thread.cost_usd + Decimal(str(usd)))
            )

    async def total_cost(self) -> float:
        async with self._session() as session:
            return float(await session.scalar(select(func.coalesce(func.sum(Thread.cost_usd), 0))))

    async def start_run(self, *, thread_id: uuid.UUID) -> uuid.UUID:
        run = Run(id=uuid.uuid4(), thread_id=thread_id)
        async with self._session.begin() as session:
            session.add(run)
        return run.id

    async def finish_run(self, run_id: uuid.UUID, *, status: str, error: str | None = None):
        async with self._session.begin() as session:
            await session.execute(
                update(Run)
                .where(Run.id == run_id)
                .values(status=status, error=error, finished_at=func.now())
            )

    async def record_message(
        self, *, thread_id: uuid.UUID, run_id: uuid.UUID | None, role: str, content: Any
    ) -> uuid.UUID:
        message = Message(
            id=uuid.uuid4(), thread_id=thread_id, run_id=run_id, role=role, content=content
        )
        async with self._session.begin() as session:
            session.add(message)
        return message.id

    async def record_tool_call_start(
        self,
        *,
        thread_id: uuid.UUID,
        run_id: uuid.UUID,
        tool_id: str,
        tool_class: str,
        args: dict[str, Any],
    ) -> uuid.UUID:
        call = ToolCall(
            id=uuid.uuid4(),
            thread_id=thread_id,
            run_id=run_id,
            tool_id=tool_id,
            tool_class=tool_class,
            args=args,
        )
        async with self._session.begin() as session:
            session.add(call)
        return call.id

    async def record_tool_call_finish(
        self, tool_call_id: uuid.UUID, *, status: str, result: Any = None
    ) -> None:
        async with self._session.begin() as session:
            await session.execute(
                update(ToolCall)
                .where(ToolCall.id == tool_call_id)
                .values(status=status, result=result, completed_at=func.now())
            )

    async def record_approval(
        self, *, tool_call_id: uuid.UUID, tool_id: str, summary: str
    ) -> uuid.UUID:
        """Create a pending approval record; call `set_approval_decision` once resolved."""
        approval = Approval(
            id=uuid.uuid4(), tool_call_id=tool_call_id, tool_id=tool_id, summary=summary
        )
        async with self._session.begin() as session:
            session.add(approval)
        return approval.id

    async def set_approval_decision(
        self, approval_id: uuid.UUID, *, decision: str, decided_by: str | None = None
    ) -> None:
        async with self._session.begin() as session:
            await session.execute(
                update(Approval)
                .where(Approval.id == approval_id)
                .values(decision=decision, decided_by=decided_by, decided_at=func.now())
            )

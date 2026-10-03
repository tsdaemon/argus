"""Argus-owned A2A task persistence and context-to-thread mapping."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from a2a.server.context import ServerCallContext
from a2a.server.tasks import TaskStore
from a2a.types.a2a_pb2 import ListTasksRequest, ListTasksResponse, Task, TaskState
from a2a.utils.errors import InvalidParamsError
from a2a.utils.task import decode_page_token, encode_page_token
from google.protobuf.json_format import MessageToDict, ParseDict
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from argus.db.models import A2AContextRow, A2ATaskRow


class A2ARepository(Protocol):
    async def create_context(self, context_id: str, owner: UUID, thread_id: UUID) -> None: ...
    async def thread(self, context_id: str, owner: UUID) -> UUID | None: ...
    async def rotate_context(
        self, context_id: str, owner: UUID, old_thread: UUID, new_thread: UUID
    ) -> bool: ...
    async def save(self, task: Task, context: ServerCallContext) -> None: ...
    async def get(self, task_id: str, context: ServerCallContext) -> Task | None: ...
    async def list(
        self, params: ListTasksRequest, context: ServerCallContext
    ) -> ListTasksResponse: ...
    async def delete(self, task_id: str, context: ServerCallContext) -> None: ...
    async def recover(self) -> None: ...


def owner(context: ServerCallContext) -> UUID:
    return UUID(context.state["token_id"])


def task_page(tasks: list[Task], params: ListTasksRequest) -> ListTasksResponse:
    # Personal, small task catalog: filter before paginating; ordering is stable on ties.
    tasks = [
        task
        for task in tasks
        if (not params.context_id or task.context_id == params.context_id)
        and (not params.status or task.status.state == params.status)
        and (
            not params.HasField("status_timestamp_after")
            or (task.status.timestamp.seconds, task.status.timestamp.nanos)
            >= (params.status_timestamp_after.seconds, params.status_timestamp_after.nanos)
        )
    ]
    tasks.sort(
        key=lambda t: (t.status.timestamp.seconds, t.status.timestamp.nanos, t.id), reverse=True
    )
    start = 0
    if params.page_token:
        task_id = decode_page_token(params.page_token)
        try:
            start = next(i for i, task in enumerate(tasks) if task.id == task_id)
        except StopIteration:
            raise InvalidParamsError(message="Invalid page token.") from None
    size = params.page_size or 50
    end = start + size
    return ListTasksResponse(
        tasks=tasks[start:end],
        total_size=len(tasks),
        page_size=size,
        next_page_token=encode_page_token(tasks[end].id) if end < len(tasks) else "",
    )


def interrupted(task: Task) -> bool:
    if task.status.state not in (TaskState.TASK_STATE_SUBMITTED, TaskState.TASK_STATE_WORKING):
        return False
    task.status.state = TaskState.TASK_STATE_FAILED
    task.status.timestamp.GetCurrentTime()
    task.status.message.parts.add(
        text="Argus restarted before this task finished. Start a new task "
        "in this context to inspect its effects and continue."
    )
    return True


class SqlA2A(TaskStore):
    def __init__(self, engine: AsyncEngine):
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def create_context(self, context_id: str, owner: UUID, thread_id: UUID) -> None:
        try:
            async with self._session.begin() as session:
                session.add(A2AContextRow(id=context_id, token_id=owner, thread_id=thread_id))
        except IntegrityError:
            # context_id is globally unique; never surface the DB error to the caller.
            raise InvalidParamsError(message="Context ID is unavailable.") from None

    async def rotate_context(
        self, context_id: str, owner: UUID, old_thread: UUID, new_thread: UUID
    ) -> bool:
        """Point the context at new_thread iff it still points at old_thread (atomic)."""
        async with self._session.begin() as session:
            result = await session.execute(
                update(A2AContextRow)
                .where(
                    A2AContextRow.id == context_id,
                    A2AContextRow.token_id == owner,
                    A2AContextRow.thread_id == old_thread,
                )
                .values(thread_id=new_thread)
            )
            return result.rowcount == 1

    async def thread(self, context_id: str, owner: UUID) -> UUID | None:
        async with self._session() as session:
            return await session.scalar(
                select(A2AContextRow.thread_id).where(
                    A2AContextRow.id == context_id, A2AContextRow.token_id == owner
                )
            )

    def _owned(self, owner_id: UUID):
        return select(A2ATaskRow).join(A2AContextRow).where(A2AContextRow.token_id == owner_id)

    async def save(self, task: Task, context: ServerCallContext) -> None:
        if await self.thread(task.context_id, owner(context)) is None:
            raise InvalidParamsError(message="Unknown context.")
        async with self._session.begin() as session:
            statement = insert(A2ATaskRow).values(
                id=task.id, context_id=task.context_id, payload=MessageToDict(task)
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[A2ATaskRow.id],
                    set_={"payload": statement.excluded.payload},
                    where=A2ATaskRow.context_id == task.context_id,
                )
            )

    async def get(self, task_id: str, context: ServerCallContext) -> Task | None:
        async with self._session() as session:
            row = await session.scalar(self._owned(owner(context)).where(A2ATaskRow.id == task_id))
            return ParseDict(row.payload, Task()) if row else None

    async def list(self, params: ListTasksRequest, context: ServerCallContext) -> ListTasksResponse:
        async with self._session() as session:
            rows = await session.scalars(self._owned(owner(context)))
            return task_page([ParseDict(row.payload, Task()) for row in rows], params)

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        async with self._session.begin() as session:
            owned_ids = self._owned(owner(context)).with_only_columns(A2ATaskRow.id)
            await session.execute(
                delete(A2ATaskRow).where(A2ATaskRow.id == task_id, A2ATaskRow.id.in_(owned_ids))
            )

    async def recover(self) -> None:
        # One server process: after restart no previous producer can still be running.
        async with self._session.begin() as session:
            rows = await session.scalars(select(A2ATaskRow))
            for row in rows:
                task = ParseDict(row.payload, Task())
                if interrupted(task):
                    await session.execute(
                        update(A2ATaskRow)
                        .where(A2ATaskRow.id == row.id)
                        .values(payload=MessageToDict(task))
                    )

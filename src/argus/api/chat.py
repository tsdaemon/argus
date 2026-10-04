"""AG-UI streaming, conversation catalog, and read-only checkpoint replay."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from ag_ui.core import (
    EventType,
    MessagesSnapshotEvent,
    RunAgentInput,
    RunFinishedEvent,
    RunStartedEvent,
)
from ag_ui.encoder import EventEncoder
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from argus.agent.api import ArgusAgent
from argus.agent.history import archive_messages
from argus.agent.identity import AUTHOR_KEY
from argus.agent.titles import MAX_LENGTH, TitleWriter
from argus.db.history import TIME_KEY, HistoryRepository


class Rename(BaseModel):
    title: str = Field(min_length=1, max_length=MAX_LENGTH)


def message_text(content) -> str | None:
    """A message's text: the string itself, or its text parts beside attachments."""
    if isinstance(content, list):
        content = " ".join(part.text for part in content if getattr(part, "type", None) == "text")
    return " ".join(content.split()) if isinstance(content, str) else None


def add_chat_routes(
    app: FastAPI,
    agent: ArgusAgent,
    history: HistoryRepository | None,
    titles: TitleWriter | None = None,
) -> None:
    # Threads with a run stream open in this process. A checkpoint that stopped partway is
    # only stalled when its thread is not here; after a restart this set is empty.
    running: set[str] = set()

    def require_history() -> HistoryRepository:
        if history is None:
            raise HTTPException(503, "Chat history requires a configured history store.")
        return history

    @app.post("/agent")
    async def run(input_data: RunAgentInput, request: Request):
        # Browser input belongs to the operator. Caller-supplied author labels are ignored.
        for message in input_data.messages:
            if message.role == "user":
                message.name = None
                if message.metadata:
                    message.metadata = {
                        k: v for k, v in message.metadata.items() if k != AUTHOR_KEY
                    }
        thread_id = None
        if history is not None:
            try:
                thread_id = UUID(input_data.thread_id)
            except ValueError:
                raise HTTPException(422, "threadId must be a UUID.") from None
            input_data.thread_id = str(thread_id)
            first_user = next((m for m in input_data.messages if m.role == "user"), None)
            text = message_text(first_user.content) if first_user else None
            await history.touch_thread(thread_id, text[:MAX_LENGTH] if text else None)
            # Archive the operator's message now, so it is stamped when it was sent.
            await archive_messages(history, thread_id, input_data.messages)

        encoder = EventEncoder(accept=request.headers.get("accept"))
        request_agent = agent.clone()

        async def events():
            running.add(input_data.thread_id)
            try:
                async for event in run_events():
                    yield event
            finally:
                running.discard(input_data.thread_id)
                if titles is not None and thread_id is not None:
                    titles.start(thread_id)  # after the run, so it can read the reply

        async def run_events():
            async for event in request_agent.run(input_data):
                if history is not None and event.type == EventType.MESSAGES_SNAPSHOT:
                    archived = await archive_messages(history, thread_id, event.messages)
                    # Preserve the transcript through working-context summarization.
                    event = MessagesSnapshotEvent.model_validate(
                        {**event.model_dump(), "messages": archived}
                    )
                yield encoder.encode(event)

        return StreamingResponse(events(), media_type=encoder.get_content_type())

    @app.get("/api/threads")
    async def threads(
        limit: int = Query(100, ge=1, le=100),
        offset: int = Query(0, ge=0),
        origin: Literal["human", "a2a", "all"] = "human",
        source: str | None = None,
    ):
        if source is not None:
            raise HTTPException(
                422, "The `source` filter was replaced by `origin` (human or a2a)."
            )
        return await require_history().list_threads(
            limit=limit, offset=offset, origin=None if origin == "all" else origin
        )

    @app.get("/api/costs")
    async def costs():
        return {"total_usd": await require_history().total_cost()}

    @app.post("/api/threads", status_code=201)
    async def new_thread():
        store = require_history()
        return await store.get_thread(await store.create_thread())

    @app.get("/api/threads/{thread_id}")
    async def thread(thread_id: UUID):
        result = await require_history().get_thread(thread_id)
        if result is None:
            raise HTTPException(404, "Conversation not found.")
        return result

    @app.patch("/api/threads/{thread_id}")
    async def rename_thread(thread_id: UUID, body: Rename):
        store = require_history()
        title = " ".join(body.title.split())
        if not title:
            raise HTTPException(422, "A title needs some text.")
        if not await store.rename_thread(thread_id, title):
            raise HTTPException(404, "Conversation not found.")
        return await store.get_thread(thread_id)

    @app.delete("/api/threads/{thread_id}", status_code=204)
    async def delete_thread(thread_id: UUID):
        if not await require_history().delete_thread(thread_id):
            raise HTTPException(404, "Conversation not found.")
        await agent.delete_thread_state(str(thread_id))

    @app.get("/api/threads/{thread_id}/run-state")
    async def run_state(thread_id: UUID):
        if str(thread_id) in running:
            return {"stalled": False, "pending": []}
        return await agent.run_state(str(thread_id))

    @app.get("/api/threads/{thread_id}/connect")
    async def connect(thread_id: UUID, request: Request, run_id: str = "replay"):
        store = require_history()
        if await store.get_thread(thread_id) is None:
            raise HTTPException(404, "Conversation not found.")
        current, interrupts = await agent.thread_snapshot(str(thread_id))
        # Preserve chat messages that are no longer in the model's summarized context.
        archived = await store.chat_messages(thread_id)
        messages = {m["id"]: m for m in archived}
        for message in current:
            # The checkpoint's copy is current, but only the archive knows when it was sent.
            if at := (messages.get(message.id, {}).get("metadata") or {}).get(TIME_KEY):
                message.metadata = {**(message.metadata or {}), TIME_KEY: at}
            messages[message.id] = message
        encoder = EventEncoder(accept=request.headers.get("accept"))
        events = [
            RunStartedEvent(type=EventType.RUN_STARTED, thread_id=str(thread_id), run_id=run_id),
            MessagesSnapshotEvent(
                type=EventType.MESSAGES_SNAPSHOT, messages=list(messages.values())
            ),
            RunFinishedEvent(
                type=EventType.RUN_FINISHED,
                thread_id=str(thread_id),
                run_id=run_id,
                outcome={"type": "interrupt", "interrupts": interrupts} if interrupts else None,
            ),
        ]
        return StreamingResponse(
            iter(encoder.encode(event) for event in events), media_type=encoder.get_content_type()
        )

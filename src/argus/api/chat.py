"""AG-UI streaming, conversation catalog, and read-only checkpoint replay."""

from __future__ import annotations

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

from argus.agent.api import ArgusAgent
from argus.db.history import HistoryRepository


def add_chat_routes(app: FastAPI, agent: ArgusAgent, history: HistoryRepository | None) -> None:
    def require_history() -> HistoryRepository:
        if history is None:
            raise HTTPException(503, "Chat history requires a configured history store.")
        return history

    @app.post("/agent")
    async def run(input_data: RunAgentInput, request: Request):
        thread_id = None
        if history is not None:
            try:
                thread_id = UUID(input_data.thread_id)
            except ValueError:
                raise HTTPException(422, "threadId must be a UUID.") from None
            input_data.thread_id = str(thread_id)
            first_user = next((m for m in input_data.messages if m.role == "user"), None)
            content = first_user.content if first_user else None
            title = " ".join(content.split())[:100] if isinstance(content, str) else None
            await history.touch_thread(thread_id, title or None)

        encoder = EventEncoder(accept=request.headers.get("accept"))
        request_agent = agent.clone()

        async def events():
            async for event in request_agent.run(input_data):
                if history is not None and event.type == EventType.MESSAGES_SNAPSHOT:
                    await history.save_chat_messages(
                        thread_id,
                        [
                            m.model_dump(mode="json", by_alias=True, exclude_none=True)
                            for m in event.messages
                        ],
                    )
                    # Keep the visible transcript intact during subsequent runs,
                    # too, when the graph's working context has been summarized.
                    event = MessagesSnapshotEvent.model_validate(
                        {
                            **event.model_dump(),
                            "messages": await history.chat_messages(thread_id),
                        }
                    )
                yield encoder.encode(event)

        return StreamingResponse(events(), media_type=encoder.get_content_type())

    @app.get("/api/threads")
    async def threads(limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0)):
        return await require_history().list_threads(limit=limit, offset=offset)

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

    @app.delete("/api/threads/{thread_id}", status_code=204)
    async def delete_thread(thread_id: UUID):
        if not await require_history().delete_thread(thread_id):
            raise HTTPException(404, "Conversation not found.")
        await agent.delete_thread_state(str(thread_id))

    @app.get("/api/threads/{thread_id}/connect")
    async def connect(thread_id: UUID, request: Request, run_id: str = "replay"):
        store = require_history()
        if await store.get_thread(thread_id) is None:
            raise HTTPException(404, "Conversation not found.")
        current, interrupts = await agent.thread_snapshot(str(thread_id))
        # Preserve chat messages that are no longer in the model's summarized context.
        archived = await store.chat_messages(thread_id)
        messages = {m["id"]: m for m in archived}
        messages.update({m.id: m for m in current})
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

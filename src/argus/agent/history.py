"""Archive snapshots consistently across the human and A2A interfaces."""

from uuid import UUID

from argus.db.history import HistoryRepository


async def archive_messages(history: HistoryRepository, thread_id: UUID, messages: list) -> list:
    await history.save_chat_messages(
        thread_id, [m.model_dump(mode="json", by_alias=True, exclude_none=True) for m in messages]
    )
    return await history.chat_messages(thread_id)

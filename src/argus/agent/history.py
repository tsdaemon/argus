"""Archive snapshots consistently across the human and A2A interfaces."""

from uuid import UUID

from ag_ui_langgraph.utils import langchain_messages_to_agui

from argus.agent.identity import AUTHOR_KEY
from argus.db.history import HistoryRepository


async def archive_messages(history: HistoryRepository, thread_id: UUID, messages: list) -> list:
    previous = {m["id"]: m for m in await history.chat_messages(thread_id)}
    rows = []
    for message in messages:
        row = message.model_dump(mode="json", by_alias=True, exclude_none=True)
        author = (previous.get(row["id"], {}).get("metadata") or {}).get(AUTHOR_KEY)
        if author and AUTHOR_KEY not in row.get("metadata", {}):
            row["metadata"] = {**row.get("metadata", {}), AUTHOR_KEY: author}
        rows.append(row)
    await history.save_chat_messages(thread_id, rows)
    return await history.chat_messages(thread_id)


def messages_to_agui(messages: list) -> list:
    converted = langchain_messages_to_agui(messages)
    authors = {
        str(m.id): m.additional_kwargs[AUTHOR_KEY]
        for m in messages
        if AUTHOR_KEY in m.additional_kwargs
    }
    for message in converted:
        if message.id in authors:
            message.metadata = {**(message.metadata or {}), AUTHOR_KEY: authors[message.id]}
    return converted

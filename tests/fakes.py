"""In-memory stand-ins so no test needs a database."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime


class InMemoryHistory:
    """A `HistoryRepository` with the same semantics as `SqlHistory`.

    `tests/db/test_history_contract.py` runs one suite against both to keep them aligned.
    """

    def __init__(self) -> None:
        self._threads: dict[uuid.UUID, dict] = {}
        self._messages: dict[uuid.UUID, dict[str, dict]] = {}

    async def create_thread(self, *, title: str | None = None) -> uuid.UUID:
        thread_id = uuid.uuid4()
        self._add(thread_id, title)
        return thread_id

    async def touch_thread(self, thread_id: uuid.UUID, title: str | None) -> None:
        if thread_id not in self._threads:
            self._add(thread_id, title)
            return
        thread = self._threads[thread_id]
        thread["updated_at"] = datetime.now(UTC)
        thread["title"] = thread["title"] or title

    async def list_threads(self, *, limit: int = 100, offset: int = 0) -> list[dict]:
        rows = sorted(
            self._threads.values(), key=lambda t: (t["updated_at"], t["id"]), reverse=True
        )
        return [dict(row) for row in rows[offset : offset + limit]]

    async def get_thread(self, thread_id: uuid.UUID) -> dict | None:
        thread = self._threads.get(thread_id)
        return dict(thread) if thread else None

    async def delete_thread(self, thread_id: uuid.UUID) -> bool:
        self._messages.pop(thread_id, None)
        return self._threads.pop(thread_id, None) is not None

    async def save_chat_messages(self, thread_id: uuid.UUID, messages: list) -> None:
        stored = self._messages.setdefault(thread_id, {})
        for message in messages:
            stored[message["id"]] = message  # an update keeps the original position

    async def chat_messages(self, thread_id: uuid.UUID) -> list:
        return list(self._messages.get(thread_id, {}).values())

    def _add(self, thread_id: uuid.UUID, title: str | None) -> None:
        now = datetime.now(UTC)
        self._threads[thread_id] = {
            "id": thread_id,
            "title": title,
            "created_at": now,
            "updated_at": now,
        }

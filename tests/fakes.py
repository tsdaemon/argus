"""In-memory stand-ins so no test needs a database."""

from __future__ import annotations

import asyncio
import secrets
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from a2a.server.tasks import TaskStore
from a2a.types.a2a_pb2 import Task
from a2a.utils.errors import InvalidParamsError
from google.protobuf.json_format import MessageToDict, ParseDict

from argus.db.a2a import interrupted, owner, task_page
from argus.db.admin import AdminAccount
from argus.db.breakglass import PENDING, BreakGlassRequest
from argus.db.history import with_time
from argus.db.tokens import ApiToken, now


class InMemoryHistory:
    """A `HistoryRepository` with the same semantics as `SqlHistory`.

    `tests/db/test_history_contract.py` runs one suite against both to keep them aligned.
    """

    def __init__(self) -> None:
        self._threads: dict[uuid.UUID, dict] = {}
        self._messages: dict[uuid.UUID, dict[str, dict]] = {}

    async def create_thread(
        self, *, title: str | None = None, origin: str = "human"
    ) -> uuid.UUID:
        thread_id = uuid.uuid4()
        self._add(thread_id, title, origin)
        return thread_id

    async def touch_thread(self, thread_id: uuid.UUID, title: str | None) -> None:
        if thread_id not in self._threads:
            self._add(thread_id, title)
            return
        thread = self._threads[thread_id]
        thread["updated_at"] = datetime.now(UTC)
        thread["title"] = thread["title"] or title

    async def list_threads(
        self, *, limit: int = 100, offset: int = 0, origin: str | None = None
    ) -> list[dict]:
        rows = sorted(
            self._threads.values(), key=lambda t: (t["updated_at"], t["id"]), reverse=True
        )
        rows = [self._with_author(row) for row in rows]
        if origin is not None:
            rows = [row for row in rows if row["origin"] == origin]
        return rows[offset : offset + limit]

    async def get_thread(self, thread_id: uuid.UUID) -> dict | None:
        thread = self._threads.get(thread_id)
        return self._with_author(thread) if thread else None

    def _with_author(self, thread):
        first = next(
            (m for m, _ in self._messages.get(thread["id"], {}).values() if m["role"] == "user"),
            {},
        )
        author = (first.get("metadata") or {}).get("argus_author")
        external = bool(author and author.get("kind") == "agent")
        return {
            **thread,
            "author": author if external else None,
        }

    async def rename_thread(self, thread_id: uuid.UUID, title: str) -> bool:
        if thread_id not in self._threads:
            return False
        self._threads[thread_id] |= {"title": title, "title_source": "user"}
        return True

    async def set_generated_title(self, thread_id: uuid.UUID, title: str) -> bool:
        thread = self._threads.get(thread_id)
        if thread is None or thread["title_source"] is not None:
            return False
        thread |= {"title": title, "title_source": "generated"}
        return True

    async def delete_thread(self, thread_id: uuid.UUID) -> bool:
        self._messages.pop(thread_id, None)
        return self._threads.pop(thread_id, None) is not None

    async def save_chat_messages(self, thread_id: uuid.UUID, messages: list) -> None:
        stored = self._messages.setdefault(thread_id, {})
        for message in messages:
            # An update keeps the original position and time.
            at = stored[message["id"]][1] if message["id"] in stored else datetime.now(UTC)
            stored[message["id"]] = (message, at)

    async def chat_messages(self, thread_id: uuid.UUID) -> list:
        return [with_time(m, at) for m, at in self._messages.get(thread_id, {}).values()]

    async def add_cost(self, thread_id: uuid.UUID, usd: float) -> None:
        if thread_id in self._threads:
            self._threads[thread_id]["cost_usd"] += usd

    async def total_cost(self) -> float:
        return sum(thread["cost_usd"] for thread in self._threads.values())

    def _add(self, thread_id: uuid.UUID, title: str | None, origin: str = "human") -> None:
        now = datetime.now(UTC)
        self._threads[thread_id] = {
            "id": thread_id,
            "title": title,
            "title_source": None,
            "origin": origin,
            "created_at": now,
            "updated_at": now,
            "cost_usd": 0.0,
        }


class InMemoryBreakGlass:
    """A `BreakGlassRepository` with the same semantics as `SqlBreakGlass`.

    `tests/db/test_breakglass_contract.py` runs one suite against both to keep them aligned.
    """

    def __init__(self) -> None:
        self._requests: dict[str, BreakGlassRequest] = {}

    async def create_request(
        self, *, reason: str, target_host: str, evidence: str, proposed_objective: str
    ) -> BreakGlassRequest:
        request = BreakGlassRequest(
            id=secrets.token_hex(4),
            created_at=datetime.now(UTC).isoformat(),
            reason=reason,
            target_host=target_host,
            evidence=evidence,
            proposed_objective=proposed_objective,
            status=PENDING,
        )
        self._requests[request.id] = request
        return request

    async def list_requests(self, *, status: str | None = None) -> list[BreakGlassRequest]:
        rows = sorted(self._requests.values(), key=lambda r: (r.created_at, r.id), reverse=True)
        return [r for r in rows if status is None or r.status == status]

    async def get_request(self, request_id: str) -> BreakGlassRequest | None:
        return self._requests.get(request_id)

    async def set_request_status(self, request_id: str, status: str) -> BreakGlassRequest:
        if request_id not in self._requests:
            raise KeyError(f"No break-glass request with id '{request_id}'")
        self._requests[request_id] = replace(self._requests[request_id], status=status)
        return self._requests[request_id]


class InMemoryAdmin:
    """An `AdminRepository` with the same semantics as `SqlAdmin`."""

    def __init__(self) -> None:
        self._admin: AdminAccount | None = None

    async def get_admin(self) -> AdminAccount | None:
        return self._admin

    async def create_admin_if_absent(self, account: AdminAccount) -> bool:
        if self._admin is not None:
            return False
        self._admin = account
        return True


# Repository fake used by API tests and the shared repository contract suite.
class InMemoryTokens:
    def __init__(self):
        self.tokens: dict[UUID, ApiToken] = {}

    async def create(self, token: ApiToken) -> None:
        self.tokens[token.id] = token

    async def get(self, token_id: UUID) -> ApiToken | None:
        return self.tokens.get(token_id)

    async def list(self) -> list[ApiToken]:
        return sorted(self.tokens.values(), key=lambda t: t.created_at, reverse=True)

    async def revoke(self, token_id: UUID) -> bool:
        if token_id not in self.tokens:
            return False
        self.tokens[token_id] = replace(self.tokens[token_id], revoked_at=now())
        return True

    async def used(self, token_id: UUID) -> None:
        self.tokens[token_id] = replace(self.tokens[token_id], last_used_at=now())


class InMemoryA2A(TaskStore):
    def __init__(self):
        self.contexts = {}
        self.tasks = {}

    async def create_context(self, context_id, owner, thread_id):
        if context_id in self.contexts:
            raise InvalidParamsError(message="Context ID is unavailable.")
        self.contexts[context_id] = (owner, thread_id)

    async def rotate_context(self, context_id, owner, old_thread, new_thread):
        if self.contexts.get(context_id) != (owner, old_thread):
            return False
        await asyncio.sleep(0)  # interleaving point: a non-atomic CAS would let two rotate
        if self.contexts.get(context_id) != (owner, old_thread):
            return False
        self.contexts[context_id] = (owner, new_thread)
        return True

    async def thread(self, context_id, owner):
        entry = self.contexts.get(context_id)
        return entry[1] if entry and entry[0] == owner else None

    async def save(self, task, context):
        if await self.thread(task.context_id, owner(context)) is None:
            raise InvalidParamsError(message="Unknown context.")
        existing = self.tasks.get(task.id)
        if existing and existing["contextId"] != task.context_id:
            return
        self.tasks[task.id] = MessageToDict(task)

    async def get(self, task_id, context):
        data = self.tasks.get(task_id)
        if data is None or await self.thread(data["contextId"], owner(context)) is None:
            return None
        return ParseDict(data, Task())

    async def list(self, params, context):
        tasks = [await self.get(task_id, context) for task_id in self.tasks]
        return task_page([task for task in tasks if task is not None], params)

    async def delete(self, task_id, context):
        if await self.get(task_id, context) is not None:
            self.tasks.pop(task_id)

    async def recover(self):
        for task_id, payload in self.tasks.items():
            task = ParseDict(payload, Task())
            if interrupted(task):
                self.tasks[task_id] = MessageToDict(task)

"""Short conversation titles, written by a model in the background.

A thread starts with its first message's opening as a placeholder title. Once a run has
produced a reply, `TitleWriter` names the topic from the first exchange and replaces the
placeholder once; an operator's own title is never replaced. Failures only leave the
placeholder, so a title can never break a run.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import Runnable

from argus.db.history import HistoryRepository

logger = logging.getLogger(__name__)

PROMPT = """\
Name the topic of a conversation between an operator and argus, an assistant for their home \
infrastructure, from its first exchange below. Write a label for a conversation list, not a \
summary of the request: a noun phrase of two to five words naming the system and the subject \
or finding, e.g. "Theseus disk health", "Router DNS outage", "Jellyfin restart loop". Do not \
start with a verb and do not repeat the operator's wording. Write the label in the language \
the operator wrote in (a Ukrainian message gets a Ukrainian label), keeping names as they are. \
Reply with the label only, without quotes or a final period."""
MAX_LENGTH = 100
_EXCERPT = 1500  # characters of each side the model sees


def clean(text: str) -> str:
    title = " ".join(text.split()).strip("\"'`*#«»“” ").removesuffix(".")
    return title[:MAX_LENGTH]


def _text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, list):
        content = " ".join(p.get("text", "") for p in content if p.get("type") == "text")
    return " ".join(content.split()) if isinstance(content, str) else ""


class TitleWriter:
    def __init__(self, model: Runnable, history: HistoryRepository) -> None:
        self._model = model
        self._history = history
        self._pending: dict[UUID, asyncio.Task] = {}  # also keeps the tasks referenced

    def start(self, thread_id: UUID) -> None:
        if thread_id not in self._pending:
            task = asyncio.create_task(self._write(thread_id))
            self._pending[thread_id] = task
            task.add_done_callback(lambda _: self._pending.pop(thread_id, None))

    async def _write(self, thread_id: UUID) -> None:
        try:
            thread = await self._history.get_thread(thread_id)
            if not thread or thread["title_source"] is not None:
                return
            messages = await self._history.chat_messages(thread_id)
            question = next((t for m in messages if m["role"] == "user" and (t := _text(m))), "")
            answer = next(
                (t for m in messages if m["role"] == "assistant" and (t := _text(m))), ""
            )
            if not question or not answer:
                return  # e.g. a run that stopped at an approval; the next run tries again
            response = await self._model.ainvoke(
                [
                    SystemMessage(PROMPT),
                    HumanMessage(
                        f"Operator:\n{question[:_EXCERPT]}\n\nargus:\n{answer[:_EXCERPT]}"
                    ),
                ],
                # CostRecorder charges the call to this thread.
                config={"metadata": {"thread_id": str(thread_id)}},
            )
            if title := clean(response.text):
                await self._history.set_generated_title(thread_id, title)
        except Exception:
            logger.warning("Could not title thread %s", thread_id, exc_info=True)

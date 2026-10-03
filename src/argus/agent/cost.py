"""Per-thread model spend, from the cost the gateway reports on each call.

OpenRouter returns what a call actually cost as `usage.cost` (`argus.agent.model` keeps it on
streamed messages). `CostRecorder` is attached to the agent's chat models, so it sees every
call they make: the planner, the worker, and summarization. It reads the thread from the run
metadata LangGraph fills in from `configurable.thread_id` and adds the cost to that thread.

Recording is never on the correctness path: a failure is logged and the run goes on.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult

logger = logging.getLogger(__name__)


def cost_in(usage: Any) -> float | None:
    """`usage["cost"]` when it is a finite, non-negative number."""
    cost = usage.get("cost") if isinstance(usage, Mapping) else None
    if isinstance(cost, bool) or not isinstance(cost, int | float):
        return None
    return float(cost) if math.isfinite(cost) and cost >= 0 else None


def result_cost(result: LLMResult) -> float | None:
    """The reported cost of one model call: `llm_output` for a plain call, the message's
    `response_metadata` for a streamed one."""
    if (cost := cost_in((result.llm_output or {}).get("token_usage"))) is not None:
        return cost
    for generations in result.generations:
        for generation in generations:
            message = getattr(generation, "message", None)
            metadata = getattr(message, "response_metadata", None) or {}
            if (cost := cost_in(metadata.get("token_usage"))) is not None:
                return cost
    return None


class CostRecorder(AsyncCallbackHandler):
    def __init__(self, record: Callable[[UUID, float], Awaitable[None]]) -> None:
        self._record = record
        self._threads: dict[UUID, UUID] = {}  # model call run id -> thread

    def _start(self, run_id: UUID, metadata: dict[str, Any] | None) -> None:
        try:
            self._threads[run_id] = UUID(str((metadata or {})["thread_id"]))
        except (KeyError, ValueError):
            pass  # not a thread run, e.g. a test or a non-UUID AG-UI thread

    async def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **_):
        self._start(run_id, metadata)

    async def on_llm_start(self, serialized, prompts, *, run_id, metadata=None, **_):
        self._start(run_id, metadata)

    async def on_llm_error(self, error, *, run_id, **_):
        self._threads.pop(run_id, None)

    async def on_llm_end(self, response: LLMResult, *, run_id, **_):
        thread_id = self._threads.pop(run_id, None)
        cost = result_cost(response)
        if thread_id is None or not cost:
            return
        try:
            await self._record(thread_id, cost)
        except Exception:
            logger.warning("Could not record $%s on thread %s", cost, thread_id, exc_info=True)

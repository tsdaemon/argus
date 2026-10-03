"""Official A2A JSON-RPC transport around Argus's unattended graph."""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from a2a.server.agent_execution import AgentExecutor, RequestContext, SimpleRequestContextBuilder
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers.default_request_handler_v2 import DefaultRequestHandlerV2
from a2a.server.request_handlers.response_helpers import agent_card_to_dict
from a2a.server.routes.common import ServerCallContextBuilder
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.types import a2a_pb2 as p
from a2a.utils.errors import InvalidParamsError, TaskNotFoundError
from fastapi import FastAPI
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from argus.agent.history import archive_messages, messages_to_agui
from argus.agent.identity import AUTHOR_KEY
from argus.db.a2a import A2ARepository, owner

logger = logging.getLogger(__name__)


class TokenContextBuilder(ServerCallContextBuilder):
    def build(self, request):
        # Authentication middleware verified the credential before parsing any protocol data.
        return ServerCallContext(
            state={
                "token_id": str(request.state.api_token.id),
                "sender": {"kind": "agent", "name": request.state.api_token.name,
                           "token_id": str(request.state.api_token.id), "interface": "a2a"},
                "headers": {"a2a-version": request.headers.get("a2a-version") or "1.0"},
            }
        )


class OwnedContextBuilder(SimpleRequestContextBuilder):
    def __init__(self, store: A2ARepository, history):
        super().__init__(task_store=store)
        self.store = store
        self.history = history

    async def build(self, context, params=None, task_id=None, context_id=None, task=None):
        existing = await self.store.get(task_id, context) if task_id else None
        if existing:
            if context_id and context_id != existing.context_id:
                raise InvalidParamsError(message="Context does not match task.")
            context_id = existing.context_id
        if params:
            if params.message.role != p.Role.ROLE_USER or not params.message.parts:
                raise InvalidParamsError(message="Send a user message with text parts.")
            if any(part.WhichOneof("content") != "text" for part in params.message.parts):
                raise InvalidParamsError(message="Only text input is supported.")
            if not any(part.text.strip() for part in params.message.parts):
                raise InvalidParamsError(message="Message must contain text.")
            if params.metadata or params.message.metadata:
                raise InvalidParamsError(message="Caller execution metadata is not supported.")
        result = await super().build(context, params, task_id, context_id, task)
        if result.context_id and await self.store.thread(result.context_id, owner(context)) is None:
            thread_id = await self.history.create_thread()
            await self.store.create_context(result.context_id, owner(context), thread_id)
        return result


class OwnedRequestHandler(DefaultRequestHandlerV2):
    # The SDK active-task registry is process-local and keyed by task ID. Check ownership
    # before accessing it, even though the persistent store already filters by owner.
    async def on_cancel_task(self, params, context):
        if await self.task_store.get(params.id, context) is None:
            raise TaskNotFoundError()
        return await super().on_cancel_task(params, context)

    async def on_subscribe_to_task(self, params, context):
        if await self.task_store.get(params.id, context) is None:
            raise TaskNotFoundError()
        async for event in super().on_subscribe_to_task(params, context):
            yield event


class ArgusExecutor(AgentExecutor):
    def __init__(self, graph, store, history, recursion_limit):
        self.graph = graph
        self.store = store
        self.history = history
        self.recursion_limit = recursion_limit
        self.active: set[asyncio.Task] = set()
        self.busy: set[str] = set()

    async def execute(self, context: RequestContext, event_queue):
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        if context.current_task is None:
            initial = p.Task(
                id=context.task_id,
                context_id=context.context_id,
                status=p.TaskStatus(state=p.TaskState.TASK_STATE_SUBMITTED),
                history=[context.message] if context.message else [],
            )
            initial.status.timestamp.GetCurrentTime()
            await event_queue.enqueue_event(initial)
        # Different A2A tasks can share a conversation. Never run the graph concurrently
        # on the same checkpoint, and do not queue hidden work behind a long request.
        if context.context_id in self.busy:
            await updater.reject(updater.new_agent_message([p.Part(text="This context is busy.")]))
            return
        self.busy.add(context.context_id)
        current = asyncio.current_task()
        self.active.add(current)
        thread_id = await self.store.thread(context.context_id, owner(context.call_context))
        config = {
            "configurable": {"thread_id": str(thread_id),
                             "argus_sender": context.call_context.state["sender"]},
            "recursion_limit": self.recursion_limit,
        }
        try:
            await updater.start_work()
            await self.history.touch_thread(thread_id, context.get_user_input()[:100])
            snapshot = await self.graph.aget_state(config)
            messages = (snapshot.values or {}).get("messages", [])
            # A new request starts a new turn. Close unanswered calls from a cancelled or
            # crashed run rather than replaying operations whose effects are unknown.
            answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
            abandoned = [
                ToolMessage(
                    content="Previous run stopped before a result was recorded. Do not assume "
                    "the operation failed or replay it without checking its effects.",
                    tool_call_id=call["id"],
                    name=call["name"],
                    status="error",
                )
                for m in messages
                if isinstance(m, AIMessage)
                for call in m.tool_calls
                if call["id"] not in answered
            ]
            final = None
            async for state in self.graph.astream(
                {
                    "messages": [
                        *abandoned,
                        HumanMessage(
                            content=context.get_user_input(),
                            id=context.message.message_id or str(uuid4()),
                            name="a2a_" + owner(context.call_context).hex,
                            additional_kwargs={AUTHOR_KEY: context.call_context.state["sender"]},
                        ),
                    ]
                },
                config,
                stream_mode="values",
                durability="sync",
            ):
                final = state
                await archive_messages(
                    self.history, thread_id, messages_to_agui(state.get("messages", []))
                )
            if not final:
                raise RuntimeError("Agent returned no state.")
            last = next((m for m in reversed(final["messages"]) if isinstance(m, AIMessage)), None)
            text = last.text if last else "No response."
            await updater.add_artifact([p.Part(text=text)], name="response", last_chunk=True)
            await updater.complete(updater.new_agent_message([p.Part(text=text)]))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("A2A execution failed for task %s", context.task_id)
            await updater.failed(
                updater.new_agent_message(
                    [
                        p.Part(
                            text="Argus could not finish this task. Inspect its conversation or server logs."
                        )
                    ]
                )
            )
        finally:
            self.busy.discard(context.context_id)
            self.active.discard(current)

    async def cancel(self, context, event_queue):
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()

    async def close(self):
        tasks = list(self.active)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def add_a2a_routes(app: FastAPI, graph, store, history, config, recursion_limit) -> ArgusExecutor:
    card = p.AgentCard(
        name="Argus",
        description="Investigate home infrastructure through constrained tools.",
        version="0.1.0",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        supported_interfaces=[
            p.AgentInterface(url=config.url, protocol_binding="JSONRPC", protocol_version="1.0")
        ],
        capabilities=p.AgentCapabilities(streaming=True),
        security_schemes={
            "bearer": p.SecurityScheme(
                http_auth_security_scheme=p.HTTPAuthSecurityScheme(scheme="bearer")
            )
        },
        security_requirements=[p.SecurityRequirement(schemes={"bearer": p.StringList()})],
        skills=[
            p.AgentSkill(
                id="infrastructure",
                name="Infrastructure investigation",
                description="Inspect services, logs, metrics, and report findings. "
                "Actions requiring human approval are refused.",
                tags=["infrastructure", "diagnostics"],
            )
        ],
    )
    executor = ArgusExecutor(graph, store, history, recursion_limit)
    handler = OwnedRequestHandler(
        executor, store, card, request_context_builder=OwnedContextBuilder(store, history)
    )
    app.router.routes.extend(create_jsonrpc_routes(handler, "/a2a", TokenContextBuilder()))

    @app.get("/.well-known/agent-card.json", include_in_schema=False)
    async def agent_card():
        return agent_card_to_dict(card)

    return executor

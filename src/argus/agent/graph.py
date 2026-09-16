"""Builds the Argus Agent's LangGraph graph via `deepagents.create_deep_agent()`.

Returns a plain `CompiledStateGraph` — LangGraph itself stays fully visible; `deepagents`
only supplies the workspace filesystem tools, skills discovery, and `AGENTS.md` memory
loading. Provider tools (currently `docker`) are bound via `argus.agent.tools`, which
reuses `PolicyEngine.decide()`. `breakglass` is deliberately never bound here — that's a
direct human action from the UI, not an agent-invoked tool. Models go through OpenRouter
via `ChatOpenAI`, regardless of which upstream model is actually selected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends import FilesystemBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from argus.agent.tools import langchain_bind
from argus.config import AgentConfig
from argus.policy import PolicyEngine
from argus.providers.base import Provider

# Excludes `execute` (arbitrary shell) and `task` (subagent delegation) — out of scope.
_WORKSPACE_TOOLS = ["ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep"]

_DEFAULT_AGENTS_MD = (
    "# Argus Agent memory\n\n"
    "Nothing recorded yet. Use `edit_file` on this file to save durable notes, "
    "preferences, and runbooks as you learn them — this file is loaded into every run's "
    "system prompt.\n"
)

# Disables deepagents' default "general purpose subagent" (the `task` tool). Keyed to
# "openai", not "anthropic"/"openrouter": any `ChatOpenAI` instance resolves as provider
# "openai" regardless of `base_url` — see `test_build_model_resolves_as_openai_provider`.
register_harness_profile(
    "openai",
    HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)),
)


def _build_model(config: AgentConfig) -> ChatOpenAI:
    return ChatOpenAI(model=config.model, base_url=config.model_base_url, api_key=config.api_key)


def build_graph(
    *,
    config: AgentConfig,
    providers: dict[str, Provider],
    provider_settings: dict[str, dict[str, Any]],
    policy: PolicyEngine,
    checkpointer: BaseCheckpointSaver | None = None,
    model: Any = None,
) -> CompiledStateGraph:
    """Assemble the Argus Agent graph.

    `providers` is every *shared* provider to bind tools from — the caller decides which
    (never `breakglass`). `provider_settings` is keyed the same way. `model` overrides
    the OpenRouter model `config` would otherwise build, for tests.
    """
    workspace_root = Path(config.workspace_root)
    workspace_root.mkdir(parents=True, exist_ok=True)
    (workspace_root / "skills").mkdir(exist_ok=True)

    agents_md = workspace_root / "AGENTS.md"
    if not agents_md.exists():
        agents_md.write_text(_DEFAULT_AGENTS_MD)

    tools: list[Any] = []
    interrupt_on: dict[str, Any] = {}
    for name, provider in providers.items():
        provider_tools, provider_interrupt_on = langchain_bind(
            policy, provider.tool_specs(provider_settings.get(name, {}))
        )
        tools.extend(provider_tools)
        interrupt_on.update(provider_interrupt_on)

    backend = FilesystemBackend(root_dir=workspace_root)
    filesystem_middleware = FilesystemMiddleware(backend=backend, tools=_WORKSPACE_TOOLS)

    return create_deep_agent(
        model=model or _build_model(config),
        tools=tools,
        backend=backend,
        middleware=[filesystem_middleware],
        skills=["skills"],
        memory=["AGENTS.md"],
        interrupt_on=interrupt_on or None,
        checkpointer=checkpointer,
    )

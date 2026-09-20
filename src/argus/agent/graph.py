"""Builds the Argus Agent's LangGraph graph via `deepagents.create_deep_agent()`.

Returns a plain `CompiledStateGraph` — LangGraph stays fully visible; `deepagents`
supplies workspace/skills/memory and one fixed "worker" `SubAgent` (cheap
`config.worker_model`, for routine tool-calling delegation via `task`) alongside the
main agent (`config.model`, planning/self-reflection). Provider tools are bound via
`argus.agent.tools` (reuses `PolicyEngine.decide()`). `breakglass` binds only its
`request_break_glass` tool, which records a request; approving and launching are human web
routes, not agent tools. Models go through OpenRouter via
`ChatOpenAI` regardless of upstream model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    SubAgent,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends import FilesystemBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from argus.agent.model import CostReportingChatOpenAI
from argus.agent.tools import langchain_bind
from argus.config import AgentConfig
from argus.policy import PolicyEngine
from argus.providers.base import Provider

# Excludes `execute` (arbitrary shell) — out of scope.
_WORKSPACE_TOOLS = ["ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep"]

_WORKER_DESCRIPTION = (
    "Delegate routine, repetitive, or read-heavy tool-calling work here (e.g. checking "
    "several containers' status/logs) to save cost. Keep planning, synthesis, and "
    "deciding whether an action needs approval on the main agent."
)

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


def _build_model(config: AgentConfig, model_name: str) -> ChatOpenAI:
    return CostReportingChatOpenAI(
        model=model_name, base_url=config.model_base_url, api_key=config.api_key
    )


def build_graph(
    *,
    config: AgentConfig,
    providers: dict[str, Provider],
    provider_settings: dict[str, dict[str, Any]],
    policy: PolicyEngine,
    checkpointer: BaseCheckpointSaver | None = None,
    model: Any = None,
    worker_model: Any = None,
) -> CompiledStateGraph:
    """Assemble the Argus Agent graph.

    `providers` is every provider to bind tools from — the caller decides which.
    `provider_settings` is keyed the same way. `model`/
    `worker_model` override the OpenRouter models `config` would otherwise build, for
    tests.
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

    worker = SubAgent(
        name="worker",
        description=_WORKER_DESCRIPTION,
        model=worker_model or _build_model(config, config.worker_model),
    )

    return create_deep_agent(
        model=model or _build_model(config, config.model),
        tools=tools,
        backend=backend,
        middleware=[filesystem_middleware],
        skills=["skills"],
        memory=["AGENTS.md"],
        subagents=[worker],
        interrupt_on=interrupt_on or None,
        checkpointer=checkpointer,
    )

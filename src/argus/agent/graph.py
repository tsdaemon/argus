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

from argus.agent.identity import SenderIdentityMiddleware
from argus.agent.memory import memory_middleware, seed_workspace
from argus.agent.model import CostReportingChatOpenAI
from argus.agent.tools import classification_middleware, external_name, langchain_bind
from argus.agent.unattended import UnattendedMiddleware
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

# Disables deepagents' default "general purpose subagent" (the `task` tool). Keyed to
# "openai", not "anthropic"/"openrouter": any `ChatOpenAI` instance resolves as provider
# "openai" regardless of `base_url` — see `test_build_model_resolves_as_openai_provider`.
register_harness_profile(
    "openai",
    HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)),
)


def _build_model(config: AgentConfig, model_name: str) -> ChatOpenAI:
    # OpenRouter models come without a profile; deepagents derives its summarization
    # thresholds from `max_input_tokens`, and falls back to 170k tokens without it.
    return CostReportingChatOpenAI(
        model=model_name,
        base_url=config.model_base_url,
        api_key=config.api_key,
        profile={"max_input_tokens": config.context_tokens},
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
    unattended: bool = False,
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
    seed_workspace(workspace_root)

    tools: list[Any] = []
    interrupt_on: dict[str, Any] = {}
    specs = []
    for name, provider in providers.items():
        provider_specs = provider.tool_specs(provider_settings.get(name, {}))
        provider_tools, provider_interrupt_on = langchain_bind(policy, provider_specs)
        tools.extend(provider_tools)
        interrupt_on.update(provider_interrupt_on)
        specs.extend(provider_specs)
    # The worker inherits `interrupt_on`, so it needs the classifications those read.
    classifier = classification_middleware(policy, specs)
    extra_middleware = [SenderIdentityMiddleware(), *([classifier] if classifier else [])]
    if unattended:
        extra_middleware.append(
            UnattendedMiddleware(policy, {external_name(s.tool_id): s for s in specs})
        )
        interrupt_on = {}

    backend = FilesystemBackend(root_dir=workspace_root)
    filesystem_middleware = FilesystemMiddleware(backend=backend, tools=_WORKSPACE_TOOLS)

    worker = SubAgent(
        name="worker",
        description=_WORKER_DESCRIPTION,
        model=worker_model or _build_model(config, config.worker_model),
        middleware=extra_middleware,
    )

    return create_deep_agent(
        model=model or _build_model(config, config.model),
        tools=tools,
        system_prompt=(
            "This run is unattended. Operational calls requiring human approval are refused. "
            "Report findings and explain any actions that need approval. You may file a "
            "break-glass request when escalation is appropriate; you cannot approve it."
        ) if unattended else None,
        backend=backend,
        # Not `memory=`: argus's own memory rules replace deepagents' generic prompt.
        middleware=[filesystem_middleware, memory_middleware(backend), *extra_middleware],
        skills=["skills"],
        subagents=[worker],
        interrupt_on=interrupt_on or None,
        checkpointer=checkpointer,
    )

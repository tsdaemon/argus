"""Wraps the LangGraph graph (`argus.agent.graph`) into an AG-UI-compatible agent
object. This is still "building the agent" — the boundary is with `argus.api.app`,
which takes the resulting object and only ever handles serving it over HTTP (FastAPI
routing, mounting MCP, ...), never how it was built.
"""

from __future__ import annotations

from typing import Any

from ag_ui_langgraph import LangGraphAgent
from langgraph.checkpoint.base import BaseCheckpointSaver

from argus.agent.graph import build_graph
from argus.config import AgentConfig
from argus.policy import PolicyEngine
from argus.providers.base import Provider


def build_agent(
    *,
    config: AgentConfig,
    providers: dict[str, Provider],
    provider_settings: dict[str, dict[str, Any]],
    policy: PolicyEngine,
    checkpointer: BaseCheckpointSaver,
) -> LangGraphAgent:
    graph = build_graph(
        config=config,
        providers=providers,
        provider_settings=provider_settings,
        policy=policy,
        checkpointer=checkpointer,
    )
    return LangGraphAgent(name="argus-agent", graph=graph)

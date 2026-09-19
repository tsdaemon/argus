"""Argus Agent: the LangGraph-based agent implementation itself — the graph
(`argus.agent.graph`), tool binding (`argus.agent.tools`), and the AG-UI wrapper
(`argus.agent.api`). HTTP serving (`argus.api`) and persistence (`argus.db`) are
siblings, not part of this package: this one only builds the agent, never serves it or
owns its own storage.

Binds `argus.providers` `ToolSpec`s directly in-process via `PolicyEngine.decide()` — no
MCP hop. Also owns the agent's private workspace (Markdown memory + skills) via
`deepagents`' `FilesystemMiddleware`/`SkillsMiddleware`; those are deliberately never
exposed over MCP, since that would let any MCP client mutate the agent's own memory.

Shared top-level modules never import from here. `argus.api` composes the agent,
persistence, and HTTP interfaces into the single application.
"""

from __future__ import annotations

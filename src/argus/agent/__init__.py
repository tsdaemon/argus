"""Argus Agent: the LangGraph-based interactive harness.

Binds `argus.providers` `ToolSpec`s directly in-process via `PolicyEngine.decide()` (see
`argus.agent.tools`) — no MCP hop. Also owns the agent's private workspace (Markdown
memory + skills) via `deepagents`' `FilesystemMiddleware`/`SkillsMiddleware` (see
`argus.agent.graph`); those are deliberately never exposed over MCP, since that would let
any MCP client mutate the agent's own memory.

Depends on the `agent` extra (`uv sync --extra agent`) — `argus serve` must keep working
without it, so shared top-level modules never import from here, and `argus.cli` only
touches this package via a lazy import inside the relevant command.
"""

from __future__ import annotations

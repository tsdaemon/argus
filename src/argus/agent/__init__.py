"""Argus Agent: the LangGraph-based agent implementation itself — the graph
(`argus.agent.graph`), tool binding (`argus.agent.tools`), and the AG-UI wrapper
(`argus.agent.api`). HTTP serving (`argus.api`) and persistence (`argus.db`) are
siblings, not part of this package: this one only builds the agent, never serves it or
owns its own storage.

Binds `argus.providers` `ToolSpec`s directly in-process via `PolicyEngine.decide()` — no
MCP hop. Also owns the agent's private workspace (Markdown memory + skills) via
`deepagents`' `FilesystemMiddleware`/`SkillsMiddleware`; those are deliberately never
exposed over MCP, since that would let any MCP client mutate the agent's own memory.

LangGraph/deepagents/etc. are base dependencies now — not an optional extra — but shared
top-level modules still never import from here, and `argus.cli` still lazy-imports this
package inside the relevant command, so `argus serve`'s own startup stays light.
"""

from __future__ import annotations

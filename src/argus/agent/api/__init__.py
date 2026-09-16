"""The AG-UI-speaking ASGI layer: exposes the LangGraph agent to the React frontend, plus
one non-AG-UI route for break-glass (calls `argus.launcher` directly, in-process). Runs
as its own process (`argus agent serve`), separate from `argus serve` (the MCP server).
"""

from __future__ import annotations

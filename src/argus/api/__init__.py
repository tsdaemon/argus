"""The AG-UI-speaking ASGI layer: exposes the LangGraph agent to the React frontend.
`argus agent serve` runs this as one process — if `ARGUS_MCP_TOKEN` is set, it also
mounts the same `/mcp` + `/breakglass` surface `argus serve` exposes standalone, so
break-glass is just the existing `/launch` page, not a second implementation.
"""

from __future__ import annotations

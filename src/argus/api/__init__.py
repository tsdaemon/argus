"""HTTP interfaces for the Argus agent, served together by the `create_app` factory.

The app serves chat, history, and AG-UI. Setting `ARGUS_MCP_TOKEN` also mounts MCP;
configuring the breakglass provider adds its authenticated web routes to that mount.
"""

from __future__ import annotations

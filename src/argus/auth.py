"""Auth for the network-reachable MCP endpoint.

Stdio transport is trusted by construction — only the process that spawned you can talk
to you. HTTP transport is not: `/mcp` is now reachable by anything on the network, so it
needs its own check, distinct from the break-glass web view's token (different audience:
this one goes in a header from a machine — Hermes — the other goes in a URL a human
bookmarks on a phone; a leaked phone bookmark should never compromise the agent control
plane).

This is a static shared-secret check, not OAuth — appropriate for a single-operator
homelab on a private network. `fastmcp.server.auth.TokenVerifier` is the extension point
FastMCP itself provides for exactly this "bearer token, no OAuth server" case.
"""

from __future__ import annotations

import hmac

from fastmcp.server.auth import AccessToken, TokenVerifier


class StaticTokenVerifier(TokenVerifier):
    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if hmac.compare_digest(token, self._token):
            return AccessToken(token=token, client_id="argus-client", scopes=[])
        return None

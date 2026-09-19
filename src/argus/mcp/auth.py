"""Bearer authentication for the agent's optional `/mcp` interface.

MCP clients send a static shared secret in the Authorization header. Break-glass
web routes use a separate admin login and signed session cookie (`argus.webauth`).
The MCP token does not protect the chat, history, or AG-UI routes.
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

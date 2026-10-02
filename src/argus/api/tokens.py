"""Admin-only token management on the same Jinja base as login and break-glass."""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from argus.db.tokens import TokenAuth
from argus.webauth import SESSION_COOKIE, AdminAuth, verify_session
from argus.webpages import render


def add_token_routes(app: FastAPI, admin: AdminAuth, tokens: TokenAuth) -> None:
    async def csrf(request: Request) -> str:
        account = await admin.get()
        cookie = request.cookies.get(SESSION_COOKIE, "")
        if account is None or not verify_session(account.session_secret, cookie):
            raise HTTPException(401, "Admin login required.")
        return hmac.new(
            account.session_secret.encode(), cookie.encode(), hashlib.sha256
        ).hexdigest()

    async def page(request: Request, *, secret=None, error=None, status=200):
        return HTMLResponse(
            render(
                "tokens.html",
                tokens=[t.public() for t in await tokens.repository.list()],
                csrf=await csrf(request),
                secret=secret,
                error=error,
            ),
            status_code=status,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    async def form(request: Request):
        data = await request.form()
        if not hmac.compare_digest(str(data.get("csrf", "")), await csrf(request)):
            raise HTTPException(403, "Invalid form token. Reload the page and try again.")
        return data

    @app.get("/settings/tokens", include_in_schema=False)
    async def token_page(request: Request):
        return await page(request)

    @app.post("/settings/tokens", include_in_schema=False)
    async def issue(request: Request):
        data = await form(request)
        try:
            raw_expiry = str(data.get("expires_at", "")).strip()
            expiry = datetime.fromisoformat(raw_expiry) if raw_expiry else None
            _, secret = await tokens.issue(str(data.get("name", "")), expiry)
        except ValueError as exc:
            return await page(request, error=str(exc), status=422)
        return await page(request, secret=secret, status=201)

    @app.post("/settings/tokens/{token_id}/revoke", include_in_schema=False)
    async def revoke(token_id: UUID, request: Request):
        await form(request)
        if not await tokens.repository.revoke(token_id):
            raise HTTPException(404, "Token not found.")
        return RedirectResponse("/settings/tokens", status_code=303)

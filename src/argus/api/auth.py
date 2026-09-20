"""One login for the whole app.

`add_auth` adds the login and logout routes and a gate in front of everything else: the UI,
the chat API, `/agent`, and the break-glass pages. Three paths stay open: `/login` and
`/logout` themselves, `/agent/health`, and `/mcp`, which external MCP clients reach with a
bearer token instead of a browser session. The gate is a plain ASGI middleware, so streamed
responses (the AG-UI event stream) pass through untouched.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from argus.webauth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE_SECONDS,
    AdminAuth,
    sign_session,
    verify_session,
)
from argus.webpages import render

_OPEN_PATHS = {"/login", "/logout", "/agent/health"}


def _is_open(path: str) -> bool:
    return path in _OPEN_PATHS or path == "/mcp" or path.startswith("/mcp/")


def safe_next(target: str | None) -> str:
    """Only local paths, so a login link cannot bounce someone to another site."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return "/"


class RequireLogin:
    def __init__(self, app: ASGIApp, admin: AdminAuth) -> None:
        self._app = app
        self._admin = admin

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _is_open(scope["path"]):
            await self._app(scope, receive, send)
            return

        request = Request(scope)
        cookie = request.cookies.get(SESSION_COOKIE)
        account = await self._admin.get() if cookie else None
        if account is not None and verify_session(account.session_secret, cookie):
            await self._app(scope, receive, send)
            return

        wants_page = request.method == "GET" and "text/html" in request.headers.get("accept", "")
        response: Response
        if wants_page:
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            response = RedirectResponse(f"/login?next={quote(target, safe='/')}", status_code=303)
        else:
            response = JSONResponse({"detail": "Login required"}, status_code=401)
        await response(scope, receive, send)


def add_auth(app: FastAPI, admin: AdminAuth, *, initial_password: str | None = None) -> None:
    """Add the login routes and the gate. Call before mounting anything at `/`."""

    @app.get("/login", include_in_schema=False)
    async def login_page(request: Request) -> Response:
        account, generated = await admin.get_or_create(initial_password)
        error = request.query_params.get("error") == "1"
        next_path = safe_next(request.query_params.get("next"))
        return HTMLResponse(
            render(
                "login.html",
                username=account.username,
                generated_password=generated,
                error=error,
                next_path=next_path,
            )
        )

    @app.post("/login", include_in_schema=False)
    async def login_submit(request: Request) -> Response:
        form = await request.form()
        next_path = safe_next(str(form.get("next", "")))
        if not await admin.verify(str(form.get("username", "")), str(form.get("password", ""))):
            return RedirectResponse(f"/login?error=1&next={quote(next_path, safe='/')}", 303)

        account = await admin.get()
        assert account is not None  # verify() above just confirmed it exists
        response = RedirectResponse(next_path, status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            sign_session(account.session_secret, account.username),
            httponly=True,
            samesite="lax",
            max_age=SESSION_MAX_AGE_SECONDS,
        )
        return response

    @app.post("/logout", include_in_schema=False)
    async def logout() -> Response:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    app.add_middleware(RequireLogin, admin=admin)

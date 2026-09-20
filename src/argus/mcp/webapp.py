"""The break-glass web UI: approve/deny pending requests, and (if a launcher is
configured) start a host session from a request or on your own initiative.

These pages need the admin session (see `argus.webauth`; the login form itself is served by
`argus.api.auth` for the whole app). They check it themselves as well as behind the app-wide
gate, since a launch is the most powerful thing here. No secret ever needs to live in a URL,
so links here carry none.

`add_breakglass_routes` mounts these routes onto any Starlette-compatible app — in
practice, the same app FastMCP's `http_app()` returns for `/mcp`, so the whole thing
runs as one process/port (the `create_app` server).
"""

from __future__ import annotations

import secrets

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from argus.db.breakglass import (
    APPROVED,
    DENIED,
    PENDING,
    BreakGlassRepository,
)
from argus.launcher import HostLauncher
from argus.webauth import SESSION_COOKIE, AdminAuth, verify_session
from argus.webpages import render


def _message(text: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(render("message.html", text=text), status_code=status)


def add_breakglass_routes(
    app: Starlette,
    repository: BreakGlassRepository,
    admin: AdminAuth,
    launcher: HostLauncher | None = None,
) -> None:
    hosts = launcher.hosts if launcher is not None else []

    async def current_user(request: Request) -> str | None:
        cookie = request.cookies.get(SESSION_COOKIE)
        if not cookie:
            return None
        account = await admin.get()
        if account is None:
            return None
        return verify_session(account.session_secret, cookie)

    async def require_login(request: Request) -> Response | None:
        """Returns a redirect to /login if unauthenticated, else None."""
        if await current_user(request) is None:
            return RedirectResponse(url="/login", status_code=303)
        return None

    async def list_pending(request: Request) -> Response:
        if (redirect := await require_login(request)) is not None:
            return redirect
        pending = await repository.list_requests(status=PENDING)
        return HTMLResponse(render("board.html", pending=pending, hosts=hosts))

    async def decide(request: Request) -> Response:
        if (redirect := await require_login(request)) is not None:
            return redirect
        request_id = request.path_params["request_id"]
        status = APPROVED if request.path_params["decision"] == "approve" else DENIED
        existing = await repository.get_request(request_id)
        if existing is None:
            return _message("No such request.", 404)

        host = str((await request.form()).get("host", ""))
        launching = status == APPROVED and launcher is not None
        if launching and host not in hosts:
            return _message("Choose a host to launch on.", 400)

        updated = await repository.set_request_status(request_id, status)

        if launching:
            context_md = (
                f"**Target:** {updated.target_host}\n\n"
                f"**Reason:** {updated.reason}\n\n"
                f"**Evidence:** {updated.evidence}\n\n"
                f"**Proposed objective:** {updated.proposed_objective}\n"
            )
            try:
                await launcher.launch(
                    host=host,
                    session_label=f"breakglass-{updated.id}",
                    context_markdown=context_md,
                )
            except Exception as exc:  # noqa: BLE001 — surfaced to the human, not swallowed
                return _message(
                    f"Approved, but launching the host session failed: {exc}. "
                    "You can still open one yourself.",
                    502,
                )
        return RedirectResponse(url="/breakglass", status_code=303)

    async def launch_form(request: Request) -> Response:
        if (redirect := await require_login(request)) is not None:
            return redirect
        if launcher is None:
            return _message("No launcher configured for this deployment.", 404)
        return HTMLResponse(render("launch.html", hosts=hosts))

    async def launch_submit(request: Request) -> Response:
        if (redirect := await require_login(request)) is not None:
            return redirect
        if launcher is None:
            return _message("No launcher configured for this deployment.", 404)
        form = await request.form()
        context_md = str(form.get("context", "")).strip()
        if not context_md:
            return _message("Context is required.", 400)
        host = str(form.get("host", ""))
        if host not in hosts:
            return _message("Choose a host to launch on.", 400)
        label = f"manual-{secrets.token_hex(4)}"
        try:
            await launcher.launch(host=host, session_label=label, context_markdown=context_md)
        except Exception as exc:  # noqa: BLE001 — surfaced to the human, not swallowed
            return _message(f"Launch failed: {exc}", 502)
        return _message(f"Launched on {host} ({label}). Open it from your Claude app.")

    app.add_route("/breakglass", list_pending, methods=["GET"])
    app.add_route("/breakglass/{request_id}/{decision:str}", decide, methods=["POST"])
    app.add_route("/launch", launch_form, methods=["GET"])
    app.add_route("/launch", launch_submit, methods=["POST"])


def create_app(
    repository: BreakGlassRepository, admin: AdminAuth, launcher: HostLauncher | None = None
) -> Starlette:
    """A bare Starlette app carrying only the break-glass routes. Used in isolation (e.g.
    for tests); in a real deployment these routes are mounted onto the MCP app instead
    (see `argus.mcp.server.build_http_app`) so the whole thing is one process/port."""
    app = Starlette()
    add_breakglass_routes(app, repository, admin, launcher)
    return app

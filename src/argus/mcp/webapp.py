"""The break-glass web UI: approve/deny pending requests, and (if a launcher is
configured) start a host session from a request or on your own initiative.

Login is a single generated admin account (see `argus.webauth`): the first visit to
`/login` creates it and shows the password once; from then on it's a normal
username+password form backed by a signed, HttpOnly session cookie. No secret ever
needs to live in a URL, so links here carry none.

`add_breakglass_routes` mounts these routes onto any Starlette-compatible app — in
practice, the same app FastMCP's `http_app()` returns for `/mcp`, so the whole thing
runs as one process/port (the `create_app` server).
"""

from __future__ import annotations

import html
import secrets

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from argus.launcher import HostLauncher
from argus.providers.breakglass_provider import APPROVED, DENIED, BreakGlassRequest, BreakGlassStore
from argus.webauth import AdminUserStore, sign_session, verify_session

SESSION_COOKIE = "argus_session"


def add_breakglass_routes(
    app: Starlette,
    store: BreakGlassStore,
    admin_store: AdminUserStore,
    launcher: HostLauncher | None = None,
) -> None:
    def current_user(request: Request) -> str | None:
        cookie = request.cookies.get(SESSION_COOKIE)
        if not cookie:
            return None
        account = admin_store.get()
        if account is None:
            return None
        return verify_session(account.session_secret, cookie)

    def require_login(request: Request) -> Response | None:
        """Returns a redirect to /login if unauthenticated, else None."""
        if current_user(request) is None:
            return RedirectResponse(url="/login", status_code=303)
        return None

    async def login_page(request: Request) -> Response:
        account, generated_password = admin_store.get_or_create()
        error = request.query_params.get("error") == "1"
        return HTMLResponse(_render_login_page(account.username, generated_password, error))

    async def login_submit(request: Request) -> Response:
        form = await request.form()
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
        if not admin_store.verify(username, password):
            return RedirectResponse(url="/login?error=1", status_code=303)

        account = admin_store.get()
        assert account is not None  # verify() above just confirmed it exists
        cookie_value = sign_session(account.session_secret, account.username)
        response = RedirectResponse(url="/breakglass", status_code=303)
        response.set_cookie(
            SESSION_COOKIE, cookie_value, httponly=True, samesite="lax", max_age=30 * 24 * 60 * 60
        )
        return response

    async def logout(request: Request) -> Response:
        response = RedirectResponse(url="/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    async def list_pending(request: Request) -> Response:
        if (redirect := require_login(request)) is not None:
            return redirect
        pending = store.list(status="pending")
        return HTMLResponse(_render_page(pending, launcher is not None))

    async def decide(request: Request) -> Response:
        if (redirect := require_login(request)) is not None:
            return redirect
        request_id = request.path_params["request_id"]
        decision = request.path_params["decision"]
        status = APPROVED if decision == "approve" else DENIED
        try:
            updated = store.set_status(request_id, status)
        except KeyError:
            return HTMLResponse("No such request.", status_code=404)

        if status == APPROVED and launcher is not None:
            context_md = (
                f"**Target:** {updated.target_host}\n\n"
                f"**Reason:** {updated.reason}\n\n"
                f"**Evidence:** {updated.evidence}\n\n"
                f"**Proposed objective:** {updated.proposed_objective}\n"
            )
            try:
                await launcher.launch(
                    session_label=f"breakglass-{updated.id}", context_markdown=context_md
                )
            except Exception as exc:  # noqa: BLE001 — surfaced to the human, not swallowed
                return HTMLResponse(
                    f"Approved, but launching the host session failed: {html.escape(str(exc))}. "
                    "You can still open one yourself.",
                    status_code=502,
                )
        return RedirectResponse(url="/breakglass", status_code=303)

    async def launch_form(request: Request) -> Response:
        if (redirect := require_login(request)) is not None:
            return redirect
        if launcher is None:
            return HTMLResponse("No launcher configured for this deployment.", status_code=404)
        return HTMLResponse(_render_launch_form())

    async def launch_submit(request: Request) -> Response:
        if (redirect := require_login(request)) is not None:
            return redirect
        if launcher is None:
            return HTMLResponse("No launcher configured for this deployment.", status_code=404)
        form = await request.form()
        context_md = str(form.get("context", "")).strip()
        if not context_md:
            return HTMLResponse("Context is required.", status_code=400)
        label = f"manual-{secrets.token_hex(4)}"
        try:
            await launcher.launch(session_label=label, context_markdown=context_md)
        except Exception as exc:  # noqa: BLE001 — surfaced to the human, not swallowed
            return HTMLResponse(f"Launch failed: {html.escape(str(exc))}", status_code=502)
        return HTMLResponse(
            f"<p>Launched ({html.escape(label)}). Open it from your Claude app.</p>"
            '<p><a href="/breakglass">Back</a></p>'
        )

    app.add_route("/login", login_page, methods=["GET"])
    app.add_route("/login", login_submit, methods=["POST"])
    app.add_route("/logout", logout, methods=["POST"])
    app.add_route("/breakglass", list_pending, methods=["GET"])
    app.add_route("/breakglass/{request_id}/{decision:str}", decide, methods=["POST"])
    app.add_route("/launch", launch_form, methods=["GET"])
    app.add_route("/launch", launch_submit, methods=["POST"])


def create_app(
    store: BreakGlassStore, admin_store: AdminUserStore, launcher: HostLauncher | None = None
) -> Starlette:
    """A bare Starlette app carrying only the break-glass routes. Used in isolation (e.g.
    for tests); in a real deployment these routes are mounted onto the MCP app instead
    (see `argus.mcp.server.build_http_app`) so the whole thing is one process/port."""
    app = Starlette()
    add_breakglass_routes(app, store, admin_store, launcher)
    return app


_STYLE = """
  body { font-family: -apple-system, system-ui, sans-serif; margin: 0; padding: 1rem;
          background: #111; color: #eee; }
  h1 { font-size: 1.2rem; }
  .card { background: #1c1c1c; border-radius: 10px; padding: 1rem; margin-bottom: 1rem; }
  .field { margin-bottom: 0.5rem; }
  .label { color: #888; font-size: 0.8rem; text-transform: uppercase; }
  .actions { display: flex; gap: 0.5rem; margin-top: 0.75rem; }
  button { flex: 1; padding: 0.75rem; font-size: 1rem; border: none; border-radius: 8px;
           cursor: pointer; }
  .approve { background: #2e7d32; color: white; }
  .deny { background: #b71c1c; color: white; }
  .launch-link { display: block; margin-top: 1rem; color: #8ab4f8; }
  .top-bar { display: flex; justify-content: space-between; align-items: baseline;
             margin-bottom: 1rem; }
  .top-bar form { margin: 0; }
  .top-bar button { flex: none; background: #333; color: #ccc; padding: 0.4rem 0.8rem;
                     font-size: 0.85rem; }
  textarea, input[type=text], input[type=password] {
    width: 100%; box-sizing: border-box; font: inherit; background: #1c1c1c; color: #eee;
    border: 1px solid #333; border-radius: 8px; padding: 0.5rem;
  }
  textarea { min-height: 8rem; }
  .field-group { margin-bottom: 0.75rem; }
  .password-reveal { background: #1c1c1c; border: 1px solid #665; border-radius: 10px;
                      padding: 1rem; margin-bottom: 1.5rem; }
  .password-reveal code { display: block; margin-top: 0.5rem; font-size: 1.1rem;
                           word-break: break-all; color: #ffd54f; }
  .error { color: #ff8a80; margin-bottom: 1rem; }
"""


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{_STYLE}</style>
</head>
<body>
{body}
</body>
</html>"""


def _render_login_page(username: str, generated_password: str | None, error: bool) -> str:
    reveal = ""
    if generated_password:
        reveal = f"""<div class="password-reveal">
  <strong>Admin account created.</strong> This password is shown once — save it now
  (a password manager, not a note you'll lose):
  <code>{html.escape(generated_password)}</code>
</div>"""
    error_html = '<p class="error">Wrong username or password.</p>' if error else ""
    body = f"""<h1>Argus</h1>
{reveal}
{error_html}
<form method="post" action="/login">
  <div class="field-group">
    <input type="text" name="username" placeholder="username" value="{html.escape(username)}" required>
  </div>
  <div class="field-group">
    <input type="password" name="password"
           placeholder="password"{f' value="{html.escape(generated_password)}"' if generated_password else ""}
           required>
  </div>
  <div class="actions">
    <button class="approve" type="submit">Log in</button>
  </div>
</form>"""
    return _page("Argus login", body)


def _render_page(pending: list[BreakGlassRequest], has_launcher: bool) -> str:
    if not pending:
        cards = "<p>No pending requests.</p>"
    else:
        cards = "\n".join(_render_card(r) for r in pending)
    launch_link = (
        '<a class="launch-link" href="/launch">Launch a session yourself &rarr;</a>'
        if has_launcher
        else ""
    )
    top_bar = (
        '<div class="top-bar"><h1>Pending break-glass requests</h1>'
        '<form method="post" action="/logout"><button type="submit">Log out</button></form></div>'
    )
    return _page("Argus break-glass", f"{top_bar}\n{cards}\n{launch_link}")


def _render_card(r: BreakGlassRequest) -> str:
    def esc(s: str) -> str:
        return html.escape(s)

    return f"""<div class="card">
  <div class="field"><span class="label">Target</span><br>{esc(r.target_host)}</div>
  <div class="field"><span class="label">Reason</span><br>{esc(r.reason)}</div>
  <div class="field"><span class="label">Evidence</span><br>{esc(r.evidence)}</div>
  <div class="field"><span class="label">Proposed objective</span><br>{esc(r.proposed_objective)}</div>
  <div class="field"><span class="label">Filed</span><br>{esc(r.created_at)}</div>
  <div class="actions">
    <form method="post" action="/breakglass/{r.id}/approve">
      <button class="approve" type="submit">Approve</button>
    </form>
    <form method="post" action="/breakglass/{r.id}/deny">
      <button class="deny" type="submit">Deny</button>
    </form>
  </div>
</div>"""


def _render_launch_form() -> str:
    body = """<h1>Launch a session yourself</h1>
<p>Starts the same host-side Claude Code session as approving a request would, seeded
with whatever you write below — independent of any pending break-glass request.</p>
<form method="post" action="/launch">
  <textarea name="context" placeholder="What do you want to investigate?" required></textarea>
  <div class="actions">
    <button class="approve" type="submit">Launch</button>
  </div>
</form>
<p><a href="/breakglass">Back</a></p>"""
    return _page("Argus: launch a session", body)

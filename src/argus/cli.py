"""`argus serve` runs the MCP server — as one HTTP process carrying both the `/mcp`
endpoint and (if the breakglass provider is enabled) the phone-facing `/breakglass`
view (login-gated by a generated admin account, see `argus.webauth`), or over stdio
for local/dev use with `--transport stdio`. `argus agent serve` runs the LangGraph
harness. `argus breakglass ...` is the local/terminal convenience for the break-glass
store — the designed path for approving a request is the phone-reachable web view, not
this; see the README."""

from __future__ import annotations

import os

import click

from argus.config import load_config
from argus.mcp.auth import StaticTokenVerifier
from argus.mcp.server import build_http_app, build_server
from argus.providers.breakglass_provider import APPROVED, DENIED, BreakGlassStore


def _resolve_store(config: str | None, store: str | None) -> BreakGlassStore:
    if store:
        return BreakGlassStore(store)
    if config:
        loaded = load_config(config)
        entry = loaded.providers.get("breakglass")
        if entry is None:
            raise click.ClickException("No 'breakglass' provider configured in that config file.")
        store_path = entry.settings().get("store_path", "./argus-breakglass.sqlite")
        return BreakGlassStore(store_path)
    raise click.ClickException("Pass --config or --store to locate the break-glass database.")


def _store_options(f):
    f = click.option(
        "--config", default=None, help="Argus config file (reads the breakglass store path from it)."
    )(f)
    f = click.option(
        "--store", default=None, help="Break-glass sqlite path directly, overrides --config."
    )(f)
    return f


@click.group()
def main() -> None:
    """Argus: policy-gated MCP server + LangGraph agent harness for home infrastructure."""


@main.command()
@click.option("--config", required=True, help="Path to the Argus YAML config file.")
@click.option(
    "--transport",
    type=click.Choice(["http", "stdio"]),
    default="http",
    help="'http' (default) runs /mcp plus the breakglass web view as one service. "
    "'stdio' is for local/dev use — no network exposure, no auth needed.",
)
@click.option("--host", default="127.0.0.1", help="HTTP transport only.")
@click.option("--port", type=int, default=8420, help="HTTP transport only.")
def serve(config: str, transport: str, host: str, port: int) -> None:
    """Run the MCP server."""
    loaded = load_config(config)

    if transport == "stdio":
        # Trusted by construction (only the process that spawned us can talk to us) —
        # no bearer auth, and no breakglass web view since there's no listening port.
        mcp, _providers = build_server(loaded)
        mcp.run()
        return

    mcp_token = os.environ.get("ARGUS_MCP_TOKEN")
    if not mcp_token:
        raise click.ClickException(
            "ARGUS_MCP_TOKEN is not set. /mcp is network-reachable over HTTP and needs "
            "its own auth — set it to a long random value, or use --transport stdio for "
            "local/trusted use."
        )

    mcp, providers = build_server(loaded, auth=StaticTokenVerifier(mcp_token))
    app = build_http_app(mcp, providers)

    import uvicorn

    uvicorn.run(app, host=host, port=port)


@main.group()
def agent() -> None:
    """Run the LangGraph-based interactive agent harness."""


@agent.command("serve")
@click.option("--config", required=True, help="Path to the Argus YAML config file.")
@click.option("--host", default="127.0.0.1")
@click.option("--port", type=int, default=8421)
def agent_serve(config: str, host: str, port: int) -> None:
    """Run the agent's AG-UI-speaking HTTP server."""
    # Lazy imports: keeps `argus serve`/`argus breakglass` startup light even though
    # LangGraph/deepagents/FastAPI/Postgres drivers are installed either way.
    import asyncio

    import uvicorn

    from argus.api.app import build_app
    from argus.db.checkpointer import build_checkpointer

    loaded = load_config(config)

    async def _run() -> None:
        async with build_checkpointer(loaded.agent.database_url) as checkpointer:
            app = build_app(loaded, checkpointer)
            server = uvicorn.Server(uvicorn.Config(app, host=host, port=port))
            await server.serve()

    asyncio.run(_run())


@main.group()
def breakglass() -> None:
    """Inspect/act on break-glass requests."""


@breakglass.command("list")
@click.option("--status", type=click.Choice(["pending", "approved", "denied"]), default=None)
@_store_options
def breakglass_list(status: str | None, config: str | None, store: str | None) -> None:
    """List break-glass requests."""
    db = _resolve_store(config, store)
    requests = db.list(status=status)
    if not requests:
        click.echo("No requests.")
        return
    for r in requests:
        click.echo(f"[{r.status:8}] {r.id}  {r.target_host}  {r.created_at}")
        click.echo(f"           reason: {r.reason}")
        click.echo(f"           proposed: {r.proposed_objective}")


@breakglass.command("approve")
@click.argument("request_id", metavar="ID")
@_store_options
def breakglass_approve(request_id: str, config: str | None, store: str | None) -> None:
    """Mark a request approved."""
    db = _resolve_store(config, store)
    request = db.set_status(request_id, APPROVED)
    click.echo(
        f"Approved {request.id} ({request.target_host}). No access was granted by this — "
        "open the privileged session yourself."
    )


@breakglass.command("deny")
@click.argument("request_id", metavar="ID")
@_store_options
def breakglass_deny(request_id: str, config: str | None, store: str | None) -> None:
    """Mark a request denied."""
    db = _resolve_store(config, store)
    request = db.set_status(request_id, DENIED)
    click.echo(f"Denied {request.id} ({request.target_host}).")


if __name__ == "__main__":
    main()

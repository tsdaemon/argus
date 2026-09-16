"""`argus serve` runs the MCP server — as one HTTP process carrying both the `/mcp`
endpoint and (if the breakglass provider is enabled) the phone-facing `/breakglass`
view (login-gated by a generated admin account, see `argus.webauth`), or over stdio
for local/dev use with `--transport stdio`. `argus breakglass ...` is the
local/terminal convenience for the break-glass store — the designed path for
approving a request is the phone-reachable web view, not this; see the README."""

from __future__ import annotations

import argparse
import os
import sys

from argus.config import load_config
from argus.mcp.auth import StaticTokenVerifier
from argus.mcp.server import build_http_app, build_server
from argus.providers.breakglass_provider import APPROVED, DENIED, BreakGlassStore


def _resolve_store(args: argparse.Namespace) -> BreakGlassStore:
    if args.store:
        return BreakGlassStore(args.store)
    if args.config:
        config = load_config(args.config)
        entry = config.providers.get("breakglass")
        if entry is None:
            print("No 'breakglass' provider configured in that config file.", file=sys.stderr)
            sys.exit(1)
        store_path = entry.settings().get("store_path", "./argus-breakglass.sqlite")
        return BreakGlassStore(store_path)
    print("Pass --config or --store to locate the break-glass database.", file=sys.stderr)
    sys.exit(1)


def _cmd_serve(args: argparse.Namespace) -> None:
    config = load_config(args.config)

    if args.transport == "stdio":
        # Trusted by construction (only the process that spawned us can talk to us) —
        # no bearer auth, and no breakglass web view since there's no listening port.
        mcp, _providers = build_server(config)
        mcp.run()
        return

    mcp_token = os.environ.get("ARGUS_MCP_TOKEN")
    if not mcp_token:
        print(
            "ARGUS_MCP_TOKEN is not set. /mcp is network-reachable over HTTP and needs "
            "its own auth — set it to a long random value, or use --transport stdio for "
            "local/trusted use.",
            file=sys.stderr,
        )
        sys.exit(1)

    mcp, providers = build_server(config, auth=StaticTokenVerifier(mcp_token))
    app = build_http_app(mcp, providers)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)


def _cmd_breakglass_list(args: argparse.Namespace) -> None:
    store = _resolve_store(args)
    requests = store.list(status=args.status)
    if not requests:
        print("No requests.")
        return
    for r in requests:
        print(f"[{r.status:8}] {r.id}  {r.target_host}  {r.created_at}")
        print(f"           reason: {r.reason}")
        print(f"           proposed: {r.proposed_objective}")


def _cmd_breakglass_approve(args: argparse.Namespace) -> None:
    store = _resolve_store(args)
    request = store.set_status(args.id, APPROVED)
    print(f"Approved {request.id} ({request.target_host}). No access was granted by this — "
          "open the privileged session yourself.")


def _cmd_breakglass_deny(args: argparse.Namespace) -> None:
    store = _resolve_store(args)
    request = store.set_status(args.id, DENIED)
    print(f"Denied {request.id} ({request.target_host}).")


def main() -> None:
    parser = argparse.ArgumentParser(prog="argus")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Run the MCP server.")
    serve.add_argument("--config", required=True, help="Path to the Argus YAML config file.")
    serve.add_argument(
        "--transport", choices=["http", "stdio"], default="http",
        help="'http' (default) runs /mcp plus the breakglass web view as one service. "
        "'stdio' is for local/dev use — no network exposure, no auth needed.",
    )
    serve.add_argument("--host", default="127.0.0.1", help="HTTP transport only.")
    serve.add_argument("--port", type=int, default=8420, help="HTTP transport only.")
    serve.set_defaults(func=_cmd_serve)

    breakglass = subparsers.add_parser("breakglass", help="Inspect/act on break-glass requests.")
    bg_sub = breakglass.add_subparsers(dest="breakglass_command", required=True)

    def add_store_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", help="Argus config file (reads the breakglass store path from it).")
        p.add_argument("--store", help="Break-glass sqlite path directly, overrides --config.")

    bg_list = bg_sub.add_parser("list", help="List break-glass requests.")
    bg_list.add_argument("--status", choices=["pending", "approved", "denied"], default=None)
    add_store_args(bg_list)
    bg_list.set_defaults(func=_cmd_breakglass_list)

    bg_approve = bg_sub.add_parser("approve", help="Mark a request approved.")
    bg_approve.add_argument("id")
    add_store_args(bg_approve)
    bg_approve.set_defaults(func=_cmd_breakglass_approve)

    bg_deny = bg_sub.add_parser("deny", help="Mark a request denied.")
    bg_deny.add_argument("id")
    add_store_args(bg_deny)
    bg_deny.set_defaults(func=_cmd_breakglass_deny)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

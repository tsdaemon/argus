"""The break-glass escape hatch.

`request_break_glass` is the one tool an agent can call when it genuinely cannot resolve
something through the read/mutate tools it has. It is classified READ, deliberately: it
cannot mutate anything on the target system, it can only record that a human's attention is
needed. There is no tool anywhere on this MCP server that approves or executes a break-glass
request — that happens entirely outside the MCP surface, via the phone-reachable web view in
`argus.mcp.webapp` (or the `argus breakglass` CLI as a local convenience). Approving a request
does not grant any access by itself; it only marks intent so the human can go open a scoped
session themselves.

`BreakGlassStore` is the shared sqlite-backed store both this provider and `argus.mcp.webapp` /
`argus.cli` read and write. Login to that web view is a generated admin account (see
`argus.webauth.AdminUserStore`) rather than a pre-shared token — no secret to configure,
and no secret ever ends up sitting in a bookmarked URL.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from argus.launcher import HostLauncher, build_launcher
from argus.notify import Notifier, build_notifier
from argus.policy import PolicyEngine, ToolClass
from argus.providers.base import ToolSpec, mcp_bind
from argus.webauth import AdminUserStore

PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"


@dataclass(frozen=True)
class BreakGlassRequest:
    id: str
    created_at: str
    reason: str
    target_host: str
    evidence: str
    proposed_objective: str
    status: str


class BreakGlassStore:
    """Sqlite-backed store of break-glass requests. Opens a short-lived connection per
    call rather than holding one open, so it's safe to share between the MCP server
    process, the web app process, and the CLI without any locking of our own."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    target_host TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    proposed_objective TEXT NOT NULL,
                    status TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def create(
        self, *, reason: str, target_host: str, evidence: str, proposed_objective: str
    ) -> BreakGlassRequest:
        request = BreakGlassRequest(
            id=secrets.token_hex(4),
            created_at=datetime.now(UTC).isoformat(),
            reason=reason,
            target_host=target_host,
            evidence=evidence,
            proposed_objective=proposed_objective,
            status=PENDING,
        )
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO requests VALUES (:id, :created_at, :reason, :target_host, "
                ":evidence, :proposed_objective, :status)",
                request.__dict__,
            )
        return request

    def list(self, *, status: str | None = None) -> list[BreakGlassRequest]:
        query = "SELECT * FROM requests"
        params: tuple[Any, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [BreakGlassRequest(**dict(row)) for row in rows]

    def get(self, request_id: str) -> BreakGlassRequest | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM requests WHERE id = ?", (request_id,)
            ).fetchone()
        return BreakGlassRequest(**dict(row)) if row else None

    def set_status(self, request_id: str, status: str) -> BreakGlassRequest:
        existing = self.get(request_id)
        if existing is None:
            raise KeyError(f"No break-glass request with id '{request_id}'")
        with self._connect() as conn:
            conn.execute(
                "UPDATE requests SET status = ? WHERE id = ?", (status, request_id)
            )
        return self.get(request_id)  # type: ignore[return-value]


class BreakglassProvider:
    name = "breakglass"

    def __init__(self, config: dict[str, Any]) -> None:
        store_path = config.get("store_path", "./argus-breakglass.sqlite")
        self.store = BreakGlassStore(store_path)
        # Same sqlite file, separate table — one less path to configure.
        self.admin_store = AdminUserStore(store_path)
        self._approval_url = config.get("approval_url")
        self._notifier: Notifier = build_notifier(config.get("notify"))
        # Not agent-reachable: only used from the human-facing web view (argus.mcp.webapp),
        # never from the request_break_glass tool itself. See argus.launcher for why.
        self.launcher: HostLauncher | None = build_launcher(config.get("launcher"))

    def tool_specs(self, config: dict[str, Any]) -> list[ToolSpec]:
        store = self.store
        notifier = self._notifier
        approval_url = self._approval_url

        async def request_break_glass(
            reason: str, target_host: str, evidence: str, proposed_objective: str
        ) -> dict[str, Any]:
            """Record that this can't be resolved with the tools available and a human
            needs to open a scoped session themselves. Does not grant or perform anything.

            Args:
                reason: Why you can't resolve this yourself.
                target_host: The host/service that needs hands-on attention.
                evidence: What you've already gathered (logs, status, etc.).
                proposed_objective: What you'd want a privileged session to do.
            """
            request = store.create(
                reason=reason,
                target_host=target_host,
                evidence=evidence,
                proposed_objective=proposed_objective,
            )
            link = f"{approval_url.rstrip('/')}/breakglass" if approval_url else None
            await notifier.notify(
                title=f"Argus break-glass request: {target_host}",
                message=f"{reason}\n\nProposed: {proposed_objective}",
                url=link,
            )
            return {
                "id": request.id,
                "status": request.status,
                "note": (
                    "Filed. This does not grant any access. A human must review and "
                    "approve it out-of-band before anyone opens a privileged session."
                ),
            }

        return [
            ToolSpec(
                tool_id="breakglass.request_break_glass",
                tool_class=ToolClass.READ,
                summary="File a break-glass escalation request. Cannot grant any access by itself.",
                fn=request_break_glass,
            )
        ]

    def register(self, mcp: Any, policy: PolicyEngine, config: dict[str, Any]) -> None:
        mcp_bind(mcp, policy, self.tool_specs(config))

"""SSH-based host launcher.

The security boundary here lives entirely on the *remote* side, not in this class: the
identity file configured here is expected to correspond to an `authorized_keys` entry
restricted with a forced command (`command="..."`) plus `no-pty,no-port-forwarding,
no-X11-forwarding,no-agent-forwarding` — so whatever this class asks the SSH client to
run is irrelevant, the server always runs the forced command instead, ignoring the
client's request entirely (standard OpenSSH behavior). See the README for the exact
`authorized_keys` line and `argus.host_launch` for what that forced command should be.

`permission_mode` and `ttl_seconds` are fixed at construction time from this
deployment's own config — never from a break-glass request's fields — precisely so an
agent's `request_break_glass` call can influence what a human reads, never how the
resulting session is launched or scoped.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any


class SshHostLauncher:
    def __init__(
        self,
        *,
        host: str,
        user: str,
        identity_file: str | None = None,
        port: int = 22,
        permission_mode: str = "manual",
        ttl_seconds: int | None = None,
    ) -> None:
        self._host = host
        self._user = user
        self._identity_file = identity_file
        self._port = port
        self._permission_mode = permission_mode
        self._ttl_seconds = ttl_seconds

    async def launch(self, *, session_label: str, context_markdown: str) -> None:
        payload: dict[str, Any] = {
            "session_label": session_label,
            "context_markdown": context_markdown,
            "permission_mode": self._permission_mode,
        }
        if self._ttl_seconds is not None:
            payload["ttl_seconds"] = self._ttl_seconds

        cmd = ["ssh", "-o", "BatchMode=yes", "-p", str(self._port)]
        if self._identity_file:
            cmd += ["-i", self._identity_file]
        cmd.append(f"{self._user}@{self._host}")
        # No remote command specified: irrelevant given the forced-command restriction
        # this launcher assumes is in place server-side, and clearer for it — we aren't
        # pretending to ask for anything the server would actually honor.

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate(json.dumps(payload).encode("utf-8"))
        if proc.returncode != 0:
            raise RuntimeError(
                f"ssh launch failed (exit {proc.returncode}): "
                f"{stderr.decode(errors='replace').strip()}"
            )

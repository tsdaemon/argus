"""SSH-based host launcher.

The security boundary here lives entirely on the *remote* side, not in this class: the
identity file configured here is expected to correspond to an `authorized_keys` entry
restricted with a forced command (`command="..."`) plus `no-pty,no-port-forwarding,
no-X11-forwarding,no-agent-forwarding` — so whatever this class asks the SSH client to
run is irrelevant, the server always runs the forced command instead, ignoring the
client's request entirely (standard OpenSSH behavior). See the README for the exact
`authorized_keys` line, and docs/breakglass.md for the forced command, which autohome installs.

`permission_mode` and `ttl_seconds` are fixed per host from this
deployment's own config — never from a break-glass request's fields — precisely so an
agent's `request_break_glass` call can influence what a human reads, never how the
resulting session is launched or scoped.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

# Sent with every payload so a host running an older or newer launcher script can refuse
# a shape it does not understand instead of misreading it.
PROTOCOL = 1


@dataclass(frozen=True)
class SshTarget:
    host: str
    user: str
    identity_file: str | None = None
    port: int = 22
    permission_mode: str = "manual"
    ttl_seconds: int | None = None


class SshHostLauncher:
    def __init__(self, targets: dict[str, SshTarget]) -> None:
        self._targets = targets

    @property
    def hosts(self) -> list[str]:
        return list(self._targets)

    async def launch(self, *, host: str, session_label: str, context_markdown: str) -> None:
        target = self._targets.get(host)
        if target is None:
            raise ValueError(f"Unknown host {host!r}; configured hosts: {self.hosts}")

        payload: dict[str, Any] = {
            "protocol": PROTOCOL,
            "session_label": session_label,
            "context_markdown": context_markdown,
            "permission_mode": target.permission_mode,
        }
        if target.ttl_seconds is not None:
            payload["ttl_seconds"] = target.ttl_seconds

        cmd = ["ssh", "-o", "BatchMode=yes", "-p", str(target.port)]
        if target.identity_file:
            cmd += ["-i", target.identity_file]
        cmd.append(f"{target.user}@{target.host}")
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
                f"ssh launch to {host} failed (exit {proc.returncode}): "
                f"{stderr.decode(errors='replace').strip()}"
            )

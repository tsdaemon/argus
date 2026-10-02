"""SSH: run a shell command on one of the configured hosts, classified per call.

Hosts are named in config. Top-level settings (key, known_hosts, user, port, classifier)
are defaults that a host entry overrides, as in the break-glass launcher. A host's
`description` tells the model what it is talking to; with the host's name and address it is
the context its classifier judges commands against.

Nothing on a host has to narrow what the key can do (the router's only account is root),
so the classifier is the boundary: policy maps its class to run / ask / refuse (see
`argus.classifier`). The key is argus's direct-access identity (`argus_ssh`), separate
from the break-glass launcher key, so widening one never widens the other.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal

from fastmcp import Context

from argus.classifier import CommandClassifier, build_classifier
from argus.policy import CallClassification, PolicyEngine, ToolClass
from argus.providers.base import ToolSpec, mcp_bind

_MAX_OUTPUT_CHARS = 16_000


@dataclass(frozen=True)
class SshHost:
    name: str
    host: str
    description: str
    user: str = "root"
    port: int = 22
    identity_file: str | None = None
    known_hosts_file: str | None = None
    # Default per command, and the most a call may ask for with `timeout_seconds`.
    timeout_seconds: float = 30.0
    max_timeout_seconds: float = 300.0

    @property
    def context(self) -> str:
        """What the classifier is told about the host a command will run on."""
        return f"host `{self.name}` ({self.user}@{self.host}): {self.description}"

    def command(self, command: str) -> list[str]:
        # `-tt` gives the command a terminal on the host. Stopping the local client then
        # closes it, and the host sends a hangup to everything the command started; without
        # it the remote command outlives a timeout. The router has no `timeout` binary.
        # LogLevel=ERROR drops the "Connection closed" notice `-tt` prints, not real errors.
        cmd = [
            "ssh", "-tt", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "LogLevel=ERROR", "-p", str(self.port),
        ]
        if self.identity_file:
            cmd += ["-i", self.identity_file]
        if self.known_hosts_file:
            cmd += ["-o", f"UserKnownHostsFile={self.known_hosts_file}"]
        return [*cmd, "--", f"{self.user}@{self.host}", command]


class SshProvider:
    name = "ssh"

    def __init__(
        self,
        config: dict[str, Any],
        *,
        classifiers: dict[str, CommandClassifier] | None = None,
    ) -> None:
        hosts = config.get("hosts") or {}
        if not hosts:
            raise ValueError("The ssh provider needs at least one entry under `hosts`.")
        defaults = {k: v for k, v in config.items() if k != "hosts"}

        self._hosts: dict[str, SshHost] = {}
        self._classifiers: dict[str, CommandClassifier] = {}
        for name, entry in hosts.items():
            settings = {**defaults, **entry}
            classifier_config = settings.pop("classifier", None)
            # `${ENV_VAR}` expansion yields a string.
            settings["port"] = int(settings.get("port", 22))
            host = SshHost(name=name, **settings)
            self._hosts[name] = host
            self._classifiers[name] = (classifiers or {}).get(name) or build_classifier(
                classifier_config, context=host.context
            )

    async def _classify(self, args: dict[str, Any]) -> CallClassification:
        classifier = self._classifiers.get(args["host"])
        if classifier is None:
            return CallClassification(ToolClass.DESTRUCTIVE, f"Unknown host {args['host']!r}.")
        return await classifier.classify(args["command"])

    def tool_specs(self, config: dict[str, Any]) -> list[ToolSpec]:
        async def run(
            host: str, command: str, ctx: Context, timeout_seconds: float | None = None
        ) -> str:
            """Run a shell command on a named host and return its exit code and output."""
            target = self._hosts.get(host)
            if target is None:
                raise ValueError(f"Unknown host {host!r}; configured hosts: {list(self._hosts)}")
            limit = min(timeout_seconds or target.timeout_seconds, target.max_timeout_seconds)
            proc = await asyncio.create_subprocess_exec(
                *target.command(command),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            # Read as it arrives, so a command that times out still returns what it printed.
            output = asyncio.ensure_future(asyncio.gather(proc.stdout.read(), proc.stderr.read()))
            try:
                await asyncio.wait_for(proc.wait(), limit)
                timed_out = False
            except TimeoutError:
                proc.kill()
                await proc.wait()
                timed_out = True
            stdout, stderr = await output
            result = _format_result(proc.returncode, stdout, stderr)
            if timed_out:
                result = f"Timed out after {limit:g}s; stopped on the host. Output so far:\n{result}"
            return result

        # The host names become an enum in the tool schema, for the model and MCP clients.
        run.__annotations__["host"] = Literal[tuple(self._hosts)]
        run.__annotations__["timeout_seconds"] = float | None

        host_list = "; ".join(f"`{h.name}`: {h.description}" for h in self._hosts.values())
        return [
            ToolSpec(
                tool_id="ssh.run",
                tool_class=ToolClass.MUTATE,
                summary=(
                    f"Run a shell command over SSH on one of these hosts: {host_list}. Each "
                    "command is risk-classified: reads run at once, changes need operator "
                    "approval, destructive commands are refused. Prefer one plain command per "
                    "call; no interactive commands. Commands are stopped after 30 seconds by "
                    "default; pass `timeout_seconds` (up to 300) for a longer one, such as a ping "
                    "series. A command that times out returns the output it printed so far."
                ),
                fn=run,
                mcp_kwargs={"name": "ssh_run"},
                classify=self._classify,
            ),
        ]

    def register(self, mcp: Any, policy: PolicyEngine, config: dict[str, Any]) -> None:
        mcp_bind(mcp, policy, self.tool_specs(config))


def _format_result(returncode: int | None, stdout: bytes, stderr: bytes) -> str:
    # The terminal (`-tt`) merges the command's stderr into stdout and ends lines with \r\n;
    # stderr here is the ssh client's own (connection and key errors).
    out = stdout.decode("utf-8", errors="replace").replace("\r\n", "\n")
    err = stderr.decode("utf-8", errors="replace")
    text = f"exit {returncode}\n{out}"
    if err.strip():
        text += f"\n[stderr]\n{err}"
    if len(text) > _MAX_OUTPUT_CHARS:
        text = text[:_MAX_OUTPUT_CHARS] + f"\n[truncated to {_MAX_OUTPUT_CHARS} characters]"
    return text

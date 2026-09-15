"""Docker: container visibility (READ) and container restart (MUTATE).

Scoped to an explicit allowlist by design — even the READ tools should only ever see
containers you've chosen to expose to the agent, not everything a shared Docker socket
happens to be running.
"""

from __future__ import annotations

from typing import Any

import docker
from docker.errors import NotFound
from fastmcp import Context

from argus.policy import PolicyEngine, ToolClass


class DockerProvider:
    name = "docker"

    def __init__(self, config: dict[str, Any]) -> None:
        self._socket = config.get("socket", "unix:///var/run/docker.sock")
        self._allowed: list[str] | None = config.get("allowed_containers")
        self._client: docker.DockerClient | None = None

    def _get_client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.DockerClient(base_url=self._socket)
        return self._client

    def _check_allowed(self, name: str) -> None:
        if self._allowed is not None and name not in self._allowed:
            raise ValueError(
                f"Container '{name}' is not in this deployment's allowed_containers list."
            )

    def register(self, mcp: Any, policy: PolicyEngine, config: dict[str, Any]) -> None:
        client = self._get_client

        @policy.register(
            mcp,
            tool_id="docker.list_containers",
            tool_class=ToolClass.READ,
            summary="List containers visible to this deployment.",
        )
        async def list_containers() -> list[dict[str, Any]]:
            """List containers this Argus deployment is allowed to see, with their state."""
            containers = client().containers.list(all=True)
            return [
                {"name": c.name, "status": c.status, "image": _image_tag(c)}
                for c in containers
                if self._allowed is None or c.name in self._allowed
            ]

        @policy.register(
            mcp,
            tool_id="docker.get_container_status",
            tool_class=ToolClass.READ,
            summary="Get one container's status.",
        )
        async def get_container_status(name: str) -> dict[str, Any]:
            """Get the current status of a single container by name."""
            self._check_allowed(name)
            container = _get_container_or_raise(client(), name)
            return {"name": container.name, "status": container.status, "image": _image_tag(container)}

        @policy.register(
            mcp,
            tool_id="docker.get_container_logs",
            tool_class=ToolClass.READ,
            summary="Read recent log lines from a container.",
        )
        async def get_container_logs(name: str, tail: int = 200) -> str:
            """Return the last `tail` lines of a container's logs."""
            self._check_allowed(name)
            container = _get_container_or_raise(client(), name)
            return container.logs(tail=tail).decode("utf-8", errors="replace")

        @policy.register(
            mcp,
            tool_id="docker.inspect_container",
            tool_class=ToolClass.READ,
            summary="Get a container's full inspect output.",
        )
        async def inspect_container(name: str) -> dict[str, Any]:
            """Return the raw `docker inspect` attributes for a container."""
            self._check_allowed(name)
            container = _get_container_or_raise(client(), name)
            return container.attrs

        @policy.register(
            mcp,
            tool_id="docker.restart_container",
            tool_class=ToolClass.MUTATE,
            summary="Restart a running container.",
        )
        async def restart_container(name: str, ctx: Context) -> str:
            """Restart a container by name. Requires operator approval."""
            self._check_allowed(name)
            container = _get_container_or_raise(client(), name)
            container.restart(timeout=30)
            return f"Restarted '{name}'."


def _get_container_or_raise(client: docker.DockerClient, name: str) -> Any:
    try:
        return client.containers.get(name)
    except NotFound as exc:
        raise ValueError(f"No such container: '{name}'") from exc


def _image_tag(container: Any) -> str:
    tags = container.image.tags
    return tags[0] if tags else container.image.short_id

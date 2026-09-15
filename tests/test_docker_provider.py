from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from docker.errors import NotFound

from argus.policy import PolicyEngine
from argus.providers.docker_provider import DockerProvider


class FakeMCP:
    def __init__(self) -> None:
        self.registered: dict[str, object] = {}

    def tool(self, **kwargs):
        def decorator(fn):
            self.registered[fn.__name__] = fn
            return fn

        return decorator


class AlwaysApprove:
    async def request(self, ctx, *, action, summary, details) -> bool:
        return True


def fake_container(name: str, status: str = "running", tag: str = "img:latest"):
    return SimpleNamespace(
        name=name,
        status=status,
        image=SimpleNamespace(tags=[tag], short_id="sha256:abc"),
        logs=MagicMock(return_value=b"log line\n"),
        restart=MagicMock(),
        attrs={"Id": name},
    )


@pytest.fixture
def provider_and_mcp():
    provider = DockerProvider({"allowed_containers": ["qbittorrent"]})
    mcp = FakeMCP()
    policy = PolicyEngine(AlwaysApprove())
    provider.register(mcp, policy, {})
    return provider, mcp


@pytest.mark.asyncio
async def test_list_containers_filters_to_allowlist(provider_and_mcp):
    provider, mcp = provider_and_mcp
    fake_client = MagicMock()
    fake_client.containers.list.return_value = [
        fake_container("qbittorrent"),
        fake_container("some-other-service"),
    ]
    provider._client = fake_client

    result = await mcp.registered["list_containers"]()

    assert [c["name"] for c in result] == ["qbittorrent"]


@pytest.mark.asyncio
async def test_get_container_status_rejects_non_allowlisted(provider_and_mcp):
    _provider, mcp = provider_and_mcp
    with pytest.raises(ValueError, match="not in this deployment's allowed_containers"):
        await mcp.registered["get_container_status"](name="some-other-service")


@pytest.mark.asyncio
async def test_get_container_status_raises_on_missing_container(provider_and_mcp):
    provider, mcp = provider_and_mcp
    fake_client = MagicMock()
    fake_client.containers.get.side_effect = NotFound("no such container")
    provider._client = fake_client

    with pytest.raises(ValueError, match="No such container"):
        await mcp.registered["get_container_status"](name="qbittorrent")


@pytest.mark.asyncio
async def test_restart_container_only_runs_after_approval(provider_and_mcp):
    provider, mcp = provider_and_mcp
    container = fake_container("qbittorrent")
    fake_client = MagicMock()
    fake_client.containers.get.return_value = container
    provider._client = fake_client

    result = await mcp.registered["restart_container"](name="qbittorrent", ctx="fake-ctx")

    container.restart.assert_called_once_with(timeout=30)
    assert "Restarted" in result

from __future__ import annotations

import pytest

from argus.policy import PolicyEngine
from argus.providers.breakglass_provider import PENDING, BreakglassProvider
from argus.webauth import AdminAuth
from tests.fakes import InMemoryAdmin, InMemoryBreakGlass


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


@pytest.fixture
def provider_and_mcp():
    provider = BreakglassProvider(
        {}, repository=InMemoryBreakGlass(), admin=AdminAuth(InMemoryAdmin())
    )
    mcp = FakeMCP()
    policy = PolicyEngine(AlwaysApprove())
    provider.register(mcp, policy, {})
    return provider, mcp


def test_provider_refuses_to_exist_without_its_dependencies():
    with pytest.raises(RuntimeError, match="needs a BreakGlassRepository and AdminAuth"):
        BreakglassProvider({})


@pytest.mark.asyncio
async def test_request_break_glass_is_registered_and_files_a_pending_request(provider_and_mcp):
    provider, mcp = provider_and_mcp

    result = await mcp.registered["request_break_glass"](
        reason="pihole down",
        target_host="theseus",
        evidence="dig fails",
        proposed_objective="restart pihole via ssh",
    )

    assert result["status"] == PENDING
    stored = await provider.repository.get_request(result["id"])
    assert stored is not None
    assert stored.target_host == "theseus"
    assert await provider.repository.list_requests(status=PENDING) == [stored]

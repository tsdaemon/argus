from __future__ import annotations

from pathlib import Path

import pytest

from argus.policy import PolicyEngine
from argus.providers.breakglass_provider import PENDING, BreakglassProvider


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
def provider_and_mcp(tmp_path: Path):
    provider = BreakglassProvider({"store_path": str(tmp_path / "breakglass.sqlite")})
    mcp = FakeMCP()
    policy = PolicyEngine(AlwaysApprove())
    provider.register(mcp, policy, {})
    return provider, mcp


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
    stored = provider.store.get(result["id"])
    assert stored is not None
    assert stored.target_host == "theseus"
    assert provider.store.list(status=PENDING) == [stored]

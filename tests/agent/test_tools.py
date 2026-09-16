from __future__ import annotations

import pytest

from argus.agent.tools import external_name, langchain_bind
from argus.policy import PolicyDecision, PolicyEngine, ToolClass
from argus.providers.base import ToolSpec
from argus.providers.docker_provider import DockerProvider


class AlwaysApprove:
    async def request(self, ctx, *, action, summary, details) -> bool:
        return True


def make_policy(**kwargs) -> PolicyEngine:
    return PolicyEngine(AlwaysApprove(), **kwargs)


def test_external_name_replaces_dots():
    assert external_name("docker.restart_container") == "docker_restart_container"


def test_read_tools_are_bound_without_interrupt():
    provider = DockerProvider({"allowed_containers": ["qbittorrent"]})
    policy = make_policy()

    tools, interrupt_on = langchain_bind(policy, provider.tool_specs({}))

    names = {t.name for t in tools}
    assert "docker_list_containers" in names
    assert "docker_get_container_status" in names
    assert interrupt_on == {} or "docker_list_containers" not in interrupt_on


def test_mutate_tool_defaults_to_require_approval_and_is_gated():
    provider = DockerProvider({"allowed_containers": ["qbittorrent"]})
    policy = make_policy()

    tools, interrupt_on = langchain_bind(policy, provider.tool_specs({}))

    assert "docker_restart_container" in {t.name for t in tools}
    assert "docker_restart_container" in interrupt_on
    config = interrupt_on["docker_restart_container"]
    assert config["allowed_decisions"] == ["approve", "reject"]


def test_override_can_allow_a_mutate_tool_without_interrupt():
    provider = DockerProvider({"allowed_containers": ["qbittorrent"]})
    policy = make_policy(overrides={"docker.restart_container": PolicyDecision.ALLOW})

    tools, interrupt_on = langchain_bind(policy, provider.tool_specs({}))

    assert "docker_restart_container" in {t.name for t in tools}
    assert "docker_restart_container" not in interrupt_on


def test_destructive_tool_is_never_bound():
    async def destroy(name: str) -> str:
        return "boom"

    spec = ToolSpec(
        tool_id="docker.destroy_everything",
        tool_class=ToolClass.DESTRUCTIVE,
        summary="nope",
        fn=destroy,
    )
    policy = make_policy()

    tools, interrupt_on = langchain_bind(policy, [spec])

    assert tools == []
    assert interrupt_on == {}


@pytest.mark.asyncio
async def test_bound_tool_invokes_the_real_implementation():
    calls = []

    async def read_thing(value: int) -> str:
        calls.append(value)
        return f"read {value}"

    spec = ToolSpec(tool_id="x.read_thing", tool_class=ToolClass.READ, summary="reads", fn=read_thing)
    policy = make_policy()

    tools, _interrupt_on = langchain_bind(policy, [spec])
    (tool,) = tools

    result = await tool.ainvoke({"value": 42})

    assert calls == [42]
    assert "read 42" in result


@pytest.mark.asyncio
async def test_bound_tool_strips_ctx_from_schema_but_still_injects_it():
    seen_ctx = []

    async def mutate_thing(value: int, ctx=None) -> str:
        seen_ctx.append(ctx)
        return f"did {value}"

    spec = ToolSpec(
        tool_id="x.mutate_thing", tool_class=ToolClass.READ, summary="mutates", fn=mutate_thing
    )
    policy = make_policy()

    tools, _interrupt_on = langchain_bind(policy, [spec])
    (tool,) = tools

    assert "ctx" not in tool.args_schema.model_fields
    await tool.ainvoke({"value": 1})
    assert seen_ctx == [None]

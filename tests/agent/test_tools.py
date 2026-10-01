from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from argus.agent.classification import METADATA_KEY
from argus.agent.tools import classification_middleware, external_name, langchain_bind
from argus.policy import CallClassification, PolicyDecision, PolicyEngine, ToolClass
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



def classified_spec(tool_class: ToolClass, ran: list) -> ToolSpec:
    async def router(command: str, ctx=None) -> str:
        ran.append(command)
        return "ok"

    async def classify(args):
        return CallClassification(tool_class, f"note for {args['command']}")

    return ToolSpec(
        tool_id="ssh.router",
        tool_class=ToolClass.MUTATE,
        summary="Run on the router.",
        fn=router,
        classify=classify,
    )


def state_with(tool_class: ToolClass | None, command: str = "ip route") -> dict:
    """Conversation state whose last model message calls `ssh_router` once, with the
    classification the middleware would have stored (or none)."""
    message = AIMessage(
        content="",
        tool_calls=[{"name": "ssh_router", "args": {"command": command}, "id": "call-1"}],
    )
    if tool_class is not None:
        message.response_metadata[METADATA_KEY] = {
            "call-1": {"tool_class": tool_class.value, "note": f"note for {command}"}
        }
    return {"messages": [message]}


def interrupt_wanted(config, state: dict) -> bool:
    (tool_call,) = state["messages"][-1].tool_calls
    return config["when"](SimpleNamespace(tool_call=tool_call, state=state))


@pytest.mark.parametrize(
    ("tool_class", "asks"),
    [
        (ToolClass.READ, False),
        (ToolClass.MUTATE, True),
        (ToolClass.DESTRUCTIVE, False),
        (None, True),  # nothing stored: never run unseen
    ],
)
def test_classified_tool_interrupts_from_the_stored_classification(tool_class, asks):
    tools, interrupt_on = langchain_bind(make_policy(), [classified_spec(ToolClass.READ, [])])

    assert [t.name for t in tools] == ["ssh_router"]
    assert interrupt_wanted(interrupt_on["ssh_router"], state_with(tool_class)) is asks


def test_classified_interrupt_description_carries_the_stored_note():
    _, interrupt_on = langchain_bind(make_policy(), [classified_spec(ToolClass.MUTATE, [])])
    state = state_with(ToolClass.MUTATE, "service restart_wan")
    (tool_call,) = state["messages"][-1].tool_calls

    description = interrupt_on["ssh_router"]["description"](tool_call, state, None)

    assert description == "Approve `ssh.router`? note for service restart_wan"


def test_classification_middleware_covers_only_classified_bound_tools():
    async def plain(value: int) -> str:
        return "x"

    plain_spec = ToolSpec(tool_id="x.plain", tool_class=ToolClass.READ, summary="s", fn=plain)

    assert classification_middleware(make_policy(), [plain_spec]) is None
    denied = make_policy(overrides={"ssh.router": PolicyDecision.DENY})
    assert classification_middleware(denied, [classified_spec(ToolClass.READ, [])]) is None
    assert classification_middleware(make_policy(), [classified_spec(ToolClass.READ, [])]) is not None

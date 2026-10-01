from __future__ import annotations

import pytest

from argus.policy import CallClassification, PolicyDecision, PolicyEngine, ToolClass


class FakeMCP:
    """Records what would have been registered, without any real MCP machinery."""

    def __init__(self) -> None:
        self.registered: dict[str, object] = {}

    def tool(self, **kwargs):
        def decorator(fn):
            self.registered[fn.__name__] = fn
            return fn

        return decorator


class FakeApprovalBackend:
    def __init__(self, *, approve: bool) -> None:
        self.approve = approve
        self.calls: list[dict] = []

    async def request(self, ctx, *, action, summary, details) -> bool:
        self.calls.append({"ctx": ctx, "action": action, "summary": summary, "details": details})
        return self.approve


def make_engine(approve: bool = True, **kwargs) -> tuple[PolicyEngine, FakeApprovalBackend]:
    backend = FakeApprovalBackend(approve=approve)
    engine = PolicyEngine(backend, **kwargs)
    return engine, backend


def test_read_defaults_to_allow():
    engine, _ = make_engine()
    assert engine.decide("x.read_thing", ToolClass.READ) is PolicyDecision.ALLOW


def test_mutate_defaults_to_require_approval():
    engine, _ = make_engine()
    assert engine.decide("x.mutate_thing", ToolClass.MUTATE) is PolicyDecision.REQUIRE_APPROVAL


def test_destructive_defaults_to_deny():
    engine, _ = make_engine()
    assert engine.decide("x.destroy_thing", ToolClass.DESTRUCTIVE) is PolicyDecision.DENY


def test_override_wins_over_class_default():
    engine, _ = make_engine(overrides={"x.mutate_thing": PolicyDecision.ALLOW})
    assert engine.decide("x.mutate_thing", ToolClass.MUTATE) is PolicyDecision.ALLOW


def test_destructive_tool_is_never_registered():
    engine, _ = make_engine()
    mcp = FakeMCP()

    @engine.register(mcp, tool_id="x.destroy_thing", tool_class=ToolClass.DESTRUCTIVE, summary="nope")
    async def destroy_thing(ctx):
        return "boom"

    assert destroy_thing is None
    assert "destroy_thing" not in mcp.registered


def test_read_tool_is_registered_unwrapped():
    engine, backend = make_engine()
    mcp = FakeMCP()

    @engine.register(mcp, tool_id="x.read_thing", tool_class=ToolClass.READ, summary="reads")
    async def read_thing():
        return "ok"

    assert "read_thing" in mcp.registered
    assert backend.calls == []


@pytest.mark.asyncio
async def test_mutate_tool_runs_only_after_approval():
    engine, backend = make_engine(approve=True)
    mcp = FakeMCP()

    @engine.register(mcp, tool_id="x.mutate_thing", tool_class=ToolClass.MUTATE, summary="mutates")
    async def mutate_thing(value: int, ctx: object) -> str:
        return f"did it: {value}"

    result = await mcp.registered["mutate_thing"](value=1, ctx="fake-ctx")
    assert result == "did it: 1"
    assert len(backend.calls) == 1
    assert backend.calls[0]["action"] == "x.mutate_thing"
    assert backend.calls[0]["details"]["args"] == {"value": 1}


@pytest.mark.asyncio
async def test_mutate_tool_refuses_when_not_approved():
    engine, backend = make_engine(approve=False)
    mcp = FakeMCP()

    @engine.register(mcp, tool_id="x.mutate_thing", tool_class=ToolClass.MUTATE, summary="mutates")
    async def mutate_thing(value: int, ctx: object) -> str:
        return f"did it: {value}"

    with pytest.raises(PermissionError):
        await mcp.registered["mutate_thing"](value=1, ctx="fake-ctx")
    assert len(backend.calls) == 1


def classified(tool_class: ToolClass):
    async def classify(args):
        return CallClassification(tool_class, f"classified {args['command']}")

    return classify


async def run_classified(tool_class: ToolClass, *, approve=True, **engine_kwargs):
    engine, backend = make_engine(approve=approve, **engine_kwargs)
    mcp = FakeMCP()
    ran = []

    async def shell(command: str, ctx=None) -> str:
        ran.append(command)
        return "ok"

    engine.register(
        mcp, tool_id="ssh.router", tool_class=ToolClass.MUTATE, summary="run",
        classify=classified(tool_class),
    )(shell)
    result = await mcp.registered["shell"](command="ip route", ctx="ctx")
    return result, ran, backend


@pytest.mark.asyncio
async def test_classified_read_call_runs_without_asking():
    result, ran, backend = await run_classified(ToolClass.READ)

    assert result == "ok" and ran == ["ip route"]
    assert backend.calls == []


@pytest.mark.asyncio
async def test_classified_mutate_call_asks_with_the_classifier_note():
    _, ran, backend = await run_classified(ToolClass.MUTATE)

    assert ran == ["ip route"]
    (call,) = backend.calls
    assert "classified ip route" in call["summary"]


@pytest.mark.asyncio
async def test_classified_mutate_call_does_not_run_when_declined():
    with pytest.raises(PermissionError):
        await run_classified(ToolClass.MUTATE, approve=False)


@pytest.mark.asyncio
async def test_classified_destructive_call_is_refused_without_asking():
    engine, backend = make_engine()
    mcp = FakeMCP()

    async def shell(command: str, ctx=None) -> str:
        raise AssertionError("must not run")

    engine.register(
        mcp, tool_id="ssh.router", tool_class=ToolClass.MUTATE, summary="run",
        classify=classified(ToolClass.DESTRUCTIVE),
    )(shell)

    with pytest.raises(PermissionError, match="refused"):
        await mcp.registered["shell"](command="mtd-erase2 nvram", ctx="ctx")
    assert backend.calls == []


def test_denying_a_classified_tool_removes_it():
    engine, _ = make_engine(overrides={"ssh.router": PolicyDecision.DENY})
    mcp = FakeMCP()

    async def shell(command: str, ctx=None) -> str:
        return "ok"

    engine.register(
        mcp, tool_id="ssh.router", tool_class=ToolClass.MUTATE, summary="run",
        classify=classified(ToolClass.READ),
    )(shell)

    assert mcp.registered == {}

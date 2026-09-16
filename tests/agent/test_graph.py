from __future__ import annotations

from pathlib import Path

from deepagents import GeneralPurposeSubagentProfile, HarnessProfile, register_harness_profile
from deepagents._models import get_model_provider
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver

from argus.agent.graph import _build_model, build_graph
from argus.config import AgentConfig
from argus.policy import PolicyDecision, PolicyEngine
from argus.providers.docker_provider import DockerProvider

# graph.py disables the "general purpose subagent" for provider "openai" (what
# ChatOpenAI/OpenRouter resolves as). GenericFakeChatModel resolves as a different
# provider, so the same disabling is registered here to match production behavior.
register_harness_profile(
    "genericfakechatmodel",
    HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)),
)


class AlwaysApprove:
    async def request(self, ctx, *, action, summary, details) -> bool:
        return True


def fake_model() -> GenericFakeChatModel:
    return GenericFakeChatModel(messages=iter([]))


def build(tmp_path: Path, **policy_kwargs):
    config = AgentConfig(workspace_root=str(tmp_path / "workspace"))
    policy = PolicyEngine(AlwaysApprove(), **policy_kwargs)
    provider = DockerProvider({"allowed_containers": ["qbittorrent"]})
    graph = build_graph(
        config=config,
        providers={"docker": provider},
        provider_settings={"docker": {}},
        policy=policy,
        checkpointer=InMemorySaver(),
        model=fake_model(),
    )
    return graph, config


def bound_tool_names(graph) -> set[str]:
    return set(graph.nodes["tools"].bound.tools_by_name.keys())


def test_graph_binds_workspace_tools_without_execute_or_task(tmp_path: Path):
    graph, _config = build(tmp_path)

    names = bound_tool_names(graph)

    for expected in ["ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep"]:
        assert expected in names
    assert "execute" not in names
    assert "task" not in names


def test_graph_binds_provider_tools(tmp_path: Path):
    graph, _config = build(tmp_path)

    names = bound_tool_names(graph)

    assert "docker_list_containers" in names
    assert "docker_restart_container" in names


def test_denied_tool_is_never_bound(tmp_path: Path):
    graph, _config = build(
        tmp_path, overrides={"docker.get_container_logs": PolicyDecision.DENY}
    )

    assert "docker_get_container_logs" not in bound_tool_names(graph)


def test_creates_default_agents_md_when_missing(tmp_path: Path):
    _graph, config = build(tmp_path)

    agents_md = Path(config.workspace_root) / "AGENTS.md"
    assert agents_md.is_file()
    assert "Argus Agent memory" in agents_md.read_text()


def test_does_not_overwrite_existing_agents_md(tmp_path: Path):
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True)
    (workspace_root / "AGENTS.md").write_text("custom memory content")

    build(tmp_path)

    assert (workspace_root / "AGENTS.md").read_text() == "custom memory content"


def test_creates_skills_directory(tmp_path: Path):
    _graph, config = build(tmp_path)

    assert (Path(config.workspace_root) / "skills").is_dir()


def test_build_model_points_at_openrouter():
    config = AgentConfig(model="anthropic/claude-sonnet-4.5", api_key="sk-or-fake")

    model = _build_model(config)

    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "anthropic/claude-sonnet-4.5"
    assert str(model.openai_api_base) == "https://openrouter.ai/api/v1"


def test_build_model_resolves_as_openai_provider():
    """Guards the assumption graph.py's harness-profile key ("openai") relies on."""
    config = AgentConfig(model="google/gemini-3-pro", api_key="sk-or-fake")

    model = _build_model(config)

    assert get_model_provider(model) == "openai"

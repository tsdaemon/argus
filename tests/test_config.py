from __future__ import annotations

from pathlib import Path

import pytest

from argus.config import AgentConfig, load_config
from argus.policy import PolicyDecision


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "argus.yaml"
    path.write_text(text)
    return path


def test_env_var_is_expanded_in_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARGUS_TEST_TOPIC", "https://ntfy.sh/my-secret-topic")
    path = write_config(
        tmp_path,
        """
        providers:
          breakglass:
            enabled: true
            notify:
              type: ntfy
              topic_url: ${ARGUS_TEST_TOPIC}
        """,
    )

    config = load_config(path)

    assert config.providers["breakglass"].settings()["notify"]["topic_url"] == (
        "https://ntfy.sh/my-secret-topic"
    )


def test_dollar_brace_in_a_comment_does_not_require_the_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("ARGUS_NEVER_SET_THIS_ONE", raising=False)
    path = write_config(
        tmp_path,
        """
        # A ${ARGUS_NEVER_SET_THIS_ONE}-style reference would be expanded in real values.
        providers: {}
        """,
    )

    # Must not raise: the reference only appears in a comment, which yaml.safe_load
    # already drops before expansion ever sees it.
    load_config(path)


def test_missing_env_var_in_an_actual_value_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ARGUS_NEVER_SET_THIS_ONE", raising=False)
    path = write_config(
        tmp_path,
        """
        providers:
          breakglass:
            enabled: true
            notify:
              type: ntfy
              topic_url: ${ARGUS_NEVER_SET_THIS_ONE}
        """,
    )

    with pytest.raises(ValueError, match="ARGUS_NEVER_SET_THIS_ONE"):
        load_config(path)


def test_defaults_and_overrides_parse_into_policy_decisions(tmp_path: Path):
    path = write_config(
        tmp_path,
        """
        policy:
          default_mutate: allow
          overrides:
            docker.restart_container: require_approval
        """,
    )

    config = load_config(path)

    assert config.policy.default_mutate is PolicyDecision.ALLOW
    assert config.policy.overrides["docker.restart_container"] is PolicyDecision.REQUIRE_APPROVAL


def test_agent_config_defaults_when_section_omitted(tmp_path: Path):
    path = write_config(tmp_path, "providers: {}\n")

    config = load_config(path)

    assert config.agent == AgentConfig()


def test_agent_config_section_overrides_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = write_config(
        tmp_path,
        """
        agent:
          workspace_root: /data/agent-workspace
          database_url: postgresql://argus:argus@postgres:5432/argus
          model: openai/gpt-5.2
          api_key: ${ARGUS_TEST_OPENROUTER_KEY}
          otel:
            enabled: true
            endpoint: http://phoenix:4317
        """,
    )
    monkeypatch.setenv("ARGUS_TEST_OPENROUTER_KEY", "sk-or-fake")

    config = load_config(path)

    assert config.agent.workspace_root == "/data/agent-workspace"
    assert config.agent.model == "openai/gpt-5.2"
    assert config.agent.api_key == "sk-or-fake"
    assert config.agent.otel.enabled is True
    assert config.agent.otel.endpoint == "http://phoenix:4317"

"""Config schema and loader for an Argus deployment.

A config file says which providers are enabled and how each is set up, plus the policy
defaults/overrides the `PolicyEngine` enforces. See `examples/argus.example.yaml` for the
full annotated shape and `examples/theseus.argus.yaml` for a real deployment.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from argus.policy import PolicyDecision

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ProviderEntry(BaseModel):
    """One provider's config block. `enabled` is ours; everything else is the
    provider's own settings, passed through untouched."""

    model_config = ConfigDict(extra="allow")

    enabled: bool = False

    def settings(self) -> dict[str, Any]:
        return self.model_dump(exclude={"enabled"})


class PolicyConfig(BaseModel):
    default_mutate: PolicyDecision = PolicyDecision.REQUIRE_APPROVAL
    default_destructive: PolicyDecision = PolicyDecision.DENY
    overrides: dict[str, PolicyDecision] = {}


class OtelConfig(BaseModel):
    """Tracing must never be on the correctness path — see `argus.agent.tracing`."""

    enabled: bool = False
    endpoint: str = "http://localhost:4317"
    project_name: str = "argus-agent"


class AgentConfig(BaseModel):
    """Settings for the LangGraph-based interactive agent harness (the `create_app` server)."""

    workspace_root: str = "/var/lib/argus/agent-workspace"
    database_url: str = "postgresql://argus:argus@localhost:5432/argus"
    # Model access goes through OpenRouter (one key routes to any upstream model).
    # `model` is OpenRouter's `<provider>/<model>` id; `api_key` should be set via
    # `${OPENROUTER_API_KEY}` in the YAML config, never hardcoded. `worker_model` is a
    # cheaper model for the "worker" subagent's routine/menial tool-calling delegation
    # — see `argus.agent.graph` — kept separate from `model` (planning/self-reflection).
    model: str = "anthropic/claude-sonnet-4.5"
    worker_model: str = "anthropic/claude-haiku-4.5"
    model_base_url: str = "https://openrouter.ai/api/v1"
    api_key: str = ""
    otel: OtelConfig = OtelConfig()


class ArgusConfig(BaseModel):
    providers: dict[str, ProviderEntry] = {}
    policy: PolicyConfig = PolicyConfig()
    agent: AgentConfig = AgentConfig()


def load_config(path: str | Path) -> ArgusConfig:
    """Load and validate a YAML config file, expanding `${ENV_VAR}` references in string
    values so secrets (tokens, topic URLs, ...) never need to be committed to the file.

    Expansion happens after YAML parsing, on string values only — not on the raw file
    text — so a `${...}` mentioned in a comment (e.g. documenting this very feature) is
    never mistaken for a reference to expand.
    """
    data = yaml.safe_load(Path(path).read_text()) or {}
    return ArgusConfig.model_validate(_expand_env(data))


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            resolved = os.environ.get(name)
            if resolved is None:
                raise ValueError(
                    f"Config references ${{{name}}}, but that environment variable is not set."
                )
            return resolved

        return _ENV_VAR_PATTERN.sub(replace, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value

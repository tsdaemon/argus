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


class ArgusConfig(BaseModel):
    providers: dict[str, ProviderEntry] = {}
    policy: PolicyConfig = PolicyConfig()


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

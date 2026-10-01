"""The agent's durable knowledge in its workspace: conventions in `AGENTS.md`, facts in
`memory/`, one topic per file.

`AGENTS.md` is the day-0 rulebook: the operator's standing instructions. What the agent
learns about the home goes into topic files under `memory/`, each listed on one line of
`memory/INDEX.md`. Both `AGENTS.md` and the index are loaded into every conversation;
the fact files are read when their index line is relevant. The rules for this live here, in
argus code, so the agent cannot edit them away; they replace deepagents' generic memory
prompt. Procedures belong in `skills/`, which deepagents' skills middleware loads.
"""

from __future__ import annotations

from pathlib import Path

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.memory import MemoryMiddleware

SOURCES = ["AGENTS.md", "memory/INDEX.md"]

MEMORY_PROMPT = """<agent_memory>
{agent_memory}
</agent_memory>

<memory_rules>
The files above come from your workspace. They may be outdated or wrong: when they disagree
with the operator or with what your tools show now, trust the operator and the tools.

AGENTS.md holds conventions: the operator's standing instructions on how to work. Follow
them. Edit AGENTS.md only when the operator asks you to, or tells you how you should work
from now on. Never put facts about the environment there.

memory/ holds facts, one topic per file. What you learn about the home (the network and its
hosts, a service, the operator's preferences, an incident and what fixed it) goes into the
file for its topic; add a fact to an existing topic file before starting a new one:
- Path: memory/<topic>.md, with a short kebab-case name (network.md, nas.md, operator.md).
- Start each file with:
  ---
  title: <one line>
  type: host | network | service | operator | incident
  updated: <YYYY-MM-DD>
  ---
  then the facts, each with how you know it (the command that showed it, or "operator said").
- Keep memory/INDEX.md in step: one line per file, `- [title](file.md): when it matters`.
  The index is loaded every conversation and the files are not, so read a file when its
  line is relevant.
- Before writing, look for a file that already covers it and update that instead. When a
  fact turns out wrong, fix or delete its file and its index line.

Save stable facts the moment you confirm them, and what the operator tells you about the
home or about their preferences. Do not save what your tool descriptions already say,
transient state (current load, a container's status right now), one-off answers, or any
secret. Write step-by-step procedures as skills under skills/, not as memory. Facts drift:
before acting on one updated more than a few weeks ago, re-check it with a read command.
</memory_rules>
"""

DEFAULT_AGENTS_MD = """# Conventions

Standing instructions for argus. Facts about the home live in memory/ (see memory/INDEX.md),
not here.

- Investigate with read commands first. Propose a change together with the evidence for it.
- When it is unclear which host or device something refers to, check memory/, then ask.
"""

DEFAULT_INDEX_MD = """# Memory index

One line per file in memory/: `- [title](file.md): when it matters`.
"""


def seed_workspace(workspace_root: Path) -> None:
    """Create the conventions file and the memory index if they are missing; never overwrite."""
    (workspace_root / "memory").mkdir(exist_ok=True)
    for path, content in (
        (workspace_root / "AGENTS.md", DEFAULT_AGENTS_MD),
        (workspace_root / "memory" / "INDEX.md", DEFAULT_INDEX_MD),
    ):
        if not path.exists():
            path.write_text(content)


def memory_middleware(backend: BackendProtocol) -> MemoryMiddleware:
    return MemoryMiddleware(
        backend=backend, sources=SOURCES, add_cache_control=True, system_prompt=MEMORY_PROMPT
    )

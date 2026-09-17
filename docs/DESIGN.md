# Argus Agent — Design & Requirements

Status: living document, updated as decisions change. See [`argus-agent-v0.md`](argus-agent-v0.md)
for execution status against this design.

## Goal

Deliver a self-hosted agent harness for Theseus (the home NAS) that can run interactive
diagnostics, persist its execution/history, safely invoke constrained tools, expose
traces, and provide a deliberately separate Claude Code break-glass path.

Argus is not a greenfield agent application — it started as a standalone, policy-gated
MCP server (`argus.mcp`, see the root `README.md`/`AGENTS.md`). This design evolves that
codebase into the larger Argus system: **Argus Agent is the thing that always runs.**
MCP is not a separate, independently-deployed pillar to preserve — it's an optional
interface the always-running agent process can also expose (toggled by whether
`ARGUS_MCP_TOKEN` is set, see [API / deployment shape](#api--deployment-shape)), for
external MCP clients (Hermes, Claude Code) that want to talk to the same provider tools
directly. `argus.mcp` stays a real, separately testable module because that boundary is
useful internally, not because standalone MCP-only deployment is a goal.

**Design constraint that overrides all others below**: this stays a small, understandable
harness for a single-operator homelab — not another Hermes/OpenClaw-sized platform. The
point is to genuinely use it *and* to learn the real engineering problems it exercises:
context construction, memory, skills, checkpoints/resumption, durable execution, tool
execution, permissions/HITL, tracing, cron jobs, dynamic UI integration, and artifacts.
Prefer reusing an existing, maintained piece over hand-rolling one, but never at the cost
of hiding what's actually happening.

## Components

- **Argus Agent** (`src/argus/agent/`) — the LangGraph-based agent implementation itself:
  the graph, tool binding, and the AG-UI wrapper. A first-party *consumer* of the same
  provider tools MCP exposes, bound directly in-process — not a separate client talking
  to an MCP server. Builds the agent; doesn't serve it or own storage — those are
  siblings, not part of this package.
- **Argus API** (`src/argus/api/`) — the FastAPI HTTP layer: routing, mounting MCP, the
  one thing that runs (`argus agent serve`). The default `docker-compose.yml`/`Dockerfile`
  ship it.
- **Argus DB** (`src/argus/db/`) — Postgres persistence: LangGraph's own checkpoints
  (`checkpointer.py`) and Argus-owned history tables (`repo.py`, schema via Alembic).
- **Argus MCP** (`src/argus/mcp/`) — the pre-existing controlled interface to the
  homelab/external systems (currently: `docker`), exposed as an *optional interface of
  the same running agent process* (see [API / deployment shape](#api--deployment-shape)),
  not a separate deployment.
- **Shared** (`src/argus/policy.py`, `config.py`, `providers/`, `approval/` protocol,
  `launcher/`, `notify/`, `webauth.py`) — transport-agnostic code every other package
  depends on. None of `agent/`, `api/`, `db/`, `mcp/` depend on each other except where
  explicitly noted (`api/` composes `agent/` + `db/` + `mcp/`).

## Trust boundaries

- **MCP-facing tools** (`argus.providers`, e.g. `docker.*`) — access to systems outside
  the agent itself. Classified `READ`/`MUTATE`/`DESTRUCTIVE` via `PolicyEngine`, same
  classification enforced identically whether bound to MCP (`mcp_bind`, real MCP
  elicitation for approval) or to the agent (`argus.agent.tools.langchain_bind`, LangGraph
  `interrupt_on` for approval — see [Permissions/HITL](#permissionshitl)). One
  implementation, two bindings, never two implementations.
- **Agent's private workspace** (Markdown memory + skills) — the agent's own knowledge,
  not an external system. Deliberately **never** an MCP tool: exposing it over MCP would
  let any MCP client mutate the agent's memory, collapsing this boundary with the one
  above. Provided by `deepagents`' `FilesystemMiddleware`/`SkillsMiddleware`/
  `MemoryMiddleware` (see [Workspace & skills](#workspace--skills)), always auto-run, never
  gated by `PolicyEngine`.
- **Native external knowledge sources** (planned: Notion — see the ledger) — read-only
  reference material the agent draws on, conceptually grouped with memory rather than
  with MCP-facing tools even though the source itself isn't agent-owned. Not a
  `Provider`/`ToolSpec`, never exposed over `/mcp`: there's no mutation risk to gate
  (read-only) and no reason an external MCP client should be able to read the user's
  Notion workspace just because it can call `docker.*`. A native, agent-only tool, same
  treatment as the workspace above.
- **Break-glass** — deliberately *outside* both boundaries above. A human action (from the
  Argus Agent UI or the existing `/breakglass`/`/launch` web view — the same page, not two
  implementations) that launches a real, unconstrained Claude Code session on the target
  host via `argus.launcher`. Never agent-invocable. `sudo` stays password-gated on the
  target host; no `NOPASSWD`.

## Concepts kept separate

- **State** — LangGraph checkpoints (Postgres, via `langgraph-checkpoint-postgres`,
  entirely managed by that package — `argus.db.checkpointer`).
- **History** — raw chat/event/tool-call log, Argus-owned Postgres tables (`threads`,
  `runs`, `messages`, `tool_calls`, `approvals` — `argus.db`, schema via Alembic).
- **Explicit memory** — agent-managed Markdown in the workspace (`AGENTS.md`, notes,
  runbooks), primary and human-editable. No vector index for now — grep/progressive-
  disclosure only (via `deepagents`), with the seam left open for a Mem0 or pgvector
  backend later (**explicitly not built now**).
- **Artifacts** — future durable run outputs (reports, investigations). Only a schema
  seam (`runs` table) reserved; not built.

## Tool binding architecture

A provider (`argus.providers.base.Provider`) exposes `tool_specs(config) ->
list[ToolSpec]` — transport-agnostic classification + implementation. Two bindings
consume the same specs:

- `mcp_bind(mcp, policy, specs)` — MCP-facing, unchanged existing behavior
  (`PolicyEngine.register`, real MCP elicitation for `REQUIRE_APPROVAL`).
- `argus.agent.tools.langchain_bind(policy, specs)` — agent-facing. Uses
  `PolicyEngine.decide()` (transport-agnostic) for the same classification; a
  `REQUIRE_APPROVAL` decision becomes an `interrupt_on` entry for deepagents'/LangChain's
  `HumanInTheLoopMiddleware`, not MCP elicitation. `DENY` means the tool never exists in
  either tool list, not just refused at call time — same invariant on both bindings.

The `breakglass` provider is bound to MCP only, never to the agent (see Trust boundaries).

## Workspace & skills

Provided by `deepagents` (`create_deep_agent`), not hand-rolled:

- `FilesystemBackend` + a restricted `FilesystemMiddleware` (`tools=["ls", "read_file",
  "write_file", "edit_file", "delete", "glob", "grep"]`, deliberately excluding `execute`
  — no arbitrary shell) rooted at `AgentConfig.workspace_root`. The default general-purpose
  subagent is disabled via `HarnessProfile`; `task` delegates to the fixed worker described
  under Model access.
- `SkillsMiddleware` (`skills=["skills"]`, relative to the workspace root) — progressive
  disclosure: name/description at startup, full `SKILL.md` on activation via `read_file`.
  Skill install/edit is just `write_file`/`edit_file` under `skills/<name>/SKILL.md` — no
  separate tool needed.
- `MemoryMiddleware` (`memory=["AGENTS.md"]`) — loads the workspace's `AGENTS.md` into
  every run's system prompt. A default file is created if missing.

`create_deep_agent(...)` returns a plain `langgraph.graph.state.CompiledStateGraph` —
LangGraph stays fully visible (checkpointer wiring, `.ainvoke()`, `Command(resume=...)`
are all ours), `deepagents` only supplies the workspace/skills/memory layer and the base
tool set.

**Installing external skills** (in v0 scope): `AgentConfig.skill_sources: list[str]`,
entries shaped `"owner/repo"` or `"owner/repo/path/to/skill"` for a specific
subdirectory — the same shorthand the community `npx skills add owner/repo` tool
([vercel-labs/skills](https://github.com/vercel-labs/skills),
[antfu/skills-cli](https://github.com/antfu/skills-cli)) uses, so a skill shared as
"install `owner/repo`" for that ecosystem installs into Argus the same way. Resolved
at `argus agent serve` startup (idempotent — safe to re-run every boot, like the
checkpointer's own `.setup()`): fetch the repo (no `npx`/Node dependency — a pure-Python
fetch, e.g. GitHub's tarball/codeload endpoint or the contents API), find the matched
`SKILL.md` (+ its directory), write into `workspace/skills/<name>/`. This is why
installable skills matter architecturally, not just as a convenience: Argus's long-term
scope is many heterogeneous home systems (Home Assistant, routers, dashboards — see the
ledger), and a skill carrying domain knowledge is a cheaper way to teach the agent about
a new system than hand-writing a new `Provider` class for each one. Not yet implemented
— see the ledger.

## Model access

Through **OpenRouter**, not a single native provider SDK — `AgentConfig.model` is
OpenRouter's `<provider>/<model>` id (default: `anthropic/claude-sonnet-4.5`),
`AgentConfig.api_key` set via `${OPENROUTER_API_KEY}`. One `ChatOpenAI` client (pointed
at OpenRouter's endpoint) is used regardless of the upstream model — switching models is
a config change, not a code change.

Non-obvious: any `ChatOpenAI` instance resolves as provider `"openai"` to
`deepagents`'/LangChain's harness-profile system, regardless of `base_url` or upstream
model (verified directly, locked in by
`tests/agent/test_graph.py::test_build_model_resolves_as_openai_provider`) — the
harness-profile registration below is keyed `"openai"`, not `"anthropic"` or
`"openrouter"`, for exactly this reason.

**Two models, not one**: `AgentConfig.model` (default `anthropic/claude-sonnet-4.5`) runs
the main agent — planning, synthesis, self-reflection, deciding whether an action needs
approval. `AgentConfig.worker_model` (default `anthropic/claude-haiku-4.5`) runs one
fixed "worker" `SubAgent`, delegated to via the `task` tool for routine/read-heavy
tool-calling work, to save cost. This is one purpose-built worker, not a generic
pluggable multi-agent framework — deepagents' own default "general purpose subagent"
stays disabled via the registered `HarnessProfile`; only the explicit worker is
registered, so `task` exists but only ever delegates to that one subagent.

## Permissions/HITL

`PolicyEngine` (shared, `argus.policy`) is the single source of classification truth.
`approval_backend` is optional — the agent's own `PolicyEngine` instance only ever calls
`.decide()`, never `.register()`, so it needs no `ApprovalBackend` at all.

- MCP path: `ElicitApproval`, real MCP elicitation, fails closed on protocol mismatch.
- Agent path: LangGraph's own `interrupt()`, via deepagents'/LangChain's
  `HumanInTheLoopMiddleware` (`interrupt_on={tool_name: InterruptOnConfig(...)}`).
  `argus.agent.api.ArgusAgent` enables the native AG-UI interrupt outcome and disables the
  legacy `on_interrupt` custom event. The paused run ends with
  `RUN_FINISHED.outcome = {"type": "interrupt", "interrupts": [...]}`. Each interrupt has
  an ID and preserves the HITL `action_requests`/`review_configs` in
  `metadata.langgraph.raw` for the frontend to render.

To answer, POST another `RunAgentInput` to `/agent` using the same `threadId`, a new
`runId`, and the standard `resume` array. For example, the approval portion is:

```json
{
  "resume": [{
    "interruptId": "<id from the interrupt outcome>",
    "status": "resolved",
    "payload": {"decisions": [{"type": "approve"}]}
  }]
}
```

Supply one `approve` or `reject` decision for each action in that interrupt, in the
displayed order. Multiple interrupts (including parallel workers) are matched by ID,
independently of response order. `status: "cancelled"` rejects every action in the
specified interrupt, ignoring any attached payload. A request without a resume response
re-emits the pending interrupt without executing the operation.

The adapter validates interrupt IDs and decisions before constructing LangGraph's
`Command(resume={interrupt_id: {"decisions": [...]}})`. Stale/duplicate IDs and malformed
decisions produce a native `RUN_ERROR` with code `INVALID_APPROVAL`; the pending
checkpoint remains available for a corrected response. Legacy
`forwardedProps.command` input is rejected so it cannot bypass ID validation.

These HTTP/graph semantics are covered by `tests/api/test_approvals.py`, including a
listening Uvicorn server. Models and Docker are faked in those checks; real OpenRouter
and target-host operations remain separate integration work.

`docker.restart_container` is the one MUTATE tool exercising this end-to-end for the MVP.

## Notifications (planned, not designed in detail)

Severity-tiered, distinct from the HITL approval channel above: a SEV0 issue calls the
user's phone, SEV1 pushes via ntfy (the existing `argus.notify.NtfyNotifier`, currently
used only for break-glass), SEV2 sends an email. Not yet decided: what actually assigns
a severity (the agent itself? a fixed per-alert-type mapping?), which telephony/email
services back SEV0/SEV2 (neither exists in the codebase yet — `notify` only has ntfy),
and whether this becomes a general `argus.notify` capability the agent itself can invoke
(e.g. a classified tool) versus staying break-glass-specific. Needs a real design pass
before implementing — see the ledger.

## Persistence

One Postgres instance, two categories of tables, never mixed:

- **LangGraph-owned** (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`,
  `checkpoint_migrations`) — created/managed entirely by `langgraph-checkpoint-postgres`
  (`AsyncPostgresSaver.setup()`), never hand-edited.
- **Argus-owned** (`threads`, `runs`, `messages`, `tool_calls`, `approvals`) — schema via
  **Alembic** (`alembic.ini` + `src/argus/db/migrations/`), plain functions over
  them in `argus.db.repo` (no ORM, mirroring the existing sqlite `BreakGlassStore`
  style). `alembic upgrade head` is a separate, explicit step — not run automatically at
  agent startup (unlike the LangGraph checkpointer's own `.setup()`, which is idempotent
  and safe to run every boot; schema migrations are not, by convention).

## API / deployment shape

One process, `argus agent serve` (`src/argus/api/app.py`):

- `/agent` — AG-UI endpoint (`ag_ui_langgraph.LangGraphAgent` +
  `add_langgraph_fastapi_endpoint`), the primary interface for the React frontend.
- `/agent/health` — plain health check.
- `/mcp` + `/breakglass` (+ `/login`, `/launch`) — mounted from the exact same
  `argus.mcp.server.build_http_app()` a standalone `argus serve` would use, **if**
  `ARGUS_MCP_TOKEN` is set. The agent UI's break-glass action is a link to this existing
  `/launch` page — not a second implementation.

Non-obvious ordering constraint (locked in by
`tests/agent/test_api_app.py`): the AG-UI routes must be added to the FastAPI app
*before* the MCP Starlette app is mounted at `/` — a root `Mount` can swallow a request
that only partially matches an earlier route (right path, wrong method), and FastMCP's
mounted app needs its own `lifespan` forwarded into the parent `FastAPI(...)`
constructor or every `/mcp` request 500s (an internal task group never starts otherwise).

Default `docker-compose.yml`/`Dockerfile` ship Postgres + Phoenix + the agent as one
stack — not an opt-in overlay. LangGraph/deepagents/FastAPI/Postgres drivers are base
`pyproject.toml` dependencies, not an optional extra — there's no "MCP-only install" to
keep light for, since Argus Agent is the thing that always runs.

## Interfaces

Three real interfaces, all committed for v0, each with a distinct caller:

- **AG-UI** — the human-facing interface, and the primary way to interact with Argus
  Agent. Backend: `/agent` (see [API / deployment shape](#api--deployment-shape)).
  Frontend: a **React app** (not yet built — `frontend/`, see the ledger) using
  `@ag-ui/client` to start/continue runs, stream agent/tool activity, and render the
  HITL approve/reject flow from the native interrupt outcome (see
  [Permissions/HITL](#permissionshitl)).
- **MCP** (`/mcp`) — the interface for existing MCP clients (Hermes, Claude Code) to
  call the same provider tools (`docker.*`) Argus Agent itself uses, bound via the
  MCP-facing side of [tool binding](#tool-binding-architecture) rather than the
  agent-facing side. Fully real and already working (see the ledger) — not a legacy
  leftover kept only for compatibility.
- **A2A (Agent2Agent)** — the interface for *other agents* to call into Argus Agent
  itself as a callable peer (not its tools — the whole agent), distinct from MCP
  (Argus's own tools, outbound-shaped) and from AG-UI (human-facing). Not yet designed
  — see the ledger.

## Observability

OpenTelemetry + OpenInference → Phoenix (`arize-phoenix-otel`,
`openinference-instrumentation-langchain`). Must never be on the correctness path —
`AgentConfig.otel.enabled` defaults to `false`; a tracing setup failure must not break a
run. **Not yet implemented** — see the ledger.

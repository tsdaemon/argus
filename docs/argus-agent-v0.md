# Argus Agent v0 — execution ledger

Tracks the refactor from a standalone MCP server into Argus Agent + Argus MCP. See
[`DESIGN.md`](DESIGN.md) for the design/requirements this
executes against. Update this file as work lands — it's a status log, not a plan;
finished phases stay here as a record of what actually happened, including reversals.

Legend: `[x]` done and verified · `[~]` done but unverified/partial · `[ ]` not started.

## Acceptance criteria (current status)

- `[x]` Interactive LangGraph agent can run and resume from persisted state — verified
      against real Postgres (`tests/agent/test_checkpointer.py`).
- `[ ]` Minimal AG-UI web interface can start/continue a run and display agent/tool
      activity — backend endpoint verified live; no frontend yet.
- `[x]` Agent can invoke read-only Theseus diagnostics through constrained Argus tools —
      `docker.*` bound via `langchain_bind`.
- `[x]` Agent has native, non-MCP private-workspace tools (list/read/create/edit/delete +
      grep) — via `deepagents`' `FilesystemMiddleware`.
- `[~]` Index over Markdown memory "beyond plain grep" — descoped for v0 to grep/
      progressive disclosure by explicit decision; seam left for Mem0/vector later.
- `[x]` Skills as a native harness concept (list/discover, read, install, edit) — via
      `deepagents`' `SkillsMiddleware` + generic `write_file`/`edit_file`.
- `[x]` Deterministic permission metadata + one real HITL approval flow — `docker
      .restart_container` via `interrupt_on`, verified structurally
      (`tests/agent/test_tools.py`, `test_graph.py`); not yet exercised through a live
      AG-UI round-trip.
- `[x]` PostgreSQL persists checkpoints — verified live. Raw history/tool-calls/approvals:
      schema + repo functions exist and are tested; **not yet wired into a live run**.
- `[x]` Explicit Markdown memory loadable/modifiable by the agent itself, including
      `AGENTS.md` — via `deepagents`' memory middleware.
- `[ ]` Traces visible in Phoenix via OTel/OpenInference — not started.
- `[ ]` Break-glass action launches Claude Code on Theseus from the Agent UI without
      NOPASSWD sudo — the mechanism is proven (existing, tested `argus.launcher` +
      `/launch`, now reachable from the same process as the agent); no frontend link yet.
- `[ ]` A2A interface lets another agent call into Argus Agent — not started, but in v0
      scope (see design doc's Interfaces section).

## Explicitly out of scope for v0

Mem0/learned memory, vector/semantic search over workspace Markdown (grep-only for now,
seam left open), scheduled health runs, A2UI, self-hosted LangSmith, generic multi-agent
support, chat gateways (Telegram/Discord), broad multi-SDK provider abstraction (OpenRouter
is the one abstraction used, and it's a config value, not a code path per provider),
Temporal, unrestricted shell/root access (`execute` tool deliberately excluded).

**Considered, deliberately not adopted now** (see `AGENTS.md` for the fuller note):
NVIDIA OpenShell (sandboxed agent runtime) — no current use case needs it; revisit if
break-glass hardening or an agent-side `execute` capability is ever pursued.

## Phase 1 — Restructure into `mcp/` + shared

`[x]` `src/argus/{server,webapp,auth}.py` + `approval/elicit.py` → `src/argus/mcp/`.
Shared code (`policy.py`, `config.py`, `providers/`, `approval/` protocol, `launcher/`,
`notify/`, `webauth.py`) stays top-level. 70/70 existing tests green throughout, zero
test changes needed except two import-path fixes.

## Phase 2 — Tool dual-binding

`[x]` `ToolSpec` + `mcp_bind()` added to `providers/base.py`. `docker_provider.py` and
`breakglass_provider.py` split into `tool_specs()` + one-line `register()`. New test
coverage for `breakglass_provider` (previously untested at the tool-call level).

## Phase 3 — `agent/` skeleton + dependencies

`[x]` `agent` extra added to `pyproject.toml`, resolved against real PyPI (not
guessed): `langgraph`, `langchain-core`, `langchain-anthropic` (later unused, see
below), `langgraph-checkpoint-postgres`, `psycopg[binary]`, `fastapi`, `ag-ui-protocol`,
`ag-ui-langgraph`, `openinference-instrumentation-langchain`, `arize-phoenix-otel`,
`opentelemetry-sdk`/`-exporter-otlp`. `AgentConfig` added to `config.py`. Base install
confirmed unaffected (lazy imports only).

## Phase 4 — Workspace/skills: built, then reversed in favor of `deepagents`

`[x]` reversed. Originally hand-built `agent/workspace/{fs,search}.py` +
`agent/skills/store.py` (path-traversal-safe file ops, grep search, `SKILL.md`
lifecycle) with full test coverage. **Discarded** after finding `deepagents` (LangChain's
package) already ships `FilesystemMiddleware`/`SkillsMiddleware`/`MemoryMiddleware`
covering the same ground, battle-tested, matching the open "Agent Skills" standard other
tools speak. Decision: take deepagents as much as possible, keep LangGraph itself
visible (confirmed `create_deep_agent()` returns a plain `CompiledStateGraph`).

## Phase 5 — Tool binding + graph + checkpointer

`[x]` `agent/tools.py::langchain_bind` — provider `ToolSpec`s → LangChain
`StructuredTool`s via `PolicyEngine.decide()`; `REQUIRE_APPROVAL` → deepagents'/
LangChain's `interrupt_on` (`HumanInTheLoopMiddleware`), not a hand-rolled `interrupt()`
gate (verified the real `interrupt(HITLRequest)`/`Command(resume={"decisions": [...]})`
shape by reading `HumanInTheLoopMiddleware.after_model` source directly).

`[x]` `agent/graph.py::build_graph` — `deepagents.create_deep_agent()`, restricted
`FilesystemMiddleware` (`execute`/`task` excluded — `task` requires disabling deepagents'
default "general purpose subagent" via a registered `HarnessProfile`, empirically
determined since `excluded_tools` on the profile does *not* remove built-in filesystem
tools, only a custom `FilesystemMiddleware(tools=[...])` does).

`[x]` **Mid-course model correction**: initially wired for native Anthropic
(`model="anthropic:claude-sonnet-5"`). Corrected to route through **OpenRouter**
(`ChatOpenAI` pointed at OpenRouter's endpoint) per explicit direction — "any model,"
not pinned to Anthropic. Discovered any `ChatOpenAI` instance resolves as provider
`"openai"` to deepagents' harness-profile matching regardless of `base_url`/upstream
model — the `task`-exclusion profile is keyed `"openai"`, not `"anthropic"`, for this
reason. Locked in by `test_build_model_resolves_as_openai_provider`.

`[x]` `agent/checkpointer.py` — Postgres-backed LangGraph state.
**Verified against a real Postgres container** (not mocked): built a graph, ran a turn,
tore everything down, built a *fresh* checkpointer + graph instance, and confirmed the
prior thread's message history resumed. This is MVP acceptance criterion #1, proven.

`[x]` AG-UI compatibility spike: confirmed `ag_ui_langgraph.LangGraphAgent(graph=...)` +
`add_langgraph_fastapi_endpoint` consume a deepagents `CompiledStateGraph` directly, no
adapter needed, and that LangGraph `interrupt()` payloads map onto AG-UI's native
`Interrupt` wire event automatically (`lg_interrupt_to_agui`), with the raw payload
preserved in `metadata.langgraph.raw` for custom frontend rendering. De-risks what the
original plan flagged as the main uncertainty in this stack.

## Phase 6 — Postgres history schema + API layer

`[x]` `PolicyEngine.approval_backend` made optional (the agent's own instance only
calls `.decide()`). `PROVIDER_REGISTRY` extracted from `argus.mcp.server` into shared
`argus.providers.registry` (both MCP and agent instantiate providers the same way).

`[x]` History schema switched from raw idempotent DDL to **Alembic**
(`alembic.ini` + `src/argus/agent/db/migrations/`) per explicit request. Initial
revision (`threads`, `runs`, `messages`, `tool_calls`, `approvals`) applied against real
Postgres and verified alongside LangGraph's own checkpoint tables (10 tables total, no
collisions). `agent/db/repo.py` — plain async functions over them (no ORM), all 6 CRUD
paths tested against real Postgres.

`[x]` `agent/api/app.py` — combined FastAPI app. Two real bugs found and fixed via live
testing (not just unit tests):
1. A root `Mount("/", mcp_app)` can swallow a request that only *partially* matches an
   earlier route (right path, wrong method) — Starlette prefers a later full match over
   an earlier partial one. Fix: register `/agent` routes before mounting MCP, always.
2. FastMCP's mounted app needs its own `lifespan` forwarded into the parent
   `FastAPI(...)` constructor, or every `/mcp` request 500s (internal task group never
   starts). Fix: build the MCP sub-app before constructing `FastAPI(...)` so its
   `.lifespan` can be passed in, even though it's mounted later.

Both verified live (`curl` against a real running server) and locked in as automated
`TestClient` regression tests (`tests/agent/test_api_app.py`).

`[x]` `argus agent serve` CLI subcommand, lazy-importing the `agent` extra so `argus
serve`/`argus breakglass` stay dependency-free.

## Deployment consolidation (mid-course correction)

`[x]` Initially built `docker-compose.agent.yml` + `Dockerfile.agent` as an opt-in
overlay alongside the existing MCP-only `docker-compose.yml`/`Dockerfile`. **Reversed**
per explicit feedback: the agent is the primary feature, not an add-on. Folded Postgres
+ Phoenix + the `agent` extra into the single default `docker-compose.yml`/`Dockerfile`;
deleted the overlay files. `argus serve` (MCP-only) still works from the same image.

`[x]` **Follow-up correction**: initially left both port 8420 (leftover from the
pre-consolidation MCP-only default) and 8421 (agent) exposed/published, despite `/mcp`
and `/agent` being mounted on the *same* FastAPI app bound to *one* uvicorn port. Fixed:
one port (`AGENT_PORT`, default 8421) in `Dockerfile` `EXPOSE`, `docker-compose.yml`
`ports:`, and `.env.example`; `docker-compose.deploy.yml.example`'s Traefik
`loadbalancer.server.port` label corrected from `8420` to `8421`.

`[~]` `theseus.argus.yaml` simplified: dropped the `allowed_containers` allowlist so all
containers are visible by default (per explicit request) — not yet deployed for real.

`[x]` **Second follow-up correction**: the `agent` extra itself removed. All LangGraph/
deepagents/FastAPI/Postgres/OTel dependencies folded into base `dependencies` in
`pyproject.toml` (previously `[project.optional-dependencies].agent`) — per explicit
feedback, there's no more "MCP-only" install to keep light for. `Dockerfile` no longer
passes `--extra agent` to `uv sync` (it's the only sync now). `cli.py`'s lazy imports in
`agent serve` are kept anyway, purely for `argus serve`/`argus breakglass` startup
latency, not as a hard dependency boundary. Also removed the now-unused
`langchain-anthropic` (dead since the OpenRouter correction above) and renamed the
package from `argus-mcp` to `argus` in `pyproject.toml`.

`[x]` **Framing correction**: "Argus MCP as an independently-deployed, standalone
pillar" was never accurate to how this actually runs — corrected in the design doc.
Argus Agent is the one process that runs (`argus agent serve`); MCP is an optional
interface of that same process, toggled by whether `ARGUS_MCP_TOKEN` is set. `argus
serve` (bare MCP, no agent) still exists as a lower-level building block/testing
surface, not as a deployment story to preserve.

## Package restructuring (mid-course correction)

`[x]` `agent/api/` and `agent/db/` (including `agent/checkpointer.py`) moved out of
`src/argus/agent/` into siblings `src/argus/api/` and `src/argus/db/` — per explicit
feedback, `agent/` should hold only the agent implementation itself (graph, tool
binding), not HTTP serving or persistence. `agent/checkpointer.py` moved into `db/`
alongside `repo.py` since both are Postgres persistence concerns.

`[x]` Split the AG-UI wrapping out of `api/app.py` into a new `agent/api.py`
(`build_agent()`, wraps `build_graph()`'s `CompiledStateGraph` into an
`ag_ui_langgraph.LangGraphAgent`) — that's still "building the agent," not HTTP serving.
`src/argus/api/app.py` now only ever handles FastAPI routing/MCP-mounting, agnostic to
how the agent object it serves was built.

Tests moved to match: `tests/agent/{test_graph,test_tools}.py` (agent implementation),
`tests/api/test_app.py` (HTTP layer), `tests/db/{test_repo,test_checkpointer,conftest}.py`
(persistence). All import paths fixed; 108/108 passing, `alembic current` confirmed
working from the new `src/argus/db/migrations/` location against real Postgres.

## Subagent-based model routing

`[x]` Implemented per explicit request ("we should have [subagents, skills,
summarization] from day 0"). Skills and summarization were already active by default
(`SkillsMiddleware` already wired; `SummarizationMiddleware` is part of `deepagents`'
unconditional base stack — confirmed present in a live traceback even though it adds no
separate graph node, wrapping the model call instead). Subagents was the real gap.

`AgentConfig.worker_model` (default `anthropic/claude-haiku-4.5`) added alongside
`model` (planning). `graph.py` builds one fixed `SubAgent("worker", ...)` with its own
`_build_model(config, config.worker_model)` instance and passes it via `subagents=`.
Verified empirically before implementing: an explicit `SubAgent` still registers the
`task` tool even with deepagents' default general-purpose subagent disabled via our
`HarnessProfile` — the two are independent, so `task` now exists but only ever delegates
to this one fixed worker, not a generic pluggable multi-agent framework. Tests updated:
`test_graph.py` now asserts `task` *is* present (inverted from the original "no task"
assertion, back when subagents were deliberately excluded) and that the planner/worker
models differ.

## Not started

- `[ ]` **History instrumentation**: `db/repo.py` functions exist and are tested, but
  nothing calls them during a live run yet — no `messages`/`tool_calls`/`approvals`
  rows are written by an actual agent turn. Needs a LangChain middleware or callback
  hooked into the graph.
- `[ ]` **OTel/Phoenix wiring** (`agent/tracing.py`, not created). Must be non-fatal to a
  run — not yet designed how.
- `[ ]` **React frontend**. Backend AG-UI endpoint is live and tested; no UI consumes it
  yet. Includes the break-glass "launch" action, which is a design decision already
  made (link to the existing `/launch` page, not a new API) but not yet built.
- `[ ]` **Live HITL round-trip**: `interrupt_on` wiring is verified structurally
  (`tests/agent/test_tools.py`, `test_graph.py`), but no test has actually driven a full
  `interrupt()` → AG-UI event → resume round-trip through the real `/agent` endpoint.
- `[ ]` `alembic upgrade head` isn't run automatically anywhere (by design — see the
  design doc) — needs a documented step in the README/Taskfile once the rest is wired.
- `[ ]` A2A interface — **in v0 scope** (see design doc's new Interfaces section), not
  yet designed. Worth checking LangChain's own
  [Agent Protocol](https://github.com/langchain-ai/agent-protocol) (`runs`/`threads`/
  `store` REST contract, which LangGraph Platform implements a superset of) as a
  candidate foundation rather than inventing something bespoke.
- `[ ]` **Installing external skills** (`AgentConfig.skill_sources`, `owner/repo`
  shorthand) — design decided (see design doc's Workspace & skills section), not yet
  implemented.
- `[ ]` **Notion as a knowledge source**: the agent needs read access to the user's
  Notion workspace as a separate knowledge database (the "Digital Home" inventory the
  repo's `AGENTS.md`/README previously assumed only an *external* MCP client would
  reach — now that Argus Agent itself is "the operating agent," it needs this
  directly). **Corrected design** (was initially proposed as a `Provider`, then
  corrected): not a `Provider`/`ToolSpec`, never exposed over `/mcp` — it's read-only
  reference material, conceptually grouped with the agent's own memory/workspace rather
  than with MCP-facing operational tools. A native, agent-only tool. Not yet designed in
  detail (auth to the user's Notion, search/fetch shape) or implemented.
- `[ ]` **Severity-tiered notifications** (SEV0 phone call, SEV1 ntfy, SEV2 email) — see
  design doc's new Notifications section. No telephony/email integration exists yet;
  what assigns a severity is undecided. Needs a design pass before implementing.

## Known open risks / things to re-verify before calling v0 done

- `ag-ui-langgraph` is an early (`0.0.x`) package. The interrupt-to-AG-UI mapping was
  verified by reading its source, not by driving a real interrupt through the live
  `/agent` endpoint end-to-end with a real model. Do that before relying on it.
- No real `OPENROUTER_API_KEY` has been used against a live model anywhere in this
  work — all graph/API tests use fake chat models. The actual OpenRouter integration
  (auth, rate limits, real tool-calling behavior) is unverified against the real
  service.
- `docker compose up --build` for the full consolidated stack (agent image, Postgres,
  Phoenix) has not been run end-to-end — only `docker compose config` (syntax) and a
  local (non-containerized) `uv run argus agent serve` against a real Postgres
  container.

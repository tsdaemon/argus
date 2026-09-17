# Argus Agent v0 — execution ledger

Tracks the refactor from a standalone MCP server into Argus Agent + Argus MCP. See
[`DESIGN.md`](DESIGN.md) for the design/requirements this
executes against. Update this file as work lands — it's a status log, not a plan;
finished phases and verification logs live in [History](#history), including reversals.
Current working instructions live in [`AGENTS.md`](../AGENTS.md).

Legend: `[x]` done and verified · `[~]` done but unverified/partial · `[ ]` not started.

## Acceptance criteria (current status)

- `[x]` Interactive LangGraph agent can run and resume from persisted state — verified
      against real Postgres (`tests/db/test_checkpointer.py`), using a fake chat model.
- `[ ]` Minimal AG-UI web interface can start/continue a run and display agent/tool
      activity — backend endpoint verified live; no frontend yet.
- `[x]` Agent can invoke constrained read-only provider tools — `docker.*` bound via
      `langchain_bind`; tests mock the Docker SDK, so live Theseus diagnostics remain
      unverified.
- `[x]` Agent has native, non-MCP private-workspace tools (list/read/create/edit/delete +
      grep) — via `deepagents`' `FilesystemMiddleware`.
- `[~]` Index over Markdown memory "beyond plain grep" — descoped for v0 to grep/
      progressive disclosure by explicit decision; seam left for Mem0/vector later.
- `[x]` Skills as a native harness concept (list/discover, read, install, edit) — via
      `deepagents`' `SkillsMiddleware` + generic `write_file`/`edit_file`.
- `[x]` Deterministic permission metadata + a backend HITL approval flow —
      `docker.restart_container` pauses and resumes through `/agent`, including a real
      loopback Uvicorn server (`tests/api/test_approvals.py`). Models and Docker SDK calls
      are faked; a real OpenRouter/frontend/host round-trip remains unverified.
- `[x]` PostgreSQL persists checkpoints — verified live. Raw history/tool-calls/approvals:
      schema + repo functions exist and are tested; **not yet wired into a live run**.
- `[x]` Explicit Markdown memory loadable/modifiable by the agent itself, including
      `AGENTS.md` — via `deepagents`' memory middleware.
- `[ ]` Traces visible in Phoenix via OTel/OpenInference — not started.
- `[ ]` Break-glass action launches Claude Code on Theseus from the Agent UI without
      NOPASSWD sudo — `argus.launcher` and `/launch` exist, but real SSH/Claude Remote
      Control and the frontend link remain unverified/unbuilt.
- `[ ]` A2A interface lets another agent call into Argus Agent — not started, but in v0
      scope (see design doc's Interfaces section).

## Explicitly out of scope for v0

Mem0/learned memory, vector/semantic search over workspace Markdown (grep-only for now,
seam left open), scheduled health runs, A2UI, self-hosted LangSmith, generic multi-agent
support beyond the fixed worker, chat gateways (Telegram/Discord), broad multi-SDK provider
abstraction (OpenRouter is the one abstraction used, and it's a config value, not a code path
per provider),
Temporal, unrestricted shell/root access (`execute` tool deliberately excluded).

Additional sandbox runtimes are not part of v0; see [History](#history) for the OpenShell
evaluation.

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
- `[ ]` A2A interface — **in v0 scope** (see design doc's new Interfaces section), not
  yet designed. Worth checking LangChain's own
  [Agent Protocol](https://github.com/langchain-ai/agent-protocol) (`runs`/`threads`/
  `store` REST contract, which LangGraph Platform implements a superset of) as a
  candidate foundation rather than inventing something bespoke.
- `[ ]` **Installing external skills** (`AgentConfig.skill_sources`, `owner/repo`
  shorthand) — design decided (see design doc's Workspace & skills section), not yet
  implemented.
- `[ ]` **Notion as a knowledge source**: native, agent-only read access to the user's
  "Digital Home" inventory. Never a `Provider`/`ToolSpec` or exposed over `/mcp`.
  Authentication and the search/fetch tool shape are not yet designed or implemented.
- `[ ]` **Severity-tiered notifications** (SEV0 phone call, SEV1 ntfy, SEV2 email) — see
  design doc's new Notifications section. No telephony/email integration exists yet;
  what assigns a severity is undecided. Needs a design pass before implementing.

Provider roadmap beyond the current Docker/break-glass tools: `systemd` (status,
journal, restart), `disk`/SMART, and `network` probes. An asynchronous MCP approval-queue
backend is also unbuilt; it is separate from the agent's LangGraph interrupt/resume flow.
No CI workflow is committed; pytest and ruff are the intended baseline.

## Known open risks / things to re-verify before calling v0 done

- The approval path now has HTTP/graph integration coverage with deterministic models
  and a mocked Docker SDK, including worker delegation and parallel interrupts. A real
  model and the future React UI still need to exercise that same path. The adapter's
  resume hook is an upstream integration point; retain these checks when upgrading it.
- No real `OPENROUTER_API_KEY` has been used against a live model anywhere in this
  work — all graph/API tests use fake chat models. The actual OpenRouter integration
  (auth, rate limits, real tool-calling behavior) is unverified against the real
  service.
- `docker compose up --build` for the full consolidated stack (agent image, Postgres,
  Phoenix) has not been run end-to-end — only `docker compose config` (syntax) and a
  local (non-containerized) `uv run argus agent serve` against a real Postgres
  container.
- `/agent` currently has no application-level authentication. The MCP bearer token
  protects `/mcp` only; resolve agent access control before network deployment.
- `examples/theseus.argus.yaml` has no `agent:` block. Complete its database, model
  credential, and workspace settings before using it for an agent deployment.
- Real Hermes elicitation remains unobserved; the original compatibility investigation
  was documentation-only. The README's stronger "confirmed to work" wording is not live
  verification evidence.
- The Docker provider has not been exercised against a real daemon with real containers.
- Real forced-command SSH and Claude Remote Control remain unverified, including
  noninteractive login/workspace trust and whether the session stays reachable.
- No deployment to Theseus is recorded. Overlay/configuration checks do not establish
  that a service is running there.

## History

This section records implementation milestones, reversals, and checks reported during
prior work. Paths, test counts, and choices in early entries describe that point in time;
later entries can supersede them. Moving these notes here did not rerun the checks.

### Original MCP implementation and project direction

The 2026-09-15 handover described the original MCP-only version as complete, with 70
passing tests, clean ruff output, and two commits pushed to `github.com:tsdaemon/argus`
("First version" and "Logo and readme"). It reported no real deployment and only the
committed deployment-overlay template. These were historical observations, not the status
of the subsequent Argus Agent build.

The project began with an explicit generic OSS constraint. The operator then prioritized
personal usefulness and learning; the provider architecture stayed because it was already
useful and inexpensive to retain. The later agent work follows that smaller scope.

### Original approval and break-glass decisions

- Real MCP elicitation was chosen over an advisory approval tool so the gate runs in
  Python before the provider operation. No agent cooperation is needed to enforce it.
- A combined HTTP service was chosen for remote MCP clients and phone-accessible
  break-glass approval. Stdio remained available for local testing; bearer auth protected
  the HTTP MCP surface without adding OAuth machinery for a single operator.
- Break-glass initially addressed unattended external-agent runs: record context, send
  an ntfy push, and let a human approve from a browser.
- A pre-shared `ARGUS_BREAKGLASS_TOKEN` in URLs was replaced with the generated admin
  account, PBKDF2 password hashing, and signed sessions.
- Using Docker's socket to launch a privileged session was rejected in favor of an SSH
  key restricted to the fixed host-launch script. Session permission mode and TTL came
  from deployment configuration rather than request text.
- A browser terminal was rejected as disproportionate. The intended session interface
  was Claude Remote Control, with `CONTEXT.md` carrying material for human review rather
  than an initial CLI prompt that might conflict with the interactive session lifecycle.

### Original MCP and launcher verification log

These checks predate the consolidated agent stack:

- A real `docker build` and `docker compose up` exercised the MCP-only image/container,
  including `/mcp` 401→200 and break-glass HTTP access checks with curl.
- A running `argus serve` exercised first-visit password generation/reveal, no second
  reveal, wrong-password rejection, successful login, and session cookies.
- `argus-breakglass-launch` ran with a **fake `claude` shell executable**, confirming
  `CONTEXT.md`, timeout wrapping, and a child process surviving launcher exit. This did
  not verify real Claude Remote Control or forced-command SSH.
- `docker compose config` checked the base/Theseus overlay merge: labels, `!reset []`
  removing the development port, and `DEPLOY_` secret overrides.
- An isolated go-task check confirmed `.env` and `.env.deploy` dotenv layering.
  `task --list`, `task test`, and `task lint` were also exercised.

### Phase 1 — Restructure into `mcp/` + shared

`[x]` `src/argus/{server,webapp,auth}.py` + `approval/elicit.py` → `src/argus/mcp/`.
Shared code (`policy.py`, `config.py`, `providers/`, `approval/` protocol, `launcher/`,
`notify/`, `webauth.py`) stays top-level. 70/70 existing tests green throughout, zero
test changes needed except two import-path fixes.

### Phase 2 — Tool dual-binding

`[x]` `ToolSpec` + `mcp_bind()` added to `providers/base.py`. `docker_provider.py` and
`breakglass_provider.py` split into `tool_specs()` + one-line `register()`. New test
coverage for `breakglass_provider` (previously untested at the tool-call level).

### Phase 3 — `agent/` skeleton + dependencies

`[x]` `agent` extra added to `pyproject.toml`, resolved against real PyPI (not
guessed): `langgraph`, `langchain-core`, `langchain-anthropic` (later unused, see
below), `langgraph-checkpoint-postgres`, `psycopg[binary]`, `fastapi`, `ag-ui-protocol`,
`ag-ui-langgraph`, `openinference-instrumentation-langchain`, `arize-phoenix-otel`,
`opentelemetry-sdk`/`-exporter-otlp`. `AgentConfig` added to `config.py`. Base install
confirmed unaffected (lazy imports only).

### Phase 4 — Workspace/skills: built, then reversed in favor of `deepagents`

`[x]` reversed. Originally hand-built `agent/workspace/{fs,search}.py` +
`agent/skills/store.py` (path-traversal-safe file ops, grep search, `SKILL.md`
lifecycle) with full test coverage. **Discarded** after finding `deepagents` (LangChain's
package) already ships `FilesystemMiddleware`/`SkillsMiddleware`/`MemoryMiddleware`
covering the same ground, battle-tested, matching the open "Agent Skills" standard other
tools speak. Decision: take deepagents as much as possible, keep LangGraph itself
visible (confirmed `create_deep_agent()` returns a plain `CompiledStateGraph`).

### Phase 5 — Tool binding + graph + checkpointer

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

### Phase 6 — Postgres history schema + API layer

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

### Deployment consolidation (mid-course correction)

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

### Package restructuring (mid-course correction)

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

### Subagent-based model routing

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

### Knowledge-source scope and deferred sandbox runtime

The Notion "Digital Home" inventory was initially left to external MCP clients' own
Notion connections. Once Argus became the operating agent, direct read access became
needed. An initial provider proposal was corrected to a native, agent-only knowledge
source, grouped conceptually with memory and kept off `/mcp`.

NVIDIA OpenShell was considered for hardening break-glass sessions or sandboxing a future
agent `execute` capability. It was not adopted: neither use case required another runtime
at this stage, and keeping the harness small took priority.

### Development tooling and handover cleanup

`[x]` Pre-commit configuration and the `precommit:install`/`precommit:run` tasks exist for
local lint/hygiene checks. The README and `task migrate` already document/run explicit
Argus-owned Alembic migrations; the stale unfinished migration-documentation item was
removed from this ledger. Migrations still do not run automatically at agent startup.

`[x]` AGENTS.md was refreshed for the agent/API/DB package layout, fixed worker, current
provider binding, and development commands. Historical notes and verification logs were
moved into this History section. The old protocol-era/SEP speculation was removed from
active guidance; the current invariant is simply that failed elicitation refuses the
action. No protocol compatibility claim was newly verified during this documentation work.

### Agent verification evidence carried forward from the handover

- Real Postgres checkpoint persistence was exercised across fresh graph/checkpointer
  instances with a **fake chat model** (`tests/db/test_checkpointer.py`). This does not
  prove OpenRouter integration.
- Alembic migrations and history repository operations ran against real Postgres
  (`tests/db/test_repo.py`), alongside LangGraph's checkpoint tables. The repository
  functions are still not wired into live agent turns.
- Local `argus agent serve` against real Postgres exercised AG-UI route reachability and
  mounted MCP auth/lifespan behavior. Regression tests are now in `tests/api/test_app.py`.
  Reachability is not a complete real-model or approval/resume round-trip.
- The package-restructuring milestone recorded 108/108 passing tests and working
  `alembic current` from the new migrations path. That count is historical, not a new
  test result or the current suite size.
- Tool-policy/interrupt metadata, workspace tools, model configuration, and worker
  registration have unit/structural coverage. Docker SDK clients and launcher subprocesses
  are mocked; real-service gaps remain listed above.

### Native AG-UI approval and resume integration

`[x]` Drove the complete pause → HTTP interrupt outcome → approve/reject → resume path
through `/agent`. This exposed three gaps in the default AG-UI adapter: structured
interrupt outcomes were disabled, a single response discarded its interrupt ID, and
cancellation/multiple responses used sentinels that LangChain HITL could not consume.

`argus.agent.api.ArgusAgent` now enables the native `RUN_FINISHED` interrupt outcome and
translates `resume[]` into LangGraph's ID-keyed resume map. Cancellation rejects all
actions in that interrupt. Validation happens before checkpoint writes: stale/duplicate
IDs, malformed decisions, and legacy command input return `INVALID_APPROVAL` without
consuming the pending action. The frontend contract is documented in `DESIGN.md`.

`tests/api/test_approvals.py` adds 18 integration cases covering planner and worker
approvals, optional MCP mounting, reconnecting while paused, replaying a consumed answer,
invalid-response recovery, cancellation, mixed batch decisions, and parallel workers.
One case runs a listening Uvicorn server over loopback TCP with MCP mounted and a worker
performing the approved call. The real graph, policy, provider implementation, HTTP
adapter, and SSE encoding run; the models and Docker SDK are faked, and these approval
tests use an in-memory checkpointer.

Validation: **128 passed, no skips** in the full pytest suite, including the existing
real-Postgres persistence/repository checks; ruff and `git diff --check` passed. Upstream
deprecation warnings remain. No OpenRouter key was configured, and no actual Docker
restart or real-model/frontend approval flow was exercised.

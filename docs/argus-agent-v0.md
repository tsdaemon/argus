# Argus Agent v0 — execution ledger

Tracks implementation of the Argus agent and its interfaces. See
[`DESIGN.md`](DESIGN.md) for the design/requirements this
executes against. Update this file as work lands — it's a status log, not a plan;
finished phases and verification logs live in [History](#history), including reversals.
Current working instructions live in [`AGENTS.md`](../AGENTS.md).
The [live test plan](LIVE_TEST_PLAN.md) gives the staged real-model/container checks;
those live checks remain unexecuted until results are recorded here.

Legend: `[x]` done and verified · `[~]` done but unverified/partial · `[ ]` not started.

## Acceptance criteria (current status)

- `[x]` Interactive LangGraph agent can run and resume from persisted state — verified
      against real Postgres (`tests/db/test_checkpointer.py`), using a fake chat model.
- `[x]` React/CopilotKit interface starts/continues runs, displays chat/tool activity,
      and restores saved conversations. Browser integration checks use a real Python
      server and Postgres with deterministic model/Docker fakes.
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
      are faked; the CopilotKit UI is now verified too. A real OpenRouter/target-host
      round-trip remains unverified.
- `[x]` PostgreSQL persists checkpoints and chat history — verified live. Conversations
      are indexed and message snapshots archived during runs. Separate raw
      run/tool-call/approval audit instrumentation remains unfinished.
- `[x]` Explicit Markdown memory loadable/modifiable by the agent itself, including
      `AGENTS.md` — via `deepagents`' memory middleware.
- `[x]` Traces visible in Phoenix via OTel/OpenInference — a synthetic LangChain call reached Phoenix; a real agent run is still to be checked.
- `[ ]` Break-glass action launches Claude Code on Theseus from the Agent UI without
      NOPASSWD sudo — `argus.launcher` and `/launch` exist, but real SSH/Claude Remote
      Control remain unverified. The frontend links to the existing authenticated
      `/launch` route when the break-glass web interface is enabled.
- `[ ]` A2A interface lets another agent call into Argus Agent — not started, but in v0
      scope (see design doc's Interfaces section).

## Explicitly out of scope for v0

Mem0/learned memory, vector/semantic search over workspace Markdown (grep-only for now,
seam left open), scheduled health runs, self-hosted LangSmith, generic multi-agent
support beyond the fixed worker, chat gateways (Telegram/Discord), broad multi-SDK provider
abstraction (OpenRouter is the one abstraction used, and it's a config value, not a code path
per provider),
Temporal, unrestricted shell/root access (`execute` tool deliberately excluded).

Additional sandbox runtimes are not part of v0; see [History](#history) for the OpenShell
evaluation.

## Next up, in order

Agreed 2026-09-20. Each item gets its own design pass before it is built.

1. **Notion** (above): small, needs no host access, and gives later items inventory context.
2. **`prometheus` provider**: instant and range queries, active alerts, scrape targets, all
   READ. SMART data is probably already scraped by a `prometheus-smartctl` exporter, which
   would make a separate SMART provider unnecessary. To confirm: where Prometheus runs and
   whether that exporter is scraped.
3. **`journalctl` provider**: needs host access while argus runs in a container, so either a
   read-only journal mount or a read-only forced-command script installed by autohome, in the
   break-glass pattern. If logs already flow to Loki, a Loki provider replaces it.
4. **Unattended runs, then scheduled jobs**: one run mode for callers with nobody to approve
   (scheduled jobs and A2A alike), where a MUTATE tool fails closed and the run files a
   break-glass request or a notification. Jobs are a Postgres table (schedule, prompt, allowed
   tools) with an in-process scheduler, each run a normal thread in history. Needs the
   severity-tiered notification design first. Scheduled health runs were out of scope for v0;
   this changes that.
5. **Generative UI (A2UI)**: dashboards from provider data, so after item 2. A fixed
   catalog of read-only components the agent fills with data, so a rendered component can
   never be a way around approval.
6. **A2A interface**: a small endpoint on the same app with bearer auth like `/mcp`, calling the
   agent in unattended mode.

## Not started

- `[ ]` **Execution audit instrumentation**: chat indexing and `messages` snapshot
  archival are wired into live runs. Separate `runs`/`tool_calls`/`approvals` audit
  records still need a LangChain middleware or callback hooked into the graph.
- `[x]` **OTel/Phoenix wiring** (`agent/tracing.py`). Non-fatal to a run; see the tracing entry in the ledger.
- `[ ]` A2A interface — **in v0 scope** (see design doc's new Interfaces section), not
  yet designed. Worth checking LangChain's own
  [Agent Protocol](https://github.com/langchain-ai/agent-protocol) (`runs`/`threads`/
  `store` REST contract, which LangGraph Platform implements a superset of) as a
  candidate foundation rather than inventing something bespoke.
- `[ ]` **Installing external skills** (`AgentConfig.skill_sources`, `owner/repo`
  shorthand) — design decided (see design doc's Workspace & skills section), not yet
  implemented.
- `[ ]` **Notion as a native agent tool** (decided 2026-09-20, not built): read and write
  access to the "Digital Home" hub through the raw Notion API, built as a `NotionMiddleware`
  (a LangChain `AgentMiddleware`, passed to `create_deep_agent` beside the filesystem
  middleware) since deepagents ships none and `langchain-notion` has no query or update. Native
  to the agent like the workspace tools: no `Provider`/`ToolSpec`, no `PolicyEngine`, no
  approval cards, never over `/mcp`. Its `wrap_tool_call` hook is the single place that
  captures previous values, writes the audit record, and refuses deletion; a system-prompt
  fragment carries the schema summary. Deletion is excluded in code (no delete, archive, or trash tool; the update tool
  rejects `archived`, `in_trash`, and block removal), since an integration token cannot forbid
  it by itself. Every write is logged to a Postgres table before it is sent, with the target,
  the new values, and the previous ones so an edit can be undone; the table follows the
  repository pattern with an in-memory fake. Tools: search, query a data source with a filter,
  fetch a page as markdown, create a page, update a page. The token comes from an environment
  variable, and the integration is shared with only the hub page. Open: check the current API
  version and data-source endpoints before writing the client. Accepted risk: page text is
  untrusted input and edits are not gated, so an injected instruction could cause a wrong
  edit; the log and Notion's page history make it recoverable, not prevented.
  Layout found in the workspace: three databases under the hub (Projects, Tasks, Inventory),
  and Inventory has seven data sources (Hardware, Software, Systems, Signals, Repos,
  Maintenance, Backups). Its `Signals` table (SEV1 to SEV3, channel, runbook) is a starting
  point for the notification design below, `Maintenance` (frequency, last executed) for
  scheduled jobs, and `Hardware` (IP, hostname, criticality) for break-glass hosts and
  Prometheus targets. A workspace skill should describe the schema.
- `[ ]` **Severity-tiered notifications** (SEV0 phone call, SEV1 ntfy, SEV2 email) — see
  design doc's new Notifications section. No telephony/email integration exists yet;
  what assigns a severity is undecided. Needs a design pass before implementing.
- `[ ]` **Generative UI**: let the agent render purpose-built UI in the chat (for example a
  container status table, a log viewer, or a metrics panel) instead of only text and the
  approval card. Not designed and not started; this replaces A2UI's earlier place in the
  out-of-scope list. What exists: CopilotKit's chat with a wildcard tool-call renderer
  (`WildcardToolCallRender`) and a custom interrupt renderer for approvals
  (`frontend/src/Approvals.tsx`). "Dynamic UI integration" is also one of the design doc's
  named learning goals. Open questions: the mechanism (CopilotKit generative UI through tool
  renderers over AG-UI, or the A2UI protocol); where the component definitions live and how
  the agent chooses one; how to keep it read-only so a rendered component can never become
  a way around the approval flow; and how it is tested without a browser-only path.

Provider roadmap beyond the current Docker/break-glass tools: `systemd` (status,
journal, restart), `disk`/SMART, and `network` probes. An asynchronous MCP approval-queue
backend is also unbuilt; it is separate from the agent's LangGraph interrupt/resume flow.
No CI workflow is committed; pytest and ruff are the intended baseline.

## Known open risks / things to re-verify before calling v0 done

- The approval path now has HTTP/graph integration coverage with deterministic models
  and a mocked Docker SDK, including worker delegation and parallel interrupts. The
  CopilotKit UI now exercises approval, rejection, mixed batches, and reload recovery.
  A real model still needs to exercise that same path. The adapter's
  resume hook is an upstream integration point; retain these checks when upgrading it.
- No real `OPENROUTER_API_KEY` has been used against a live model anywhere in this
  work — all graph/API tests use fake chat models. The actual OpenRouter integration
  (auth, rate limits, real tool-calling behavior) is unverified against the real
  service.
- `docker compose up --build` for the full consolidated stack (agent image, Postgres,
  Phoenix) has not been run end-to-end — only `docker compose config` (syntax) and a
  local Argus process against a real Postgres
  container.
- `/agent`, the chat UI, and the history API currently have no application-level
  authentication. The MCP bearer token protects `/mcp` only; resolve agent access
  control before network deployment.
- The frontend uses CopilotKit's direct-agent registration hook
  (`agents__unsafe_dev_only`) to keep one Python service; dependencies are pinned.
  Recheck this integration when upgrading CopilotKit. Browser restoration replays a
  durable transcript and pending checkpoint approvals, not every past event or an
  in-flight run; full cross-tab live synchronization is not built.
- `examples/theseus.argus.yaml` has no `agent:` block. Complete its database, model
  credential, and workspace settings before using it for an agent deployment.
- Real Hermes elicitation remains unobserved; the original compatibility investigation
  was documentation-only.
- The Docker provider has not been exercised against a real daemon with real containers.
- Real forced-command SSH and Claude Remote Control remain unverified, including
  noninteractive login/workspace trust and whether the session stays reachable.
- No deployment to Theseus is recorded. Overlay/configuration checks do not establish
  that a service is running there.

## History

This section records implementation milestones, reversals, and checks reported during
prior work. Paths, test counts, and choices in early entries describe that point in time;
later entries can supersede them. Moving these notes here did not rerun the checks.
**Historical server commands below are obsolete:** the sole runtime is now `argus serve`,
which starts the agent with its interfaces. The old MCP-only/stdio modes and
`argus agent serve` command have been removed; do not restore them from these records.

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
deleted the overlay files. An MCP-only command remained at that point; it was later
removed in the single-runtime correction below.

`[x]` **Follow-up correction**: initially left both port 8420 (leftover from the
pre-consolidation MCP-only default) and 8421 (agent) exposed/published, despite `/mcp`
and `/agent` being mounted on the *same* FastAPI app bound to *one* uvicorn port. Fixed:
one port (`ARGUS_PORT`, default 8421) in `Dockerfile` `EXPOSE`, `docker-compose.yml`
`ports:`, and `.env.example`; `docker-compose.deploy.yml.example`'s Traefik
`loadbalancer.server.port` label corrected from `8420` to `8421`.

`[~]` `theseus.argus.yaml` simplified: dropped the `allowed_containers` allowlist so all
containers are visible by default (per explicit request) — not yet deployed for real.

`[x]` **Second follow-up correction**: the `agent` extra itself removed. All LangGraph/
deepagents/FastAPI/Postgres/OTel dependencies folded into base `dependencies` in
`pyproject.toml` (previously `[project.optional-dependencies].agent`) — per explicit
feedback, there's no more "MCP-only" install to keep light for. `Dockerfile` no longer
passes `--extra agent` to `uv sync` (it's the only sync now). `cli.py`'s lazy imports in
`agent serve` were retained at that point for CLI startup latency, not as a hard
dependency boundary. The command split was later removed. Also removed the now-unused
`langchain-anthropic` (dead since the OpenRouter correction above) and renamed the
package from `argus-mcp` to `argus` in `pyproject.toml`.

`[x]` **Framing correction**: "Argus MCP as an independently-deployed, standalone
pillar" was never accurate to how this actually runs — corrected in the design doc.
The intended architecture was one agent process with an optional MCP interface,
toggled by `ARGUS_MCP_TOKEN`. Leaving a bare-MCP command for testing contradicted that
requirement; the single-runtime correction below removes it outright.

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

### CopilotKit frontend and persistent chat history — 2026-09-18

`[x]` Added the React/CopilotKit v2 frontend at `/`: chat, streamed tool rendering,
approve/reject cards, conversation navigation, reload recovery, a mobile layout, and a
link to the existing `/launch` page only when its launcher is configured. CopilotKit's
`CopilotChat` and `useInterrupt` own the chat/approval lifecycle; the small Argus
`HttpAgent` subclass supplies read-only history replay and sends only a new user turn
or approval response to the backend.

The user explicitly requested CopilotKit and chat history. The implementation follows
the upstream [self-managed persistence guidance](https://docs.copilotkit.ai/langgraph-python/threads-self-managed):
Argus owns the conversation list and Postgres storage. Direct agent registration uses
`agents__unsafe_dev_only`, keeping a single Python application process without a
CopilotKit runtime server or Enterprise thread store. Versions are pinned in npm's
lockfile; the integration should be rechecked on upgrades.

`[x]` Added thread creation/list/detail and read-only connect endpoints. `/agent` now
indexes conversations and archives AG-UI message snapshots through `db/repo.py`. The
`c81a9a2d4f10` migration adds activity ordering, stable message IDs, and insertion order.
Snapshots upsert messages without removing older context; the UI retains the full
archive when the agent summarizes, without returning that archive to the model.
Pending approvals come from the current checkpoint, so reading history cannot execute
them. Separate run/tool/approval audit records remain future work.

`[x]` Vite supports local development against the Python API. Production assets are
served by FastAPI, before the optional MCP mount. The Dockerfile builds them in a Node
stage and copies them into the Python image. Added frontend setup/build/test tasks and
updated README, design, and current AGENTS guidance.

Verification:

- **136 pytest tests passed, no skips**; real Postgres checkpoint/repository/history
  checks included. History tests rebuild the API/checkpointer before answering restored
  approvals, check transcript ordering and deduplication, and verify archived context
  stays visible without entering the model prompt. Ruff and `git diff --check` passed.
- **5 Playwright tests passed** in Chromium against a listening Uvicorn server and a
  freshly migrated, isolated Postgres schema. Covered continued chats, switching/reload,
  approving/rejecting restored requests, mixed batch decisions, mobile layout, and
  history-load error recovery. The real graph/policy/provider code runs; the model and
  Docker SDK are deterministic fakes. Screenshots were inspected on desktop and mobile.
- `npm run build` passed TypeScript checking and Vite compilation. Vite reports large
  dependency chunks from CopilotKit's rendering stack; bundle optimization is not done.
- `docker build -t argus:ui-check .` succeeded. A process in that built image used
  FastAPI's TestClient to retrieve `/`, all five assets referenced by its index,
  `/agent/health`, and `/api/ui-config`. This does not establish a full Compose deployment.

No live OpenRouter, real container operation, forced-command SSH, or Theseus deployment
was exercised. Authentication for the agent/chat/history interface and reconnecting to
an actively streaming run remain open; saved messages and pending approvals do restore.

### Single agent runtime and local live testing — 2026-09-19

`[x]` Removed the separate MCP startup path, including its HTTP/stdio transport switch.
`argus serve` now starts the complete agent application with UI, history, and optional
MCP on port 8421. There are no `agent serve` or `mcp serve` commands. Docker uses that
same entry point. Current README, design, AGENTS, package descriptions, and examples
were corrected together; AGENTS explicitly prohibits reintroducing an MCP-only runtime.

`[x]` Local testing runs Argus on the host while Compose runs Postgres, Phoenix, and
Docker canaries. The user chose to retain a/b/hidden for provider checks and wants to
watch workspace writes live. `examples/argus.dev.yaml` uses `.argus/workspace` and
allowlists a/b. `task serve` supplies the local database URL, matching `task migrate`.
The application container is behind Compose's opt-in `app` profile. Existing local
live-test configuration was adjusted to the host workspace and localhost database
access, preserving the user's canaries and removal of Argus from the local overlay.

The live test plan now covers this setup, including workspace edits visible in an editor,
real Docker approvals, allowlist checks, conversation/pending-approval recovery, and
optional MCP through the same process. These real-model checks have not yet been run.

Verification for this correction:

- **139 pytest tests passed, no skips**, including real Postgres checks; Ruff and
  `git diff --check` passed. CLI regressions require the full agent application and
  reject the removed server modes. Existing API tests cover optional MCP and UI routes.
- Compose configuration validated for default dependencies, the `test` profile, and
  the `app` deployment profile. The existing local overlay retains the canaries,
  publishes Postgres on localhost, and does not enable the Argus container.
- Isolated go-task checks verified `.env` loading, the default/overridden config path,
  and matching localhost database URLs for `task serve` and `task migrate`.
- A real local `argus serve` process using the development config and existing test
  Postgres returned 200 for `/`, `/agent/health`, `/api/threads`, and `/api/ui-config`.
  Its MCP interface rejected a missing token with 401 and accepted authenticated
  initialization with 200. It initialized `.argus/workspace` on the host and shut down
  cleanly afterward. The model key was fake; no model calls or Docker actions ran.

### Overmind development processes — 2026-09-19

Added `Procfile.dev` for the user's choice of Overmind. Its entries invoke
`uv run argus serve` and `npm --prefix frontend run dev` directly, with no Task calls
inside the Procfile. The agent workspace stays on the host. `task dev` starts Compose
dependencies, invokes the migration task, and launches Overmind after checking for
Overmind and tmux. It supplies the local database URL, loads `.env`, and passes an
optional `CONFIG=...` selection to the agent process. README documents this workflow.
The default `.overmind.sock` control socket is gitignored. Frontend changes reload
through Vite; Python changes still require restarting the agent process.

Verified setup order, direct process commands, dotenv/database environment, and both
default and overridden config paths using isolated command stubs. `git diff --check`
passed. The actual Overmind-managed application was not started during this check.

Follow-up: moved `ARGUS_AGENT_DATABASE_URL` to the Taskfile's shared top-level `env`
and renamed `task serve` to `task backend:dev`. Configuration selection now uses
`ARGUS_CONFIG` throughout the Taskfile, Procfile, and current docs, including
`task dev ARGUS_CONFIG=...` and `.env`. The Procfile still invokes uv/npm directly.

Renamed the Docker host-port variable from `AGENT_PORT` to `ARGUS_PORT`, preserving
existing local `.env` values, and grouped the Taskfile into project workflows,
dependencies/database, backend, frontend, break-glass, Docker, and deployment sections.

Regrouped the remaining top-level tasks into namespaces: `migrate` → `backend:migrate`,
`postgres:up` → `deps:postgres`, `breakglass:list` → `backend:breakglass:list`, and added
`backend:lint` (top-level `lint` now delegates to it). The break-glass section was folded
into Backend and "Dependencies and database" renamed "Dependencies". README, AGENTS.md, and
the live test plan use the new names. `task --list` verified; the renamed tasks were not run.

### mprocs replaces Overmind — 2026-09-19

Overmind runs its processes in a detached tmux server, so `task dev` did not put the user
in a tmux session. Replaced it with mprocs: `Procfile.dev` became `mprocs.yaml` (same
`agent` and `frontend` commands, `ARGUS_CONFIG` still defaulting to
`examples/argus.dev.yaml`), the `dev` task checks only for `mprocs`, and the
`.overmind.sock` gitignore entry was removed. In the TUI, `r` restarts the selected process
and `q` quits and stops both. README, AGENTS.md, the live test plan, and `.env.example`
were updated. Only `task --list` parsing was checked; mprocs is not installed here, so
`task dev` has not been run. The Overmind entry above is kept as history.

### Chat styling pass — 2026-09-19

Light restyle of the CopilotKit chat in `frontend/src/styles.css`, as a stopgap before a
full rework. CopilotKit's colour tokens are now overridden on `.argus-chat` and its
`[data-copilotkit]` descendants (they are defined on those elements, so setting them only
on the wrapper had no effect). The message list and input are centred in a 780px column,
message text is 15px with more line height and spacing, and the user bubble is a soft
green with a squared corner. Selectors use CopilotKit's `data-testid` attributes and
`copilotKit*` classes plus one `cpk:bg-muted` class, so a CopilotKit upgrade may need
them adjusted. `npm run build` passes. Not checked visually or with Playwright:
Chromium and the test Postgres are not available here.

Moved the Compose dependencies into mprocs as a foreground `deps` process
(`docker compose --profile test up postgres phoenix canary-a canary-b canary-hidden`), so their
logs are visible next to the agent and Vite. `task dev` now only invokes mprocs. The `agent`
process polls `pg_isready` in the Postgres container, runs `alembic upgrade head`, then
serves, so restarting it with `r` re-applies migrations. Quitting mprocs now stops the
containers as well (volumes are kept), where before they kept running. README, AGENTS.md, and
the live test plan were updated. Not run: mprocs is not installed here.

### Docker Desktop is the local engine — 2026-09-19

Starting Docker Desktop with WSL integration replaced `/var/run/docker.sock` and rewrote
`~/.docker/config.json` with a `credsStore` pointing at a Windows helper that WSL cannot
find, so the first image pull (`postgres`, `phoenix`, `alpine`) failed under `task dev`.
The pre-existing native `docker.service` was still running the earlier Argus dependencies
and tsdfm containers on a separate engine. That engine was stopped and disabled
(`docker.service`, `docker.socket`, `containerd.service`), and the `credsStore` line was
removed. `docker` now reaches only Docker Desktop, and the Compose dependencies run there.
Its volumes on the old engine (including the earlier Argus Postgres data) were not migrated.
Environment change only; no repository files were affected.

Made the fix durable by adding Docker Desktop's `resources/bin` directory to `PATH` in
`~/.zshenv` (read by non-interactive shells such as mprocs children), so
`docker-credential-desktop.exe` resolves even if Docker Desktop rewrites `credsStore` into
`~/.docker/config.json` on a later start. The old engine's services stay disabled.

### Database connection failure after the engine switch — 2026-09-19

`GET /api/threads` returned 500 right after the first `task dev` on Docker Desktop. The
Compose Postgres and Phoenix containers had started while the old engine still held
`127.0.0.1:5432` and `:6006`, so Docker Desktop never published their ports (`docker port`
was empty). The `agent` process's migration therefore ran against the old engine's Postgres,
and the new database had no tables. Restarting the two containers (`docker compose --profile
test restart postgres phoenix`) published the ports; `alembic upgrade head` then created the
schema and `/api/threads` returned 200. Cause was the overlap with the old engine, not a
config change.

Tracing is not implemented: `agent/tracing.py` does not exist, nothing in `src` reads
`agent.otel`, and `examples/argus.dev.yaml` has `otel.enabled: false`, so Phoenix stays empty.
This matches the open OTel/Phoenix item above.

Moved Docker back out of mprocs. `task dev` again runs `deps:up`, then `backend:migrate`,
then mprocs with only `agent` and `frontend`, so Compose keeps the dependency ordering
(`up -d --wait`) and the shell wait loop is gone. `deps:up` is now
`docker compose --profile test up -d --wait` (the profile covers Postgres, Phoenix, and the
canaries), replacing the foreground `up` that had been in the Taskfile. Quitting mprocs
leaves the containers running; `task deps:down` stops them. README, AGENTS.md, and the live
test plan were updated. Not run: mprocs is not installed here.

### Favicon — 2026-09-19

`frontend/src/favicon.svg` is a single eye (cream almond, blue iris taken from the logo's eye
colours, black pupil) on the logo's `#050709` background, linked from `frontend/index.html`.
The first attempts, a placeholder circle and then a 128px downscale of the whole logo, were
replaced: the full logo is too detailed to read at tab size. The SVG is under `src/` rather
than `public/` so Vite emits it under `/assets/`, the only static path `argus serve` exposes
besides `/`. Rendered at 16, 32, 64, and 256px to check legibility; `npm run build` emits the
hashed icon and rewrites the link. Not yet checked in a browser tab.

Changed the sidebar tagline under the Argus name from "HOME OPERATIONS" to "SEE EVERYTHING"
(`frontend/src/App.tsx`) and the page title to "Argus · See everything"
(`frontend/index.html`). No test referenced the old text.

### Palette A and logo in the UI — 2026-09-19

Restyled `frontend/src/styles.css` as a dark theme built on Color Hunt palette
`#000000 #5682b1 #739ec9 #ffe8db`, with the logo's own background `#050709` as the base.
Steel blue and cream are the accents; surface, border, and muted-text tints were derived
from them rather than taken from the palette. The document carries `class="dark"` so
CopilotKit's dark tokens and prose colours apply regardless of the OS theme, and the chat
tokens are overridden with `.argus-chat.argus-chat` to beat CopilotKit's `.dark` selector.
Project tokens are prefixed to avoid colliding with CopilotKit's `--muted` and `--accent`.
The approval card keeps an amber left border to read as "needs attention".

The `◉` text mark is replaced by the logo (`frontend/src/logo.png`, a 320px copy of
`docs/logo.png`) in the sidebar and the empty state. The service name is now lowercase in
UI text ("argus", `argus · see everything`, the input placeholder, the approval heading, the
load error), and `tests/chat.spec.ts` uses the new placeholder. Code identifiers such as
`ArgusHttpAgent` are unchanged. `tsc` and `npm run build` pass; the Playwright suite and
the visuals were not run or checked (no Chromium or test Postgres here).

### Collapsible sidebar — 2026-09-19

The sidebar has a `«`/`»` toggle (`aria-expanded`, labelled "Collapse sidebar" / "Expand
sidebar") that shrinks it to a 76px rail showing the logo, a new-conversation button, the
toggle, and the status dot; the conversation list and text labels are hidden. The choice is
kept in `localStorage` (`argus.sidebar.collapsed`, guarded by try/catch) and the width change
animates unless reduced motion is requested. On narrow screens the top-bar layout is
unchanged and the toggle is hidden. Added a Playwright test for collapse and reload
persistence. `tsc` and `npm run build` pass; the test and the visuals were not run or
checked (no Chromium or test Postgres here).

### Top bar removed — 2026-09-19

Removed the header above the chat ("WORKSPACE", the thread title or a generic "Home
infrastructure", and the Ready pill): it repeated what the sidebar list already shows. The
run state (`role="status"`, "Ready" / "Working…") and the optional "Open privileged session"
link moved into the sidebar footer, and the status dot pulses while a run is active (not
under reduced motion). The dead `.topbar`, `h1`, `.run-status`, and `.header-actions` rules
were deleted. When the sidebar is collapsed only the dot remains. No test referenced the
header. `tsc` and `npm run build` pass; visuals not checked.

Reworked the sidebar toggle to match the reference pattern: a borderless panel icon (rounded
rectangle with a vertical divider) in the top row beside the logo, replacing the bordered
`«`/`»` button at the bottom. The native `title` tooltip became a `data-tooltip` pill (dark,
rounded, delayed 0.3s), opening below the button when expanded and to the right of the rail
when collapsed; the collapsed new-conversation button uses the same tooltip. When collapsed,
the logo sits above the toggle. `tsc` and `npm run build` pass; visuals not checked.

### Tracing wired to Phoenix — 2026-09-19

Added `src/argus/agent/tracing.py`. `setup_tracing(config.agent.otel)` runs once at `argus
serve` startup (`cli.py`) and, when `otel.enabled`, calls `phoenix.otel.register(endpoint,
project_name, batch=True)` and `LangChainInstrumentor().instrument(...)`, so LangGraph,
model, and tool calls become OpenInference spans. Export happens in a background batch thread
and every setup exception is caught and logged, so an unreachable or broken collector cannot
fail or slow a run. `register` picks the protocol from the endpoint, so `OtelConfig` and its
defaults are unchanged: the dev config uses the already-published
`http://127.0.0.1:6006/v1/traces` (HTTP), and the example config's Compose-internal
`http://phoenix:4317` (gRPC) still applies. `examples/argus.dev.yaml` now sets
`otel.enabled: true`; the example config stays off.

Tests in `tests/agent/test_tracing.py` cover disabled, enabled (endpoint passed through), and
failure (logged, not raised); `tests/agent/__init__.py` was added to match the other test
packages. Verified live: with tracing on against the running Phoenix, a LangChain fake-model
call produced an `LLM` span in project `argus-trace-check` via Phoenix's REST API. That check
used a synthetic model, not a full agent run. A leftover `argus-trace-check` project remains in
Phoenix and can be deleted in its UI. The running agent must be restarted to pick up the
config.

### One local config — 2026-09-19

Local development had two configs: the committed `examples/argus.dev.yaml` and a gitignored,
older copy `argus-live.local.yaml` that the user's `.env` selected through `ARGUS_CONFIG`.
The copy differed only in `otel.enabled: false`, so the running agent ignored the newly wired
tracing. Reconciled on `examples/argus.dev.yaml` alone: removed the `ARGUS_CONFIG` override
from the Taskfile (`dev`, `backend:dev`) and `mprocs.yaml` (the path is now literal), removed
it from `.env.example`, and dropped every mention of per-developer configs from the README,
AGENTS.md, and the live test plan. In the user's working tree, the `ARGUS_CONFIG` line was
deleted from `.env` and `argus-live.local.yaml` was removed (verified identical to the dev
config apart from `otel`). The agent must be restarted to pick up the change. Docker
deployment still selects its own file through `CONFIG_FILE`. Earlier ledger entries that
mention `ARGUS_CONFIG` are kept as history.

### Delete a conversation; SQLAlchemy models and a history repository — 2026-09-19

**Feature.** `DELETE /api/threads/{id}` (204; 404 if unknown; 422 for a bad id; 503 without a
history store) removes the conversation's history and its LangGraph checkpoints
(`ArgusAgent.delete_thread_state` → `adelete_thread`), including a pending approval. The
sidebar shows a trash icon on row hover with an inline "Delete this conversation? Delete /
Cancel" confirmation; deleting the open conversation selects the next one (or the empty
state) and updates the URL. Existing Phoenix traces are not deleted.

**Cascades.** The foreign keys had no `ON DELETE CASCADE`, so deletion order mattered. Added
migration `e5b7c9a31d20` that recreates the six foreign keys with `ON DELETE CASCADE`, and
SQLAlchemy models (`argus/db/models.py`) whose relationships use `cascade="all,
delete-orphan"` and `passive_deletes=True`, so deleting a thread is one `DELETE` and the
database removes runs, messages, tool calls, and approvals. The models are Alembic's
`target_metadata` (with an `include_object` filter that ignores LangGraph's checkpoint
tables), and `alembic check` reports no drift between models and migrations. `sqlalchemy>=2.0`
is now a declared dependency.

**Repository pattern.** This replaces the earlier "no ORM, plain functions" decision.
`argus/db/repo.py` was removed. `argus/db/history.py` defines the `HistoryRepository`
protocol (threads, chat messages, delete) and `SqlHistory`, a SQLAlchemy async
implementation that also carries the not-yet-used run/tool-call/approval audit methods.
`add_chat_routes` and `build_app` take a `HistoryRepository` instead of an
`AsyncConnectionPool`; `argus serve` builds an async engine and disposes it on shutdown.

**Tests no longer need Postgres.** `tests/fakes.py` provides `InMemoryHistory`; the API tests
(`tests/api/test_history.py`, moved from `tests/db/test_chat_history.py`) use it with
`InMemorySaver`, including rebuild/durability, archive-outside-context, and delete. The
Playwright server (`tests/frontend_server.py`) uses the same in-memory stores, so it no longer
creates a schema or needs a database. `tests/db/` holds a contract suite run against both the
fake and `SqlHistory`, plus Postgres-only audit/cascade tests and the checkpointer test; they
build a throwaway schema with `alembic upgrade head` and skip when `ARGUS_TEST_DATABASE_URL`
is unreachable (`postgres_url` fixture). The old `db_pool` fixture and `tests/db/test_repo.py`
are gone.

**Verified.** Without any database: 141 passed, 9 skipped (the Postgres-only cases). Against
a scratch Postgres database: all 14 `tests/db` tests pass and the temporary schemas were
dropped. Playwright (Chromium installed to `~/.cache`): 7 of 7 pass, including new tests for
delete and for sidebar collapse persistence. Screenshots of the expanded sidebar, delete
confirmation, and collapsed rail were reviewed. Also restyled the message input from
CopilotKit's hard-coded grey to the palette. `task frontend:test` needs Chromium only.

### Phoenix cost: patched image — 2026-09-19

Question: can Phoenix show OpenRouter's cost? Findings on Phoenix 20.14.0 (read from the
running container): no config option exists; cost is computed by a server-side daemon
(`span_cost_calculator`) from token counts times its own price table, matched by regex on
`llm.model_name`; nothing reads `llm.cost.*` from a span. OpenRouter model ids such as
`anthropic/claude-sonnet-4.5` match no built-in entry (`claude-sonnet-4-5` does), and
OpenRouter reports the real charge as `usage.cost` on every response. Upstream request, open:
https://github.com/Arize-ai/phoenix/issues/15240 (the user asked its author about a PR and
will submit one if they do not).

Change: new `phoenix/` directory with `Dockerfile` and `reported-cost.patch`. The image is
stock `arizephoenix/phoenix:version-20.14.0` (same digest as `latest` at the time) with one
file replaced. The patch is a unified diff with `src/phoenix/...` paths, so it can be applied
to the upstream repo as well. It adds `apply_reported_cost` to `SpanCostCalculator`: reported
`llm.cost.prompt` / `llm.cost.completion` are applied to their side's details, a lone
`llm.cost.total` is spread over all details weighted by the computed cost (when a model
matched) or by token counts (when not), and a total plus one side derives the other. Because
the base image has no shell or patch tool, the Dockerfile applies the patch in an Alpine stage
and copies the result over the file; a Phoenix version that changed that file fails the build.
`docker-compose.yml` builds the `phoenix` service from `./phoenix` as `argus-phoenix:local`,
and `deps:up` now passes `--build`. The Phoenix container's data is not on a volume, so
recreating it clears existing traces.

Verified against the running container. Control on stock Phoenix: a span with
`llm.cost.total=0.0123` and an unmatched model stored `total_cost` NULL. On the patched image:
total only, unmatched model: total 0.0123 (split 0.01025/0.00205 by token counts); prompt and
completion given: 0.003/0.009, total 0.012; matched model with a reported total of 0.05: total
0.05 (split 50/50 because the computed prompt and completion costs were equal); no reported
cost: unchanged price-table result of 0.006. Test projects were deleted afterwards.

Not done: argus does not yet write `llm.cost.*` onto its spans, so real runs still show no
cost. That needs `usage.cost` from the OpenRouter response copied onto the LLM span (the
LangChain instrumentor does not map it), and a real call to confirm where it appears in the
response. For a total-only report the prompt/completion split is an estimate; the total is exact.

### argus reports OpenRouter cost on its spans — 2026-09-19

Finding: OpenRouter's `usage.cost` already survives into LangChain for a non-streaming call
(`response_metadata["token_usage"]["cost"]`, and `llm_output.token_usage`), but not for a
streamed one: `langchain-openai`'s `_convert_chunk_to_generation_chunk` keeps only token counts
from the final usage chunk, and the agent streams. The OpenInference LangChain instrumentor
(0.1.76) has no cost handling at all, and neither does Arize's OpenAI instrumentor, so
swapping instrumentors would not help. OpenTelemetry's GenAI conventions define no cost
attribute yet (proposals `gen_ai.usage.cost.*` are open in `semantic-conventions-genai`);
OpenInference's `llm.cost.*` is what Phoenix consumes.

Change: `src/argus/agent/model.py` adds `CostReportingChatOpenAI`, a `ChatOpenAI` subclass that
puts a streamed chunk's `usage.cost` back on the message's `response_metadata.token_usage`;
`graph._build_model` uses it. `src/argus/agent/tracing.py` adds `reported_cost(outputs)` (reads
`llm_output.token_usage` or the generation message metadata, accepts only a finite non-negative
number, and treats `0.0` as a real cost) and `_report_provider_cost()`, which wraps the
instrumentor's private `_update_span` (idempotent) so LLM spans get `llm.cost.total`. It is
installed by `setup_tracing` and errors in it are swallowed like the rest of tracing.
`reported_cost` is written to be lifted into an upstream instrumentor PR (to be filed by the
user, as with Phoenix #15240); the subclass would belong in `langchain-openai`.

Tests: `tests/agent/test_reported_cost.py`, hermetic via `httpx.MockTransport` (a fake gateway
serving JSON and SSE), covers streamed message metadata, spans for non-streaming and streaming
calls, a free call reporting `0.0`, no cost leaving the attribute absent, single wrapping, and
the number-validation cases. Disabling the subclass makes the two streaming tests fail.
Verified end to end with argus's own `setup_tracing`, `_build_model`, an OpenRouter-style stub,
and the patched Phoenix: the non-streaming and streamed spans each stored `total_cost` 0.0123
(split 0.01025/0.00205 by token counts, as the patch does for a lone total) for a model the
price table does not know. The probe project was deleted. Not done: a real OpenRouter call, so
the response shape is from OpenRouter's usage-accounting docs and a stub, and a full agent
run through the UI.

Added `docs/tracing.md`, a standalone guide to tracing: configuration, what is captured, the
cost pipeline and the upstream gaps behind each workaround, the patched Phoenix image, how to
check it, troubleshooting, and upgrade notes. Linked from the README and AGENTS.md.

### Agent autoreload — 2026-09-19

The `agent` process in `mprocs.yaml` now runs `uv run watchfiles 'argus serve --config
examples/argus.dev.yaml' src examples/argus.dev.yaml`, so it restarts when Python under `src/`
or the dev config changes. `uvicorn --reload` was not an option: it needs an importable app
object, and `argus serve` builds its app after opening the database and checkpointer.
`watchfiles>=1.0` is now declared in the `dev` extra (it was installed but undeclared) and
`uv.lock` was refreshed. Verified on a scratch port outside mprocs: touching a source file and
touching the config each restarted the server (new pid, health OK), and stopping the watcher
left no process or listener behind. Restarting cuts off an in-flight chat stream; approvals
survive because checkpoints are in Postgres. mprocs itself was not run, and a restart still
works by hand with `r`. README, AGENTS.md, the live test plan, and `docs/tracing.md` were
updated.

### `argus serve` replaced by an ASGI app factory — 2026-09-19

The `serve` command is gone. The server is now `uvicorn argus.api.app:create_app --factory`,
with the config path in `ARGUS_CONFIG` (set once in the Taskfile's top-level `env`, and as an
`ENV` in the Dockerfile, whose `ENTRYPOINT` is now uvicorn). `argus` remains as the
`breakglass` command group only.

How it works: `create_app()` (in `argus/api/app.py`) loads the config, calls `setup_tracing`,
and builds the app over a not-yet-open Postgres pool (`db.checkpointer.make_checkpointer`) and a
lazy SQLAlchemy engine. `build_app` gained a `resources` async context manager, and its
lifespan now enters that and FastMCP's lifespan through an `AsyncExitStack`. The resources
open the pool, run the idempotent `checkpointer.setup()`, and on shutdown close the pool and
dispose the engine. `build_app` keeps its signature for tests. `AsyncPostgresSaver` captures
the running event loop when constructed, so the factory must be called inside one; uvicorn does
call it from within its serving loop, and the test helper `call_like_uvicorn` does the same.
The checkpointer now uses a connection pool instead of one connection.

Development: `task backend:dev` and the mprocs `agent` process run the same command with
`--reload --reload-dir src --reload-dir examples --reload-include '*.yaml'`, replacing the
`watchfiles` wrapper. `watchfiles` stays in the `dev` extra so uvicorn's reload is event-driven.
Tests: the `serve` test was removed and `tests/api/test_factory.py` covers the missing-config
error, building without touching the database, and resources opening on startup and closing on
shutdown. The docs no longer mention `argus serve`.

Verified: 158 passed, 9 skipped (Postgres-only); 7/7 Playwright; and a real uvicorn run of the
exact mprocs command on a scratch port against the dev Postgres: `/agent/health` OK,
`/api/threads` 200, a reload on a source change and on a config change, and a clean shutdown
("Application shutdown complete", no listener left). Not done: a full `docker build` of the
image (only `docker buildx build --check`), and a run through mprocs itself.

### `argus` CLI removed — 2026-09-19

Deleted `src/argus/cli.py`, its tests (`tests/test_cli.py`), the `argus` script entry in
`pyproject.toml`, the `backend:breakglass:list` task, and the now-unused `click` dependency
(`uv.lock` refreshed). The CLI only wrapped `argus breakglass list|approve|deny` over the
sqlite store, and its `approve` merely flipped a status without launching anything; the
designed path is the `/breakglass` web view (with the optional host launcher). The separate
`argus-breakglass-launch` script (`argus.host_launch`) is untouched. The one behaviour the CLI
tests covered that the store tests did not, denying a request, now has a store test. README,
AGENTS.md, `examples/argus.example.yaml`, the Dockerfile, and the break-glass provider
docstrings no longer mention the CLI. Verified: 149 passed, 9 skipped (Postgres-only), lint
clean, `docker buildx build --check` clean, imports resolve.

### Break-glass live checks planned; generative UI recorded — 2026-09-19

Added a "Break-glass check" section to `docs/LIVE_TEST_PLAN.md`: setup (an
`ARGUS_MCP_TOKEN`, the `breakglass` provider block, restarting `task dev` because Task reads
`.env` once), a helper to file a request as an MCP client and one to read the sqlite store
(the CLI is gone), a table of checks (token gate, login gate, admin creation, the agent being
unable to file, filing, deny, approve without a launcher, decisions needing a login, restart
persistence, optional ntfy push and push failure), and a launcher table that needs the host
setup from the README (link visibility, approve launches, launch failure reported, forced key
limits, TTL, not agent-reachable). The launcher checks are unexercised.

Before writing it, the web and MCP path was scripted once on a scratch server (port 8431, a
temporary config and sqlite file, a throwaway token) so the steps match the code: `/mcp`
without a token returned 401; `/breakglass` logged out redirected to `/login`; the first
`/login` created the account (`admin`) and showed a 24-character password once, a wrong password
was rejected and the right one set a session cookie, and a second `/login` no longer showed
it; the only MCP tool listed was `request_break_glass`; two filed requests appeared on
`/breakglass` with Approve and Deny; approving one and denying the other were recorded in the
store and cleared them from the page; an unknown id returned 404; and a logged-out decision
redirected to `/login` and changed nothing. Not exercised: the ntfy push, the launcher and SSH,
the agent-cannot-file check in a real chat, and restart persistence. Whoever opens `/login`
first on a fresh store becomes the admin, which the plan now states.

Also recorded the generative UI item under "Not started" and removed A2UI from the out-of-scope
list (see above).

### Break-glass design page; multi-host direction decided — 2026-09-20

Wrote [breakglass.md](breakglass.md) (linked from DESIGN.md's break-glass bullet): the parts,
four sequence diagrams (request and approve, manual launch, working in the session and
root, host onboarding), and a trust table. It describes the built single-host flow and marks
the multi-host parts as planned.

Decisions, none implemented yet:

- The per-host transport stays SSH with a forced command; no new network daemon.
- Root is reached by the operator typing the sudo password into the launched session, so
  the password passes through the model provider by choice. No `NOPASSWD`, and argus never
  handles the password. The launched session runs as an unprivileged sudoer.
- One `claude auth login` per host, no copied credentials. Read from the Claude Code docs:
  Remote Control needs a full-scope login, so `claude setup-token` and API keys cannot be
  used, and a saved login expires (warned three days ahead).
- Install as an autohome Ansible role; the launcher becomes a standalone stdlib-only script.
- argus config gains a named `hosts:` map. `target_host` on a request is free text today and
  routes nothing.

Unverified: whether `sudo` works without a tty from Claude's shell tool, whether the docs'
refresh behavior means copied logins would log hosts out of each other (moot now), and
everything on a real host. No code changed; nothing was run.

### Break-glass host side moved to autohome; launcher key as a Docker secret — 2026-09-20

The host script and its install now live in autohome (`roles/utilities/breakglass`, with a
`claude` package role, both syntax-checked, launcher unit tests passing, never run on a real
host). argus still has `host_launch.py`, which is to be removed with the `hosts:` work.
Launcher key pair generated in the gitignored `.ssh/` of this repo; autohome takes
the public half from `secrets.yaml`, so no key material is committed.

`docker-compose.deploy.yml.example` now injects the private key and a `known_hosts` file as
Compose secrets (mode 0400 root for the key) instead of the old bind-mount comment, which would
have resolved on theseus under `docker --context theseus`. New keys `ARGUS_LAUNCHER_KEY_PATH`
and `ARGUS_KNOWN_HOSTS_PATH` in `.env.deploy.example`, both required. Verified only that
`docker compose config` interpolates and that a missing variable fails with the message. Not
verified: that Compose copies secret files to the remote engine with the requested mode, and
that ssh accepts the key there. No Docker daemon was reachable from this shell, and I did not
touch theseus.

### Break-glass launcher verified on theseus — 2026-09-20

Ran the autohome `claude` and `breakglass` roles on theseus, then launched a session over SSH
with the launcher key (`.ssh/argus_launcher`, gitignored) and a 15-minute TTL. First runs
failed twice, each time with `claude` exiting immediately, and the log showed why:

1. `Workspace not trusted`: every session gets a fresh directory. The launcher now writes
   `hasTrustDialogAccepted` for it into the user's `~/.claude.json` (undocumented file; the shape
   matches a report from another user and was confirmed by the file's contents on theseus).
2. `Enable Remote Control? (y/n)` read from stdin, which was `/dev/null`. The launcher now
   answers `y` through a pipe.

After both, `claude remote-control` stayed up and the operator reported the session working.
Also confirmed earlier: the forced-command key runs the launcher whatever the client asks for,
and a malformed payload exits 1 without starting anything.

Not exercised: the sudo path with a password (whether `sudo` works without a tty and where the
password ends up), launching through argus's approve and `/launch`, ntfy, and Compose
delivering the launcher key as a secret. `host_launch.py` and its test are still in this repo
and are now dead code; remove them with the `hosts:` work.

### Break-glass moved to Postgres; launcher takes named hosts — 2026-09-20

The request store and the admin account were the last things on sqlite, left from the standalone
MCP server. They are now `breakglass_requests` and `admin_account` in the argus Postgres,
behind a `BreakGlassRepository` (`argus.db.breakglass`: `SqlBreakGlass`, plus
`tests.fakes.InMemoryBreakGlass`), with Alembic revision `f2a6d8c41b93`. `AdminUserStore` became
`AdminAuth` over that repository, the provider takes the repository as a constructor
dependency (`instantiate_providers(..., dependencies=...)`), and `create_app` wires
`SqlBreakGlass(engine)`. `store_path` is gone from the config, and the compose `argus-state`
volume that held the sqlite file with it. Existing sqlite rows are not carried over; the
first `/login` after migrating creates a new admin and shows a new password.

The launcher config is now a `hosts:` map with shared defaults and per-host overrides. The
approve button and `/launch` show a host picker; the request's `target_host` text preselects a
match; approving or launching without a configured host is refused before anything changes.
Every payload carries `protocol: 1`, which the host script in autohome enforces. `host_launch.py`,
its test and the console-script entry were removed; the script lives in autohome.

Verified: full suite, 160 passed, none skipped, so the Postgres half of the repository contract
suite ran against the real database in a throwaway schema (which also ran the new migration),
and `alembic check` reports no drift. Not verified: the changed app against a running
`task dev`, since the dev database needs `task backend:migrate` first, and the host picker in a
browser.

### One app-wide login — 2026-09-20

The chat UI, `/api/threads`, `/agent`, and the break-glass pages had no authentication, which the
ledger and AGENTS.md listed as an open risk. Traefik basic auth was considered and rejected
because it and the MCP bearer token share the `Authorization` header, so external MCP clients
would be refused; the login is in the app instead.

`argus.api.auth.add_auth` adds `/login` and `/logout` and an ASGI gate (not
`BaseHTTPMiddleware`, so the AG-UI event stream is untouched) in front of everything else.
Open paths: `/login`, `/logout`, `/agent/health`, `/mcp`. Page requests without a session are
redirected to `/login?next=...` (only local paths are honored), other requests get 401, and the
UI's `api()` helper sends a 401 to the login form. The account is the existing `admin_account`,
split out of the break-glass store into its own `AdminRepository` (`argus.db.admin`); the
break-glass pages still check the session themselves. `ARGUS_ADMIN_PASSWORD` seeds the password
on first `/login` so a deployment never shows a claimable form (unset means a generated
password shown once, for local development); the deploy overlay requires
`DEPLOY_ARGUS_ADMIN_PASSWORD`. The app is open only where it is built without an admin
repository, which is what the tests and the Playwright server do. Sidebar gets a Log out button
when `/api/ui-config` reports `auth_enabled`.

Verified: 170 pytest passed, none skipped (Postgres half included), ruff clean, `tsc` clean. Not
verified: the login in a browser, through Vite on 5173, or with `task frontend:test`. Known gaps:
no rate limiting or lockout, no password rotation short of deleting the row, and a session
lasts 30 days.

### Shared page template and login restyle — 2026-09-20

The login and break-glass pages were built from f-strings in two modules, full-width, with no
favicon. They are now Jinja templates in `src/argus/templates/` (`jinja2` added as a
dependency, lockfile updated) that extend one `base.html`: shared head, the eye logo as an
inline data-URI favicon (so no route to serve or gate), one stylesheet, and a header linking to
the chat. Content is centered in a narrow column and uses the app's palette; the host picker is a
set of radio pills (a native select drew an unstyleable OS dropdown); autoescaping
replaces the hand-written `html.escape` calls, and error and status text goes through a shared
`message.html`. `mark.svg` is a copy of `frontend/src/favicon.svg`; update both together.

Checked by rendering each template in headless Chromium at desktop and phone widths, and a test
run of 170 passed. Not checked: the pages served by a live app, or the approve page on a phone
through a real request.

### The agent can file a break-glass request — 2026-09-20

`breakglass` had been kept off the agent's tool list since the first commit, stated in AGENTS.md,
DESIGN.md, and the README but never argued anywhere. A live check showed the agent could not
help when asked about a break-glass procedure, and invented an "internal access portal".
`request_break_glass` is READ (it only records a request), so `build_app` now hands the
provider to the agent whenever it has a break-glass repository; the agent binding uses the
same specs as `/mcp`. What stays out of reach is unchanged: no tool approves, denies, or
launches, on either binding, and both remain human routes behind the admin login. The
"Agent cannot file" live check became "Agent files, cannot decide".

Verified: 172 pytest passed, none skipped; the agent graph binds `breakglass_request_break_glass`
and nothing containing approve, launch, or decide, and `build_app` gives the agent the provider
only with a store. Not verified: a real model calling the tool, and the ntfy push from an agent
call. The tool runs without an approval card, since READ is allowed by policy.

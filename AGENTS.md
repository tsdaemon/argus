# Agent instructions for Argus

Use this file for current working guidance. Read [`README.md`](README.md) for the project
overview and [`docs/DESIGN.md`](docs/DESIGN.md) for the authoritative architecture,
requirements, and scope before making architectural changes.

[`docs/argus-agent-v0.md`](docs/argus-agent-v0.md) tracks current build status and unfinished
work; its [History section](docs/argus-agent-v0.md#history) holds implementation history,
reversals, and verification logs. Update that ledger as work lands. Keep dated status,
test results, and session history out of this file.

Argus is a self-hosted LangGraph agent for operating home infrastructure, primarily the
Theseus NAS, through constrained tools. Keep it small and understandable for one operator.
Optimize for personal usefulness and learning; preserve useful extension points without
building generality for its own sake. Reuse maintained components while keeping execution
and permission boundaries visible.

## What runs

**`uvicorn argus.api.app:create_app --factory` is the only server command** (the config comes
from `ARGUS_CONFIG`; there is no `argus serve`). It starts the agent and its interfaces in
one FastAPI process on port **8421** by default. There is no MCP-only runtime, stdio
mode, or separate MCP server command. Do not reintroduce one, including for testing.
It serves the built React/CopilotKit UI at `/`, chat/history routes under `/api/threads`,
`/agent` (AG-UI), and `/agent/health`. When `ARGUS_MCP_TOKEN` is set, it also mounts
the MCP application at `/mcp`; enabling the `breakglass` provider adds `/breakglass`,
`/login`, and `/launch` through that same mounted application.

The agent invokes provider tools directly in-process; MCP exposes them to external callers.
External clients such as Hermes or Claude Code can call the same provider implementations
over MCP. The planned A2A interface will let another agent call Argus Agent itself.

Run Argus locally with `task backend:dev`; `task deps:up` runs Postgres, Phoenix, and Docker
canaries in Compose. The workspace is a host directory so the operator can watch it change.
The `Dockerfile` packages the same agent for deployment, under Compose's opt-in `app`
profile. Tracing is off unless `agent.otel.enabled` is set (the dev config enables it). Agent dependencies
are base dependencies in `pyproject.toml`.

## Architecture map

| Concern | File |
|---|---|
| Agent graph, models, workspace middleware, fixed worker | `src/argus/agent/graph.py` |
| Provider tools bound to LangChain + approval metadata | `src/argus/agent/tools.py` |
| AG-UI agent wrapper | `src/argus/agent/api.py` |
| Combined FastAPI app and optional MCP mounting | `src/argus/api/app.py` |
| Chat streaming, history catalog, read-only history replay | `src/argus/api/chat.py` |
| CopilotKit chat, conversation navigation, approval cards | `frontend/src/` |
| Postgres-backed LangGraph checkpoints | `src/argus/db/checkpointer.py` |
| Argus-owned history: SQLAlchemy models, repository interface + implementation, Alembic migrations | `src/argus/db/models.py`, `src/argus/db/history.py`, `src/argus/db/migrations/` |
| Shared tool classification and policy decisions | `src/argus/policy.py` |
| Transport-independent tool specs + MCP binding | `src/argus/providers/base.py` |
| Shared provider registry and construction | `src/argus/providers/registry.py` |
| Approval backend protocol | `src/argus/approval/__init__.py` |
| MCP elicitation approval, fails closed | `src/argus/mcp/elicit.py` |
| Config schema + `${ENV_VAR}` expansion | `src/argus/config.py` |
| Optional MCP sub-app assembly + bearer auth | `src/argus/mcp/server.py`, `src/argus/mcp/auth.py` |
| Server factory `create_app` and its lifespan | `src/argus/api/app.py` |
| Docker visibility and restart tools | `src/argus/providers/docker_provider.py` |
| Break-glass provider + SQLite request store | `src/argus/providers/breakglass_provider.py` |
| Break-glass web routes + shared admin authentication | `src/argus/mcp/webapp.py`, `src/argus/webauth.py` |
| ntfy push notifications | `src/argus/notify/` |
| SSH launcher + separately deployed forced-command host script | `src/argus/launcher/ssh.py`, `src/argus/host_launch.py` |

`agent/` builds the agent; `api/` serves it; `db/` owns persistence; `mcp/` exposes provider
tools to external clients. `api/` composes these packages. `agent/` and `mcp/` share policy,
providers, and configuration without depending on each other.

To add a provider, implement `name`, `tool_specs(config) -> list[ToolSpec]`, and
`register(mcp, policy, config)` (normally a call to `mcp_bind`). Register its class in
`PROVIDER_REGISTRY` in `providers/registry.py`. The agent's `langchain_bind` consumes those
same specs. Do not bypass policy with direct `@mcp.tool` registration or duplicate tool
implementations per interface. Canonical policy IDs use dots (`docker.restart_container`);
LangChain tool names replace dots with underscores (`docker_restart_container`).

## Trust boundaries and approval

- **Operational provider tools** share one `PolicyEngine.decide()` classification path.
  Defaults are READ → allow, MUTATE → require approval, DESTRUCTIVE → deny; configuration
  can override these. DENY removes a tool from either binding's tool list entirely.
- **Agent approval** uses deepagents/LangChain `HumanInTheLoopMiddleware`, configured through
  `interrupt_on` by `langchain_bind`. It permits approve/reject decisions. `agent/api.py`
  exposes pending interrupts in the native AG-UI `RUN_FINISHED.outcome` and translates
  `resume[]` responses into LangGraph decisions keyed by interrupt ID. Validate responses
  before checkpointing them; cancellation rejects the pending actions. The agent's policy
  instance needs no `ApprovalBackend` because it only calls `.decide()`.
- **MCP approval** awaits real `ctx.elicit()` inside the policy gate before calling the
  implementation. It is not an advisory tool the model can skip. `ElicitApproval` refuses
  declined/cancelled requests and catches `ToolError` to fail closed.
- **Private workspace and skills** are native agent capabilities, outside operational
  policy gating and never exposed over MCP. They are the agent's own editable knowledge.
  Planned read-only Notion access belongs here conceptually: an agent-only knowledge tool,
  not a `Provider`/`ToolSpec` or a way for external MCP clients to read the user's inventory.
- **Break-glass** is separate. External MCP clients can file a request, but `breakglass` is
  excluded when assembling the agent's provider tools. No agent tool approves or launches a
  privileged session; a human uses the authenticated web routes. The React UI links to
  the existing `/launch` page when enabled. Keep target-host sudo password-gated, with no NOPASSWD.

Authentication is scoped per interface: the static bearer token protects `/mcp`, and the
generated admin account/signed cookie protects the break-glass web flow. `/agent`, the UI,
and `/api/threads` currently have no application-level authentication; setting
`ARGUS_MCP_TOKEN` does not protect them.

## Decisions worth preserving

- **OpenRouter for both models.** `ChatOpenAI` targets `agent.model_base_url` (OpenRouter by
  default). `agent.model` defaults to `google/gemini-3.7-flash`; `agent.worker_model`
  defaults to `stepfun/step-3.7-flash`. These are config values, not separate provider
  code paths. The harness-profile provider key is `openai` even for an Anthropic model
  accessed this way.
- **One fixed worker.** The `task` tool delegates to the explicit `worker` `SubAgent`.
  Keep deepagents' default general-purpose subagent disabled via `HarnessProfile`.
- **Use deepagents for workspace, skills, memory, and summarization.** `create_deep_agent()`
  returns a normal LangGraph `CompiledStateGraph`; checkpointing/resumption stays visible.
  Filesystem tools are `ls`, `read_file`, `write_file`, `edit_file`, `delete`, `glob`, and
  `grep`; arbitrary shell `execute` is excluded. Skills live under `skills/`; workspace
  `AGENTS.md` is loaded as memory and created if absent. This repository's handover file
  is distinct from that runtime workspace memory. Search is grep/progressive disclosure;
  no vector index or Mem0 integration exists.
- **State, history, and memory are separate.** LangGraph manages its checkpoint tables and
  calls `.setup()` at startup. Argus owns `threads`, `runs`, `messages`, `tool_calls`, and
  `approvals`, migrated explicitly with Alembic. The API indexes conversations and upserts
  AG-UI snapshots into `messages`, retaining older messages through context summarization.
  Separate run/tool/approval audit instrumentation remains unfinished. Markdown is explicit
  memory; durable artifacts are future work. Do not hand-edit LangGraph's schema or silently
  run Argus migrations at boot.
- **CopilotKit owns the chat/interrupt lifecycle.** `frontend/src/agent.ts` supplies a
  read-only connect stream and sends only the newest user message on new runs; do not feed
  the visible historical archive back into the graph. Approval resumes send no messages.
  Direct agent registration currently uses `agents__unsafe_dev_only`; keep dependencies
  pinned and exercise browser tests when upgrading. The conversation list is backed by
  Argus Postgres, without a CopilotKit Enterprise thread store or a Node runtime server.
- **HTTP route order and lifespan matter.** Add AG-UI routes before mounting MCP at `/`,
  and pass the MCP sub-app's lifespan to the parent FastAPI app. Otherwise the root mount
  can swallow requests, or MCP's internal task group never starts and requests fail.
  Regression coverage is in `tests/api/test_app.py`.
- **Break-glass requests support unattended external runs.** A request records context in
  SQLite and optionally sends an ntfy push linking to a phone-accessible approval page.
  Argus's own scheduler is not built. A live user can answer normal tool approvals instead.
- **Generated admin login, no URL secret.** First visit to `/login` provisions a password
  and reveals it once; subsequent login uses a PBKDF2 hash and signed session cookie.
  Keep login secrets out of URLs.
- **Session launch uses a forced-command SSH key.** The key on the target host is restricted
  to `argus-breakglass-launch`. Docker's socket is for provider operations, not the session
  launcher. `allowed_containers` scopes provider calls when configured; the Theseus example
  deliberately omits the allowlist. A read-only socket bind would not restrict Docker API
  calls.
- **Launch settings come from deployment config.** Request text cannot choose session
  permissions or TTL. The host script writes request/manual context to `CONTEXT.md` as
  material to review and detaches a `claude remote-control` process, with a timeout when
  configured. The intended interaction is through the operator's Claude account, without
  a browser-terminal implementation in Argus.
- **Tracing must be optional and non-fatal.** `argus.agent.tracing.setup_tracing` instruments
  LangChain/LangGraph via OpenInference and exports to Phoenix when `agent.otel.enabled` is
  true (default false). It runs once when the app is created, exports in a background
  batch thread, and swallows setup errors, so a tracing failure must never break an agent run.
  Endpoint scheme picks the protocol: `http://host:6006/v1/traces` (HTTP) or `http://host:4317` (gRPC).
- **Phoenix runs from a patched image** (`phoenix/`; full write-up in `docs/tracing.md`): stock Phoenix computes cost only from token
  counts and its own price table, which cannot match OpenRouter model ids, so the patch makes it
  use cost reported on a span (`llm.cost.*`) instead. Upstream request:
  https://github.com/Arize-ai/phoenix/issues/15240. The patch is pinned to one Phoenix version and
  fails the image build if that file changes. argus supplies the cost: `argus.agent.model` keeps
  OpenRouter's `usage.cost` on streamed messages (LangChain drops it), and `argus.agent.tracing`
  wraps the OpenInference LangChain instrumentor's private `_update_span` to set `llm.cost.total`.
  Both are workarounds for upstream gaps (`langchain-openai`, `openinference-instrumentation-langchain`
  0.1.76) and should be revisited on upgrade.

## Development and configuration

- `task install` installs Python and frontend dependencies; `task test` runs pytest and
  Playwright; `task backend:test` runs pytest alone; `task lint` runs ruff. Pre-commit
  configuration exists, but no CI workflow is committed yet.
- Local startup uses `task deps:up`, **`task backend:migrate`**, `task frontend:build`, then
  `task backend:dev`. Argus-owned migrations remain an explicit operation. The default local
  config is `examples/argus.dev.yaml`, with private workspace `.argus/workspace` and
  Docker tools scoped to `argus-live-a` and `argus-live-b`. Local
  development has that one config; do not add per-developer copies. Keep the workspace on the host for live inspection.
- UI setup: Node 24, `task frontend:install`, `task frontend:build`; the Python service
  serves the result at `/`. `task frontend:dev` runs Vite with an API proxy to port 8421.
  Docker builds the frontend in a Node stage and copies only its assets into the Python image.
- `task dev` starts Compose dependencies detached and migrates, then uses mprocs and
  `mprocs.yaml` to run local Argus + Vite together (requires mprocs).
  Open port 5173 for frontend live reload; no frontend build is needed. The agent process runs
  under `uvicorn --reload` and restarts on changes to `src/` or the dev config (`mprocs.yaml`). It uses the same single dev config. Quitting mprocs leaves dependencies running.
- `task frontend:test` runs Playwright against a real API/graph with in-memory history and
  checkpoint stores and deterministic model/Docker fakes; no database is needed. Install
  Chromium with `cd frontend && npx playwright install chromium`, and build the UI first.
- Tests must not require a real Postgres. Routes depend on `HistoryRepository` (`argus.db.history`);
  use `tests.fakes.InMemoryHistory` and `InMemorySaver`. `tests/db/` runs one contract suite against
  the fake and `SqlHistory`; the Postgres half builds a throwaway schema with `alembic upgrade head`
  and skips if `ARGUS_TEST_DATABASE_URL` is unreachable. Report skips explicitly: a green run with
  skips has not exercised the SQL implementation.
- `OPENROUTER_API_KEY` populates `agent.api_key` through YAML expansion.
- `ARGUS_AGENT_DATABASE_URL` supplies the example config and Alembic connection URL;
  `POSTGRES_PASSWORD` configures Compose's Postgres. The Taskfile's top-level `env`
  supplies the local database URL to every task, including mprocs's child processes.
- `ARGUS_MCP_TOKEN` enables the optional `/mcp` interface of the server.
- `ARGUS_NTFY_TOPIC_URL` is optional notification configuration. If a YAML string references
  `${ENV_VAR}`, that variable must exist (an empty value is allowed), or config loading fails.
- `ARGUS_BREAKGLASS_SESSIONS_DIR` is read on the **target host** by `host_launch.py` and
  defaults to `~/.argus/breakglass-sessions`.
- `examples/argus.example.yaml` includes agent configuration. `examples/theseus.argus.yaml`
  currently covers providers/policy and has no `agent:` block; add the appropriate database,
  model credential, and workspace configuration before using it for an agent deployment.
- `docker-compose.deploy.yml` and `.env.deploy` are deliberately gitignored local overlays;
  copy their `.example` templates and fill in actual deployment values when deploying.

Keep real-service checks distinct from fake-model and mocked-client tests when updating the
ledger. Report what was run, what passed or skipped, and what remains unverified.

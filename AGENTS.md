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

`argus agent serve` is the primary runtime: one FastAPI process on port **8421** by default.
It serves `/agent` (AG-UI) and `/agent/health`. When `ARGUS_MCP_TOKEN` is set, it also mounts
the MCP application at `/mcp`; enabling the `breakglass` provider adds `/breakglass`,
`/login`, and `/launch` through that same mounted application.

The agent invokes provider tools directly in-process; it does not call its own MCP server.
External clients such as Hermes or Claude Code can call the same provider implementations
over MCP. The planned A2A interface will let another agent call Argus Agent itself.

The default `Dockerfile` and `docker-compose.yml` package the agent, Postgres, and Phoenix
as one stack. Phoenix being present does **not** mean tracing is wired. Agent dependencies
are base dependencies in `pyproject.toml`, not an optional `agent` extra. `argus serve`
still provides MCP-only HTTP (default port 8420) or stdio for testing/lower-level use.

## Architecture map

| Concern | File |
|---|---|
| Agent graph, models, workspace middleware, fixed worker | `src/argus/agent/graph.py` |
| Provider tools bound to LangChain + approval metadata | `src/argus/agent/tools.py` |
| AG-UI agent wrapper | `src/argus/agent/api.py` |
| Combined FastAPI app and optional MCP mounting | `src/argus/api/app.py` |
| Postgres-backed LangGraph checkpoints | `src/argus/db/checkpointer.py` |
| Argus-owned history repository + Alembic migrations | `src/argus/db/repo.py`, `src/argus/db/migrations/` |
| Shared tool classification and policy decisions | `src/argus/policy.py` |
| Transport-independent tool specs + MCP binding | `src/argus/providers/base.py` |
| Shared provider registry and construction | `src/argus/providers/registry.py` |
| Approval backend protocol | `src/argus/approval/__init__.py` |
| MCP elicitation approval, fails closed | `src/argus/mcp/elicit.py` |
| Config schema + `${ENV_VAR}` expansion | `src/argus/config.py` |
| MCP server/sub-app assembly + bearer auth | `src/argus/mcp/server.py`, `src/argus/mcp/auth.py` |
| CLI: `argus agent serve`, `argus serve`, `argus breakglass ...` | `src/argus/cli.py` |
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
  privileged session; a human uses the authenticated web routes. The future React UI will
  link to the existing `/launch` page. Keep target-host sudo password-gated, with no NOPASSWD.

Authentication is scoped per interface: the static bearer token protects `/mcp`, and the
generated admin account/signed cookie protects the break-glass web flow. `/agent` currently
has no application-level authentication in `api/app.py`; setting `ARGUS_MCP_TOKEN` does not
protect it.

## Decisions worth preserving

- **OpenRouter for both models.** `ChatOpenAI` targets `agent.model_base_url` (OpenRouter by
  default). `agent.model` defaults to `anthropic/claude-sonnet-4.5`; `agent.worker_model`
  defaults to `anthropic/claude-haiku-4.5`. These are config values, not separate provider
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
  `approvals`, migrated explicitly with Alembic. Repository functions exist, but live agent
  runs do not yet write this history. Markdown is explicit memory; durable artifacts are
  future work. Do not hand-edit LangGraph's schema or silently run Argus migrations at boot.
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
- **Tracing must be optional and non-fatal.** OTel/OpenInference → Phoenix is planned and
  dependencies/config exist, but instrumentation is not implemented. `otel.enabled` defaults
  to false; a future tracing failure must not break an agent run.

## Development and configuration

- `task install` installs base and dev dependencies with uv; `task test` runs pytest;
  `task lint` runs ruff. Pre-commit configuration exists: `task precommit:install` installs
  the hook, and `task precommit:run` checks all files. No CI workflow is committed yet.
- Local startup uses `task postgres:up`, **`task migrate`**, then `task agent:serve`.
  Argus-owned migrations remain an explicit operation.
- Real Postgres tests under `tests/db/` use `ARGUS_TEST_DATABASE_URL` and skip if the database
  is unreachable. Report skips explicitly; a green suite with skips is not database proof.
- `OPENROUTER_API_KEY` populates `agent.api_key` through YAML expansion.
- `ARGUS_AGENT_DATABASE_URL` supplies the example config and Alembic connection URL;
  `POSTGRES_PASSWORD` configures Compose's Postgres and `task migrate`'s local URL.
- `ARGUS_MCP_TOKEN` enables the optional MCP mount in `argus agent serve`; it is required
  for standalone `argus serve --transport http`, but not stdio.
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

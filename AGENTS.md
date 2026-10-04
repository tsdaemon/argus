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
the MCP application at `/mcp`. Enabling the `breakglass` provider adds `/breakglass` and
`/launch` to the main app, with or without `/mcp`.

The agent invokes provider tools directly in-process; MCP exposes them to external callers.
External clients such as Hermes or Claude Code can call the same provider implementations
over MCP. The planned A2A interface will let another agent call Argus Agent itself. Threads it creates
(including idle-context rotations) have `origin='a2a'` and appear only in the UI's "Agents" section. The migration backfill marks A2A threads by an
`a2a_contexts` link or an agent author (`argus_author.kind='agent'`) on the first user message.

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
| Shell over SSH on named hosts, classified per command | `src/argus/providers/ssh_provider.py` |
| Home Assistant REST: state/history/logbook/log/template reads; `ha_call_service` classified per call | `src/argus/providers/homeassistant_provider.py` |
| Prometheus queries, metric discovery, targets, alerts (all READ) | `src/argus/providers/prometheus_provider.py` |
| Per-call command risk classifiers (`jev`, `llm`, `ask`) and their factory | `src/argus/classifier/` |
| Agent middleware storing per-call classifications for HITL | `src/argus/agent/classification.py` |
| Break-glass provider; its Postgres repository | `src/argus/providers/breakglass_provider.py`, `src/argus/db/breakglass.py` |
| Break-glass web routes + shared admin authentication | `src/argus/mcp/webapp.py`, `src/argus/webauth.py` |
| argus's own HTML pages (login, break-glass): Jinja templates on one base | `src/argus/webpages.py`, `src/argus/templates/` |
| Generated conversation titles (planner model, reasoning off or minimal, after the first reply) | `src/argus/agent/titles.py` |
| Per-thread model spend (callback on the chat models); Phoenix backfill | `src/argus/agent/cost.py`, `src/argus/agent/phoenix_costs.py` |
| ntfy push notifications | `src/argus/notify/` |
| SSH launcher (named hosts); the host-side script lives in autohome | `src/argus/launcher/ssh.py` |

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
- **Per-call classification** is for a tool whose risk depends on its arguments: `ssh.run`
  (agent and MCP name `ssh_run`) runs any shell command on a host named under the `ssh`
  provider's `hosts` (the router as its root `admin` user, the Home Assistant OS host as root). Top-level settings are defaults a
  host overrides, as in the launcher; each host has its own classifier, whose context is the
  host's name, `user@address`, and `description`. Commands run with `ssh -tt` (host option `tty`, default true) so that stopping the
  client on timeout hangs up the command on the host (BusyBox hosts such as the router and HAOS may have no `timeout`).
  HAOS runs nothing under `-tt`, so its host sets `tty: false`; without a tty a timeout may not stop the remote command;
  `timeout_seconds` is an optional tool argument, defaulting to the host's `timeout_seconds`
  (30) and capped at its `max_timeout_seconds` (300). A timed-out call returns its output so far.
  Its `ToolSpec.classify` uses the classifier the provider's `classifier.type` names:
  `jev` (default model `typesafe/jev-1.13` via OpenRouter's Decisions API; P(read) ≥ 0.9 is
  READ, P(destroy) ≥ 0.8 is DESTRUCTIVE, else MUTATE), `llm` (any OpenRouter chat model with
  structured output, answer mapped directly), or `ask` (always MUTATE; the default with no
  `classifier` block). A failed classification is MUTATE and is not cached. The same
  `decide()` maps that class, so a read
  runs, a change shows the approval card, and a destructive command is refused. On the
  agent side, `CallClassificationMiddleware` (`agent/classification.py`, on the planner
  and the worker) classifies asynchronously when the model returns and stores the result
  in the `AIMessage`'s `response_metadata`. The HITL `when` predicate and description
  only read it: HITL is synchronous and re-runs its node on resume, so it must do no I/O
  and see the same answer on both passes. A missing result reads as MUTATE. MCP awaits
  the classifier in `PolicyEngine._classified_gate`. The classifier is the only thing
  narrowing this tool.
  `ha.call_service` (`ha_call_service`) classifies the same way, from `domain` and `service`
  alone, with no I/O (`classify_service` in `homeassistant_provider.py`): `get_*` services are
  READ; `homeassistant.restart`/`stop`, `recorder.disable`, the `hassio`, `shell_command`,
  `python_script` and `pyscript` domains, and any service named `purge*`/`delete*`/`remove*`/
  `factory_reset*`/`wipe*`/`erase*` are DESTRUCTIVE; the rest are MUTATE. The Home Assistant token is a header
  only, never in a result, error, or log line.
- **MCP approval** awaits real `ctx.elicit()` inside the policy gate before calling the
  implementation. It is not an advisory tool the model can skip. `ElicitApproval` refuses
  declined/cancelled requests and catches `ToolError` to fail closed.
- **Private workspace and skills** are native agent capabilities, outside operational
  policy gating and never exposed over MCP. They are the agent's own editable knowledge.
  Planned Notion access (read and write, no delete, every write logged) belongs here
  conceptually: an agent-only tool, not a `Provider`/`ToolSpec`, not policy-gated, and not a
  way for external MCP clients to read the user's inventory.
- **Break-glass** is separate. The agent and external MCP clients can file a request
  (`request_break_glass`, classified READ: it only records). No tool approves or launches a
  privileged session; a human uses the authenticated web routes. The agent gets the provider
  only when `build_app` has a break-glass repository. The React UI links to
  the existing `/launch` page when enabled. Keep target-host sudo password-gated, with no NOPASSWD.

Authentication is scoped per interface: the static bearer token protects `/mcp`, and one
admin account with a signed session cookie protects everything else (`argus.api.auth`, a gate
added in `build_app` when an admin repository is supplied; `/login`, `/agent/health`, and
`/mcp` stay open). The break-glass pages check the session again themselves. Tests that build
the app without an admin repository get an open app on purpose.

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
  Summarization is deepagents' default middleware, which sizes itself from the model's
  `profile`; OpenRouter models have none, so `_build_model` sets `max_input_tokens` to
  `agent.context_tokens` (64k), giving a trigger at 85% instead of the 170k-token fallback.
  Filesystem tools are `ls`, `read_file`, `write_file`, `edit_file`, `delete`, `glob`, and
  `grep`; arbitrary shell `execute` is excluded. Skills live under `skills/`. Memory
  (`agent/memory.py`) splits conventions from facts: workspace `AGENTS.md` is the operator's
  day-0 rulebook, and learned facts go one topic per file under `memory/<topic>.md`, each listed in
  `memory/INDEX.md`. Both `AGENTS.md` and the index load into every conversation (once per
  thread); argus's own `MEMORY_PROMPT` replaces deepagents' generic memory prompt, and both
  files are created if absent, never overwritten. This repository's handover file
  is distinct from that runtime workspace memory. Search is grep/progressive disclosure;
  no vector index or Mem0 integration exists.
- **State, history, and memory are separate.** LangGraph manages its checkpoint tables and
  calls `.setup()` at startup. Argus owns `threads`, `runs`, `messages`, `tool_calls`, and
  `approvals`, migrated explicitly with Alembic. The API indexes conversations and upserts
  AG-UI snapshots into `messages`, retaining older messages through context summarization.
  Separate run/tool/approval audit instrumentation remains unfinished. Markdown is explicit
  memory; durable artifacts are future work. Do not hand-edit LangGraph's schema. The container runs
  `alembic upgrade head` before starting (`Dockerfile` entrypoint).
- **CopilotKit owns the chat/interrupt lifecycle.** `frontend/src/agent.ts` supplies a
  read-only connect stream and sends only the newest user message on new runs; do not feed
  the visible historical archive back into the graph. Approval resumes send no messages.
  Direct agent registration currently uses `agents__unsafe_dev_only`; keep dependencies
  pinned and exercise browser tests when upgrading. The conversation list is backed by
  Argus Postgres, without a CopilotKit Enterprise thread store or a Node runtime server.
- **Interrupted runs resume from the checkpoint.** Every run streams with
  `durability="sync"`. A thread whose checkpoint has a `next` step, no pending approval, and no
  live run stream in this process (`running` in `api/chat.py`) has stalled, e.g. on a restart;
  `GET /api/threads/{id}/run-state` reports it with the unanswered calls and their classes.
  A run with `forwardedProps.argus_resume` and no messages continues it: `ArgusAgent`
  overrides `get_stream_kwargs` to pass `input=None`, since the adapter's `"start"` mode would
  otherwise feed the message state back in and restart at the model. The UI resumes on its own
  when every pending call is READ and asks otherwise, since a cut-off call may already have run.
- **Runs get an explicit step limit.** The AG-UI adapter builds each run's config with
  `ensure_config`, which fills in LangChain's default `recursion_limit` of 25 and overrides the
  9,999 deepagents sets on the graph. `build_agent` passes `agent.recursion_limit` (default 200)
  as the adapter's config. A run that fails, this limit included, ends with a `RUN_ERROR` event
  from `ArgusAgent.run` rather than a dropped stream, and its checkpoint reads as stalled.
- **A run streams the transcript once.** The AG-UI adapter's `RAW` passthrough is off
  (`emit_raw_events=False`) and `ArgusAgent.get_state_snapshot` drops `messages`: both repeated
  the whole message state, attachments included, per graph step, which froze the UI in long
  threads. The UI reads only `MESSAGES_SNAPSHOT`; `test_run_stream_carries_the_transcript_once`
  guards this.
- **Chat attachments are inline.** CopilotKit's `attachments` (images, PDFs, text files, 10 MB
  each) send base64 parts in the user message; the adapter turns them into LangChain
  `image_url`/`file` blocks that OpenRouter accepts. `ArgusHttpAgent.requestInit` inlines text
  files as text parts, since providers handle text documents unevenly. Attachments are stored
  in checkpoints and history like any message content; there is no separate file store, and
  tools cannot read attachments.
- **HTTP route order and lifespan matter.** Add AG-UI routes before mounting MCP at `/`,
  and pass the MCP sub-app's lifespan to the parent FastAPI app. Otherwise the root mount
  can swallow requests, or MCP's internal task group never starts and requests fail.
  Regression coverage is in `tests/api/test_app.py`.
- **Break-glass requests support unattended external runs.** A request records context in
  Postgres and optionally sends an ntfy push linking to a phone-accessible approval page.
  Argus's own scheduler is not built. A live user can answer normal tool approvals instead.
- **One admin login, no URL secret.** First visit to `/login` creates the account: with
  `ARGUS_ADMIN_PASSWORD` set it uses that (a deployment); otherwise it generates one and
  reveals it once. Login uses a PBKDF2 hash and signed session cookie. The env var only
  seeds; rotate by deleting the `admin_account` row. Keep login secrets out of URLs.
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
  The deployed Phoenix requires a login (`PHOENIX_ENABLE_AUTH`); argus exports with its admin
  secret as `PHOENIX_API_KEY`, which `phoenix.otel` sends as a bearer token. Local Phoenix stays open.
- **The operator's title wins.** `threads.title_source` is null while the title is the first
  message's opening; `TitleWriter` replaces only that (once, as `generated`), and a rename
  through `PATCH /api/threads/{id}` sets `user`, which nothing overwrites. Title generation is
  off in `build_app` unless `generate_titles=True` (as `create_app` passes), so tests make no
  model calls for it.
- **argus counts spend itself; Phoenix is not the source.** `CostRecorder` adds each call's
  OpenRouter `usage.cost` to `threads.cost_usd`, so the UI shows spend with tracing off.
  `python -m argus.agent.phoenix_costs` only backfills threads from before it existed.
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
  `task backend:dev`. Locally migrations are a task step; the container runs them on start. The default local
  config is `examples/argus.dev.yaml`, with private workspace `.argus/workspace` and
  Docker tools scoped to `argus-live-a` and `argus-live-b`. It runs both agent models on the cheap
  `qwen/qwen3.7-flash`; model behaviour worth trusting is checked on the deployed models. Local
  development has that one config; do not add per-developer copies. Keep the workspace on the host for live inspection.
- UI setup: Node 24, `task frontend:install`, `task frontend:build`; the Python service
  serves the result at `/`. `task frontend:dev` runs Vite with an API proxy to port 8421.
  Docker builds the frontend in a Node stage and copies only its assets into the Python image.
- `task dev` starts Compose dependencies detached and migrates, then uses mprocs and
  `mprocs.yaml` to run local Argus + Vite together (requires mprocs).
  Open port 5173 for frontend live reload; no frontend build is needed. The agent process runs
  under `uvicorn --reload` and restarts on changes to `src/` or the dev config (`mprocs.yaml`). It uses the same single dev config. Quitting mprocs leaves dependencies running.
- `task frontend:test` runs Playwright against a real API/graph with in-memory history and
  checkpoint stores and deterministic model/Docker fakes; no database is needed.
  `tests/visual.spec.ts` adds screenshot comparisons of single elements (rows, cards, the
  rail) against images in `frontend/tests/__screenshots__/`, one set per platform. Use them
  for UI changes. After an intended visual change run `task frontend:test:update` and look
  at the changed images before committing them. Install
  Chromium with `cd frontend && npx playwright install chromium`, and build the UI first.
- Tests must not require a real Postgres. Routes depend on `HistoryRepository` (`argus.db.history`);
  use `tests.fakes.InMemoryHistory` and `InMemorySaver`. `tests/db/` runs one contract suite against
  the fake and `SqlHistory`; the Postgres half builds a throwaway schema with `alembic upgrade head`
  and skips if `ARGUS_TEST_DATABASE_URL` is unreachable. Report skips explicitly: a green run with
  skips has not exercised the SQL implementation.
- `OPENROUTER_API_KEY` populates `agent.api_key` and the `ssh` provider's classifier key
  through YAML expansion.
- SSH keys live in the gitignored `.ssh/`: `argus_launcher` is break-glass only, `argus_ssh`
  is direct access (the router and Home Assistant OS host), kept apart so widening direct access never widens
  break-glass. Both hosts' keys are pinned in `.ssh/known_hosts` (HAOS: `[192.168.0.100]:22222`).
- `ARGUS_AGENT_DATABASE_URL` supplies the example config and Alembic connection URL;
  `POSTGRES_PASSWORD` configures Compose's Postgres. The Taskfile's top-level `env`
  supplies the local database URL to every task, including mprocs's child processes.
- `ARGUS_HA_TOKEN` is the Home Assistant long-lived access token the `homeassistant` provider
  reads through `${ARGUS_HA_TOKEN}`. Locally it is in `.env`; deployed it is
  `DEPLOY_ARGUS_HA_TOKEN` in `.env.deploy`, required by `scripts/theseus-compose.sh`.
- `ARGUS_MCP_TOKEN` enables the optional `/mcp` interface of the server.
- `ARGUS_ADMIN_PASSWORD` seeds the admin password on first `/login`; unset means a generated
  one is shown once.
- `ARGUS_NTFY_TOPIC_URL` is optional notification configuration. If a YAML string references
  `${ENV_VAR}`, that variable must exist (an empty value is allowed), or config loading fails.
- `ARGUS_BREAKGLASS_SESSIONS_DIR` is read on the **target host** by the launcher script that
  autohome installs, and defaults to `~/.argus/breakglass-sessions`.
- `examples/theseus.argus.yaml` is the deployed config; secrets, the router's SSH port
  (`ARGUS_ROUTER_SSH_PORT`), and theseus's LAN IP (`THESEUS_IP`), both also used by the dev config, come
  from `.env`.
- `docker-compose.deploy.yml` and `.env.deploy` are deliberately gitignored local overlays;
  copy their `.example` templates and fill in actual deployment values when deploying.
  `task deploy` runs everything through `scripts/theseus-compose.sh`: the daemon is remote, so
  bind mounts and `file:` secrets would resolve on theseus; the script reads the config and
  keys locally into env vars that the overlay injects as configs and secrets.
  The image runs as a non-root `argus` user whose uid/gid are build args; the deploy builds it
  as theseus:users (1001:100), reaching the Docker socket through `group_add`. The deployed
  workspace is the host directory `/appdata/argus/workspace` (backed up with the rest of
  `/appdata`), created once by `task deploy:workspace`. `task deploy:sync` merges conversations
  and workspace both ways; files go through `rsync -e 'compose exec'` into the container.

Keep real-service checks distinct from fake-model and mocked-client tests when updating the
ledger. Report what was run, what passed or skipped, and what remains unverified.

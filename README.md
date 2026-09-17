<p align="center">
  <img src="docs/logo.png" alt="Argus" width="200">
</p>

<h1 align="center">Argus</h1>

<p align="center">
  <a href="https://github.com/tsdaemon/argus/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python 3.11+">
  <a href="https://langchain-ai.github.io/langgraph/"><img src="https://img.shields.io/badge/agent-LangGraph-1c3c3c.svg" alt="Built on LangGraph"></a>
  <a href="https://gofastmcp.com"><img src="https://img.shields.io/badge/MCP-FastMCP-6f42c1.svg" alt="Built on FastMCP"></a>
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/badge/lint-ruff-brightgreen.svg" alt="Linted with ruff"></a>
</p>

A self-hosted [LangGraph](https://langchain-ai.github.io/langgraph/) agent for operating home
infrastructure — without handing it raw shell access — plus the policy-gated MCP interface it's
built on.

The name follows a small mythological vocabulary from the design behind this project:
**Theseus** is the underlying system being operated on (in the reference deployment: a home
NAS, [tsdaemon/theseus](https://github.com/tsdaemon/theseus)); **Hermes** is whatever *other*
agent runtime might call in over MCP (a scheduled
[Hermes Agent](https://github.com/NousResearch/hermes-agent) run, Claude Code, or anything else
that speaks MCP); **Argus** — the hundred-eyed watcher — is this agent, the one thing standing
between any caller and anything that actually changes state.

See [`docs/DESIGN.md`](docs/DESIGN.md) for the full architecture and
[`docs/argus-agent-v0.md`](docs/argus-agent-v0.md) for current build status.

## Why not just give the agent SSH?

Because then the security boundary is "hope the model doesn't do anything stupid." Argus makes
it deterministic instead:

- Every tool a provider exposes is classified **READ**, **MUTATE**, or **DESTRUCTIVE**.
- **READ** tools are always available.
- **MUTATE** tools only run after a real approval round-trip — the gate lives in Argus's code,
  not in the model choosing to ask first. See [Approval](#approval), below.
- **DESTRUCTIVE** tools are, by default, never even registered — they don't exist in the tool
  list an agent sees, not just refused when called.
- **Break-glass** exists for when the agent genuinely can't resolve something with the tools it
  has — a human-reviewed escalation to a real, unconstrained Claude Code session. See
  [Break-glass](#break-glass), below.

None of this is specific to any one backend. This classification/approval boundary (`PolicyEngine`
+ `ApprovalBackend`) has no idea what Docker or Theseus are — it only knows how to classify and
gate. What actually gets exposed is decided per deployment, in a config file — and the exact same
tool implementations are bound both to Argus's own agent loop *and* to MCP for external clients,
never two separate implementations. See [`docs/DESIGN.md`](docs/DESIGN.md#tool-binding-architecture).

```
        React (AG-UI)                 Hermes / Claude Code           a phone browser
         a human, chatting             any MCP client                 approving break-glass
              │                              │                              │
              │ AG-UI                        │ MCP, bearer token            │ login (admin account)
              ▼                              ▼                              ▼
┌───────────────────────────── argus agent serve (one process) ───────────────────────────────┐
│  /agent                          /mcp  (optional, needs ARGUS_MCP_TOKEN)    /breakglass       │
│  ├── the LangGraph agent         ├── docker.list_containers    READ         pending requests, │
│  │   loop — OpenRouter model,    ├── docker.get_container_*    READ         Approve/Deny —    │
│  │   workspace/skills/memory     ├── docker.restart_container  MUTATE       separate login,   │
│  │   (deepagents), Postgres      └── breakglass.request_break_glass READ    separate auth     │
│  │   checkpoints                     (policy + approval boundary lives here, shared with      │
│  └── same docker.* tools,             the tools above)                                        │
│      bound in-process                                                                         │
└───────────────────────────────────────────────────────────────────────────────────────────────┘
```

## Quickstart

```bash
uv sync --extra dev
cp examples/argus.example.yaml my-argus.yaml   # edit to taste
cp .env.example .env                            # fill in POSTGRES_PASSWORD, OPENROUTER_API_KEY

task postgres:up      # Postgres for checkpoints + history
task migrate          # apply Argus's own history schema (Alembic)
task agent:serve       # runs on :8421 — set ARGUS_MCP_TOKEN in .env too if you also want /mcp
```

`OPENROUTER_API_KEY` ([openrouter.ai/keys](https://openrouter.ai/keys)) is the one model
credential Argus needs — it routes to any upstream model (Anthropic, OpenAI, Google, ...) you
name in `agent.model` in your config, no code change to switch. See
[Configuration](#configuration).

If a `breakglass` provider + launcher are configured, open `/breakglass` — the first visit
creates an admin account and shows the generated password once (see
[Break-glass](#break-glass)).

## Running it

A [Taskfile](Taskfile.yaml) wraps the common commands (`task --list` for the full set):

```bash
task install          # uv sync --extra dev
task test              # pytest
task lint              # ruff
task postgres:up        # local Postgres for the agent
task migrate            # apply Argus's own history schema
task agent:serve         # the agent, on :8421 — the primary way to run Argus
task mcp:run             # just the MCP server, stdio, no agent — for local MCP-client testing
task mcp:serve           # just the MCP server, HTTP, no agent — needs ARGUS_MCP_TOKEN
```

`argus serve` (bare MCP, no agent) still exists as a lower-level building block/testing surface
— see [`docs/DESIGN.md`](docs/DESIGN.md#goal) — but it's not how Argus is meant to run day to
day; `argus agent serve` is.

**Docker**: `Dockerfile` + `docker-compose.yml` at the repo root ship the whole stack — the
agent, Postgres, and Phoenix — as one thing, generic and safe to run anywhere:

```bash
cp .env.example .env   # fill in the tokens
task docker:up          # or: docker compose up -d --build
task docker:logs
```

One process, one port (`AGENT_PORT`, default 8421): `/agent` (AG-UI) always, `/mcp` +
`/breakglass` too if `ARGUS_MCP_TOKEN` is set. The compose file mounts `examples/argus.example.yaml`
(or whatever `CONFIG_FILE` in `.env` points at), named volumes for the breakglass sqlite store
and the agent's workspace, and the Docker socket (needed for the `docker` provider — see the
comment in `docker-compose.yml` about why a read-only bind there wouldn't actually restrict
anything; scope access via `allowed_containers` in the config instead).

**Deploying to theseus** (or any specific host) follows the
[homelab-compose-deploy](https://github.com/tsdaemon/theseus) two-file overlay pattern: a
`docker-compose.deploy.yml` adds Traefik/Homepage labels and host-specific env on top of the
generic base file. That overlay — and the `.env.deploy` it reads from — are **gitignored**,
deliberately: they're one person's real hostnames and label wiring, not something a generic OSS
repo should carry. Copy the committed templates to get started:

```bash
cp docker-compose.deploy.yml.example docker-compose.deploy.yml   # then fill in the placeholders
cp .env.deploy.example .env.deploy
task deploy         # docker --context theseus compose -f docker-compose.yml -f docker-compose.deploy.yml up -d --build
task deploy:logs
```

## Configuration

See [`examples/argus.example.yaml`](examples/argus.example.yaml) for every knob, annotated, and
[`examples/theseus.argus.yaml`](examples/theseus.argus.yaml) for a real deployment's shape. In
short:

- `providers:` turns MCP-facing backends on and configures them (currently: `docker`,
  `breakglass`).
- `policy:` sets the class defaults (`default_mutate`, `default_destructive`) and any per-tool
  `overrides:` keyed `"<provider>.<tool_name>"` — enforced identically whether a tool is reached
  through the agent or through `/mcp`.
- `agent:` configures the LangGraph harness: `workspace_root` (the agent's private Markdown
  memory + skills), `database_url` (Postgres, checkpoints + history), `model` (an OpenRouter
  `<provider>/<model>` id, e.g. `anthropic/claude-sonnet-4.5`), `api_key`
  (`${OPENROUTER_API_KEY}`), and `otel:` (Phoenix tracing, off by default).

`${ENV_VAR}` in any config value is expanded from the environment at load time, so tokens and
API keys never need to be committed.

## Providers shipped in v1

- **`docker`** — container visibility (`list_containers`, `get_container_status`,
  `get_container_logs`, `inspect_container`) and `restart_container`, optionally scoped to an
  `allowed_containers` list.
- **`breakglass`** — `request_break_glass` (MCP-only — see
  [`docs/DESIGN.md`](docs/DESIGN.md#trust-boundaries) for why it's never bound to the agent
  itself), plus the sqlite-backed request store shared with the `/breakglass` web view and the
  CLI.

**Roadmap** (same `Provider` shape, not built yet — see [Adding a provider](#adding-a-provider)):
`systemd` (unit status, journal logs, restart unit), `disk`/SMART (health, storage usage),
`network` (ping/probe a host).

Also planned, but deliberately **not** a `Provider`/`/mcp` tool: read access to the
user's Notion "Digital Home" inventory as a native knowledge source for the agent (see
[`docs/DESIGN.md`](docs/DESIGN.md#trust-boundaries)) — read-only reference material,
grouped with the agent's own memory/workspace rather than with MCP-facing operational
tools, so no external MCP client gets to read it just because it can call `docker.*`.

## Approval

A tool classified `MUTATE` is gated the same way — `PolicyEngine.decide()` — no matter which
interface it's reached through, but the actual human round-trip differs by interface:

- **Through the agent** (`/agent`): LangGraph's own `interrupt()`, surfaced to the React
  frontend as a native AG-UI `Interrupt` event. Approve/reject in the UI resumes the graph.
- **Through `/mcp`**: [FastMCP](https://gofastmcp.com)'s real elicitation primitive
  (`ctx.elicit()`) — a blocking round-trip to the connected MCP client, awaited *inside the tool
  handler itself* before it does anything. The model cannot skip this the way it could skip an
  advisory "please call this confirmation tool first" convention. Confirmed to work against
  [Hermes Agent](https://github.com/NousResearch/hermes-agent), the reference external client.

**Known gap** on the MCP path: MCP's newer "2026-07-28 era" protocol revision (SEP-2322/2575)
removed the server-initiated back-channel `ctx.elicit()` needs, replacing it with a
two-round-trip `InputRequiredResult` guard pattern instead. Against a client on that protocol
era, `ElicitApproval` catches the resulting error and **fails closed** — the action is refused,
never silently allowed.

The `ApprovalBackend` interface (`src/argus/approval/__init__.py`) is deliberately narrow and
optional — the agent's own `PolicyEngine` instance needs none at all, since it only ever calls
`.decide()`, never the MCP-specific `.register()` gate.

## Break-glass

`request_break_glass(reason, target_host, evidence, proposed_objective)` exists for when the
agent genuinely can't resolve something with the READ/MUTATE tools it has. It's classified
READ — it cannot mutate anything, only record that a human needs to look. There is no tool
anywhere — MCP or agent-native — that approves or executes one; it's a deliberate human-only
escalation, kept off the agent's own tool list entirely (see
[`docs/DESIGN.md`](docs/DESIGN.md#trust-boundaries)).

Break-glass is specifically for *unattended* runs — a scheduled check with nobody watching. (If
a human is live in the Argus Agent UI, they'd just answer the HITL approval prompt on a MUTATE
tool directly.) That means the approval channel can't assume a terminal or SSH session either —
it needs to work from a phone with nothing installed beyond a browser:

1. A pending request writes a row to a local sqlite store and fires a best-effort push (v1: via
   [ntfy](https://ntfy.sh) — no account, no API key, one HTTP POST to a topic URL you pick).
2. The push links straight into a minimal, plain-HTML, no-JS approval page served at
   `/breakglass` on the same `argus agent serve` process. No token to configure or embed in a
   URL: the first visit to `/login` generates a single admin account and shows the
   password once (save it — a password manager, not a note), then it's a normal
   username+password login backed by a signed session cookie. Log in, see the
   reason/evidence/target, tap Approve or Deny.
3. If a `launcher` is configured (see below), **approving also starts a Claude Code session on
   the host**, seeded with that request's reason/evidence/objective. If no launcher is
   configured, approving only flips the request's status — nothing opens on your behalf.

The same `/launch` page is also where the Argus Agent React UI's own break-glass button sends
you — one implementation, not two.

`argus breakglass list/approve/deny` is kept as a secondary terminal convenience for when you're
already at a terminal — it is not the designed path.

### Session launch (optional)

The one thing worth being deliberate about: everything else in Argus is scoped to a small,
specific, classified action. "Start a privileged, largely unrestricted Claude Code session" is
categorically bigger than any of that. So the launcher is opt-in, kept entirely off both the MCP
surface and the agent's own tool list (no agent ever triggers it — only the human-facing
`/breakglass` and `/launch` web routes do), and the security boundary lives on the machine that
actually runs the session, not in Argus's code:

```
approve (or /launch)  ──HTTP, logged in as admin──▶  argus agent serve
                                                              │
                                                              │ ssh, forced command
                                                              ▼
                                                    argus-breakglass-launch (on the host)
                                                              │
                                                              ├── writes CONTEXT.md
                                                              └── starts, detached:
                                                                  timeout <ttl> claude remote-control
                                                                      --permission-mode <mode>
```

Argus only ever sends a session label and free text over that SSH connection — never anything
that changes what gets launched or with what permissions. `permission_mode` and `ttl_seconds`
come from *this deployment's config* (the `launcher:` block), never from a break-glass
request's fields, so an agent's `request_break_glass` call can influence what a human reads,
never how the resulting session is scoped.

**Set up once, by hand, on the host that should run these sessions:**

1. Install and log in the `claude` CLI (`claude /login`, a Pro/Max/Team/Enterprise account with
   full-scope OAuth — not `ANTHROPIC_API_KEY`) as whatever account you'd normally do admin work
   as. Never as root.
2. Run `claude` once, interactively, in the directory `ARGUS_BREAKGLASS_SESSIONS_DIR` will use
   (default `~/.argus/breakglass-sessions`), to accept the workspace trust dialog.
3. `pip install argus` (or just this repo) on that host too, for the
   `argus-breakglass-launch` console script.
4. Generate a dedicated keypair (`ssh-keygen -t ed25519 -f argus_launcher`) and restrict it in
   that host's `~/.ssh/authorized_keys`:

   ```
   command="/path/to/argus-breakglass-launch",no-pty,no-port-forwarding,no-X11-forwarding,no-agent-forwarding ssh-ed25519 AAAA...
   ```

   That restriction is the actual security boundary: even if the private key leaked out of
   Argus's container, it can *only* ever run that one fixed script — nothing else, no shell, no
   port forwarding.
5. Mount the private key into wherever `argus agent serve` runs, and set:

   ```yaml
   breakglass:
     launcher:
       type: ssh
       host: theseus.internal   # or 127.0.0.1 if the host and Argus's container share a network
       user: argus-launcher
       identity_file: /run/secrets/argus_launcher_key
       permission_mode: acceptEdits   # manual | acceptEdits | dontAsk | auto
       ttl_seconds: 14400              # session is killed after 4h regardless; omit to disable
   ```

A launched session doesn't stream anywhere — you open it from your own Claude app once it's
running (Remote Control is outbound-only and tied to your account, so it just shows up there;
Argus never captures or relays a session URL). And because the context written to `CONTEXT.md`
is exactly whatever an agent's break-glass request or your own manual `/launch` input said,
it's labeled there as something to review, not follow blindly — this is the one place in the
whole system where agent-supplied text becomes input to a *more* capable, less constrained
session than the one that produced it.

## Adding a provider

A provider is a class with a `name` and a `tool_specs(config) -> list[ToolSpec]` method
(`src/argus/providers/base.py`) — the transport-agnostic classification + implementation. A
`register(mcp, policy, config)` method binds those specs onto the MCP server (usually just
`mcp_bind(mcp, policy, self.tool_specs(config))`); the agent binds the exact same specs directly
via `argus.agent.tools.langchain_bind`. Add one line to `PROVIDER_REGISTRY` in
`src/argus/providers/registry.py`, and it's configurable the same way `docker`/`breakglass` are.
No changes to the policy engine, approval backend, the agent graph, or any other provider are
needed. See [`docs/DESIGN.md`](docs/DESIGN.md#tool-binding-architecture) for the full picture.

## Development

```bash
uv sync --extra dev
uv run pytest
uv run ruff check src tests
```

Most of the suite runs without a real Postgres or a live MCP/model client — the Docker SDK,
approval backend, and chat models are mocked/faked throughout. A handful of tests
(`tests/agent/test_db_repo.py`, `test_checkpointer.py`, one case in `test_api_app.py`) exercise
a real Postgres and skip gracefully (not fail) if `task postgres:up` hasn't been run.

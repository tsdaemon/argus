# Argus

A pluggable, policy-gated MCP server for operating home infrastructure — without handing an
agent raw shell access.

The name follows a small mythological vocabulary from the design behind this project:
**Theseus** is the underlying system being operated on (in the reference deployment: a home
NAS, [tsdaemon/theseus](https://github.com/tsdaemon/theseus)); **Hermes** is whatever agent
runtime is calling in (a scheduled [Hermes Agent](https://github.com/NousResearch/hermes-agent)
run, Claude Code, or anything else that speaks MCP); **Argus** — the hundred-eyed watcher — is
this server, the one thing standing between an agent and anything that actually changes state.

## Why not just give the agent SSH?

Because then the security boundary is "hope the model doesn't do anything stupid." Argus makes
it deterministic instead:

- Every tool a provider exposes is classified **READ**, **MUTATE**, or **DESTRUCTIVE**.
- **READ** tools are always available.
- **MUTATE** tools only run after a real approval round-trip — the gate lives in Argus's code,
  not in the model choosing to ask first. See [Approval](#approval), below.
- **DESTRUCTIVE** tools are, by default, never even registered — they don't exist in the tool
  list an agent sees, not just refused when called.
- A `request_break_glass` tool exists for the case an agent genuinely can't resolve something
  with the tools it has — but it cannot grant anything by itself. See
  [Break-glass](#break-glass), below.

None of this is specific to any one backend. Argus core (policy engine + approval + the tool
registry) has no idea what Docker or theseus are — it only knows how to classify and gate. What
actually gets exposed is decided per deployment, in a config file.

```
Hermes (or any MCP client)          a phone browser
        │                                │
        │ MCP, bearer token              │ login (generated admin account)
        ▼                                ▼
┌───────────────────── argus serve (one process) ─────────────────────┐
│  /mcp                                    /breakglass                │
│  ├── docker.list_containers    READ      pending requests, Approve/ │
│  ├── docker.get_container_*    READ      Deny — separate login,     │
│  ├── docker.restart_container  MUTATE    separate auth surface      │
│  └── breakglass.request_break_glass READ                            │
│      (policy + approval boundary lives here)                        │
└───────────────────────────────────────────────────────────────────┘
```

## Quickstart

```bash
pip install -e '.[dev]'   # or: uv sync --extra dev
cp examples/argus.example.yaml my-argus.yaml   # edit to taste

export ARGUS_MCP_TOKEN=$(openssl rand -hex 24)   # Hermes sends this as a header
argus serve --config my-argus.yaml
```

Then open `/breakglass` — the first visit creates an admin account and shows the generated
password once (see [Break-glass](#break-glass)).

This runs one HTTP service carrying both `/mcp` (point your client — Hermes, Claude Code, ...
— at it with the bearer token above) and, if the breakglass provider is enabled, `/breakglass`
(see [Break-glass](#break-glass)). For local/dev use with a client that spawns Argus itself over
stdio, use `--transport stdio` instead — no network exposure, no token needed, but then there's
no listening port for the breakglass web view either.

## Running it

A [Taskfile](Taskfile.yaml) wraps the common commands (`task --list` for the full set):

```bash
task install        # uv sync --extra dev
task test            # pytest
task lint            # ruff
task run             # stdio, no config beyond examples/argus.example.yaml needed
task serve           # http, needs ARGUS_MCP_TOKEN — copy .env.example to .env first
```

**Docker**: `Dockerfile` + `docker-compose.yml` at the repo root are generic — no theseus- or
host-specific anything, safe to run anywhere:

```bash
cp .env.example .env   # fill in the tokens
task docker:up          # or: docker compose up -d --build
task docker:logs
```

The base compose file mounts `examples/argus.example.yaml` (or whatever `CONFIG_FILE` in `.env`
points at), a named volume for the breakglass sqlite store, and the Docker socket (needed for
the `docker` provider — see the comment in `docker-compose.yml` about why a read-only bind
there wouldn't actually restrict anything; scope access via `allowed_containers` in the config
instead).

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
short: `providers:` turns backends on and configures them, `policy:` sets the class defaults
(`default_mutate`, `default_destructive`) and any per-tool `overrides:` keyed
`"<provider>.<tool_name>"`. `${ENV_VAR}` in any config value is expanded from the environment at
load time, so tokens and topic URLs never need to be committed.

## Providers shipped in v1

- **`docker`** — container visibility (`list_containers`, `get_container_status`,
  `get_container_logs`, `inspect_container`) and `restart_container`, scoped to an
  `allowed_containers` list so even reads only ever see what a deployment has chosen to expose.
- **`breakglass`** — `request_break_glass`, plus the sqlite-backed request store shared with the
  `/breakglass` web view and the CLI.

**Roadmap** (same `Provider` shape, not built yet — see [Adding a provider](#adding-a-provider)):
`systemd` (unit status, journal logs, restart unit), `disk`/SMART (health, storage usage),
`network` (ping/probe a host). Explicitly **out of scope**: anything about the Notion "Digital
Home" inventory some deployments (including the theseus one) may keep — that's context the
operating agent reaches directly through its own separate Notion connection, not something
Argus reads or writes.

## Approval

MUTATE tools are gated with [FastMCP](https://gofastmcp.com)'s real elicitation primitive
(`ctx.elicit()`) — a blocking round-trip to the connected client, awaited *inside the tool
handler itself* before it does anything. The model cannot skip this the way it could skip an
advisory "please call this confirmation tool first" convention (which is what FastMCP's own
`Approval` app provider is — deliberately not used here for exactly that reason).

This is confirmed to work against [Hermes Agent](https://github.com/NousResearch/hermes-agent),
the reference client: it implements the standard `elicitation/create` callback (mcp Python SDK
≥1.11.0) and routes it through its own approval surface — an interactive prompt in the CLI/TUI,
or approval buttons on gateway platforms like Telegram/Slack for scheduled runs.

**Known gap**: MCP's newer "2026-07-28 era" protocol revision (SEP-2322/2575) removed the
server-initiated back-channel `ctx.elicit()` needs, replacing it with a two-round-trip
`InputRequiredResult` guard pattern instead. Against a client that has moved to that protocol
era, `ElicitApproval` catches the resulting error and **fails closed** — the action is refused,
never silently allowed. Supporting the guard pattern natively is a natural next step for a
client that needs it; it isn't guessed at here without something real to test it against.

The `ApprovalBackend` interface (`src/argus/approval/__init__.py`) is deliberately narrow so an
async approval-queue backend (for a client with no live human in the loop at all) can be added
later without touching provider or policy code.

## Break-glass

`request_break_glass(reason, target_host, evidence, proposed_objective)` exists for when an
agent genuinely can't resolve something with the READ/MUTATE tools it has. It's classified
READ — it cannot mutate anything, only record that a human needs to look. There is no tool
anywhere on the MCP surface that approves or executes one.

Break-glass is specifically for *unattended* runs — a scheduled check with nobody watching a
chat session. (If a human were live in an interactive session, they'd just answer the
`ctx.elicit()` prompt on a MUTATE tool directly.) That means the approval channel can't assume a
terminal or SSH session either — it needs to work from a phone with nothing installed beyond a
browser:

1. A pending request writes a row to a local sqlite store and fires a best-effort push (v1: via
   [ntfy](https://ntfy.sh) — no account, no API key, one HTTP POST to a topic URL you pick).
2. The push links straight into a minimal, plain-HTML, no-JS approval page served at
   `/breakglass` on the same `argus serve` process. No token to configure or embed in a
   URL: the first visit to `/login` generates a single admin account and shows the
   password once (save it — a password manager, not a note), then it's a normal
   username+password login backed by a signed session cookie. Log in, see the
   reason/evidence/target, tap Approve or Deny.
3. If a `launcher` is configured (see below), **approving also starts a Claude Code session on
   the host**, seeded with that request's reason/evidence/objective. If no launcher is
   configured, approving only flips the request's status — nothing opens on your behalf.

`argus breakglass list/approve/deny` (same CLI as `argus serve`) is kept as a secondary
convenience for when you're already at a terminal — it is not the designed path.

### Session launch (optional)

The one thing worth being deliberate about: everything else in Argus is scoped to a small,
specific, classified action. "Start a privileged, largely unrestricted Claude Code session" is
categorically bigger than any of that. So the launcher is opt-in, kept entirely off the MCP
surface (no agent ever triggers it — only the human-facing `/breakglass` and `/launch` web
routes do), and the security boundary lives on the machine that actually runs the session, not
in Argus's code:

```
approve (or /launch)  ──HTTP, logged in as admin──▶  argus serve
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
3. `pip install argus-mcp` (or just this repo) on that host too, for the
   `argus-breakglass-launch` console script.
4. Generate a dedicated keypair (`ssh-keygen -t ed25519 -f argus_launcher`) and restrict it in
   that host's `~/.ssh/authorized_keys`:

   ```
   command="/path/to/argus-breakglass-launch",no-pty,no-port-forwarding,no-X11-forwarding,no-agent-forwarding ssh-ed25519 AAAA...
   ```

   That restriction is the actual security boundary: even if the private key leaked out of
   Argus's container, it can *only* ever run that one fixed script — nothing else, no shell, no
   port forwarding.
5. Mount the private key into wherever `argus serve` runs, and set:

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

A provider is a class with a `name` and a `register(mcp, policy, config)` method
(`src/argus/providers/base.py`). Inside `register`, use `policy.register(mcp, tool_id=...,
tool_class=..., summary=...)` instead of `@mcp.tool` directly — that's what makes the
classification and gating actually apply. Add one line to `PROVIDER_REGISTRY` in
`src/argus/server.py`, and it's configurable the same way `docker`/`breakglass` are. No changes
to the policy engine, approval backend, or any other provider are needed.

## Development

```bash
uv sync --extra dev
uv run pytest
uv run ruff check src tests
```

Tests run without a real Docker daemon or a live MCP client — the Docker SDK and the approval
backend are mocked/faked throughout.

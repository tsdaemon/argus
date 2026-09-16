# Agent instructions for Argus

Handover notes for whoever (human or agent) picks this project up next. The README is the
user-facing pitch; this file is the "why," what's actually been verified vs. only unit-tested,
and what's still open. Read both.

## Status as of 2026-09-15

Fully built, 70 tests passing, ruff clean, pushed to `github.com:tsdaemon/argus` (2 commits:
"First version", "Logo and readme"). **Not yet deployed anywhere real** — `docker-compose.deploy.yml`
has never been created (only its `.example` template exists, deliberately gitignored), so nothing
is running on theseus yet. See [Immediate next steps](#immediate-next-steps).

The project started under an explicit "make this legitimately generic OSS, not theseus-specific"
constraint, then the operator dropped that framing partway through ("to hell with all this
generic thing, I am doing it just for myself and just for fun"). The pluggable architecture
stayed anyway — it was already built and costs nothing extra to keep — but don't over-invest in
generality for its own sake going forward; optimize for what's actually useful to run this
personally.

## What this is, in one paragraph

An MCP server that sits between an agent (Hermes, Claude Code, whatever) and home
infrastructure, so the agent never gets raw shell/SSH access. Every tool is classified READ
(free) / MUTATE (needs live approval via MCP elicitation) / DESTRUCTIVE (not registered at all
by default). A separate, deliberately weaker escape hatch — break-glass — lets an agent flag
"I can't resolve this, a human needs to look," reviewed and acted on from a phone, with an
optional path to actually spin up a privileged Claude Code session on a real host afterward.

## Architecture map

| Concern | File |
|---|---|
| Tool classification + enforcement (the actual security boundary), transport-agnostic | `src/argus/policy.py` |
| Approval `ApprovalBackend` protocol (transport-agnostic) | `src/argus/approval/__init__.py` |
| Approval via real MCP elicitation, fails closed (MCP-specific) | `src/argus/mcp/elicit.py` |
| Config schema + `${ENV_VAR}` expansion | `src/argus/config.py` |
| `/mcp` bearer auth | `src/argus/mcp/auth.py` |
| MCP server assembly, provider registry, combined HTTP app | `src/argus/mcp/server.py` |
| CLI (`argus serve`, `argus breakglass ...`) | `src/argus/cli.py` |
| Docker provider (list/status/logs/inspect/restart) | `src/argus/providers/docker_provider.py` |
| Break-glass provider + sqlite store | `src/argus/providers/breakglass_provider.py` |
| Break-glass web UI (login-gated) | `src/argus/mcp/webapp.py` |
| Generated-admin-account login + signed sessions (shared, not MCP-specific) | `src/argus/webauth.py` |
| ntfy push notifications | `src/argus/notify/` |
| SSH-based host session launcher (Argus side, shared) | `src/argus/launcher/ssh.py` |
| Forced-command host script (deployed separately, on the target host) | `src/argus/host_launch.py` |

`src/argus/mcp/` holds everything specific to the standalone MCP server surface (FastMCP
server assembly, `/mcp` auth, the break-glass web view). Everything else at the top level of
`src/argus/` (`policy.py`, `config.py`, `webauth.py`, `providers/`, `approval/` protocol,
`launcher/`, `notify/`, `host_launch.py`) is shared and transport-agnostic — an in-process
LangGraph agent harness (planned, not yet built) is meant to bind the same `providers/` tool
implementations directly, without going through `/mcp` at all. `mcp/` and any future agent
package should only ever depend on the shared top level, never on each other.

Extension point: a provider is a class with `name` + `register(mcp, policy, config)`, registered
in `PROVIDER_REGISTRY` in `server.py`. Use `policy.register(...)` instead of `@mcp.tool`
directly — that's what makes classification/gating actually apply. See "Adding a provider" in
the README.

## Design decisions and why (the parts worth not re-deriving)

- **Real elicitation, not FastMCP's `Approval` app provider.** The latter is explicitly
  documented as advisory-only (a tool the model is *asked* to call and can just skip). MUTATE
  tools call `ctx.elicit()` synchronously inside the handler, before doing anything — the gate is
  in Argus's Python, not model cooperation.
- **A real protocol gap exists and is deliberately not worked around.** MCP's "2026-07-28 era"
  revision (SEP-2322/2575) removed the server-initiated back-channel `ctx.elicit()` needs,
  replaced by a two-round-trip `InputRequiredResult` guard pattern. Implementing that pattern
  was scoped out because it couldn't be verified against a real client from this environment.
  `ElicitApproval` fails closed (refuses the action) if `ctx.elicit()` raises for this reason —
  confirm this is still the right tradeoff if/when the connecting client's protocol era changes.
  Confirmed (via docs research, not a live connection) that Hermes Agent uses the older, stable
  `elicitation/create` callback, so this was the correct call for the actual target client.
- **One combined HTTP service, not stdio + a separate web app.** Hermes supports remote MCP
  servers over HTTP with header-based auth (`url:` + `headers:` in its config) — it doesn't need
  to spawn Argus as a subprocess. `/mcp` and `/breakglass` are mounted on the same
  `mcp.http_app()` Starlette app (`server.py::build_http_app`), one process/port. `--transport
  stdio` still exists for local/dev use.
- **`/mcp` needs its own auth once it's network-reachable.** Stdio is trusted by construction;
  HTTP isn't. `StaticTokenVerifier` (`auth.py`) is a static bearer check — deliberately not OAuth,
  which would be real overhead unjustified for a single-operator homelab.
- **Break-glass is for *unattended* runs specifically** — a scheduled Hermes check with nobody
  watching a chat. (A live human would just answer the elicitation prompt directly.) That's why
  its approval channel can't assume a terminal/SSH session — it has to work from a phone with
  nothing installed beyond a browser.
- **No pre-shared breakglass token.** Originally designed with `ARGUS_BREAKGLASS_TOKEN` in the
  URL; replaced with a self-provisioning admin account (`webauth.py`) — first visit to `/login`
  generates a password, shows it once, PBKDF2-hashed thereafter, signed session cookie from
  there on. Nothing secret ever sits in a URL anymore.
- **Session launch (optional) uses a forced-command SSH key, not the Docker socket.** The docker
  *provider*'s socket mount is fine (narrow, allowlisted, that's its whole job). Using the same
  socket as an escalation path for spinning up a privileged session was rejected — it would put
  host-root-equivalent access inside the same process that's reachable over HTTP by an agent. A
  forced-command SSH key (`command="..." ` in `authorized_keys`, restricted to exactly one
  script) keeps the blast radius of a leaked key to that one fixed action.
- **`permission_mode`/`ttl_seconds` for a launched session come from Argus's own config, never
  from a break-glass request's fields.** An agent's `request_break_glass` call can influence what
  a human *reads*, never how the resulting session is launched or scoped.
- **No streaming UI for launched sessions.** `claude remote-control` is outbound-only and tied to
  the operator's Claude account — it just shows up in their app once started. Building a
  browser-terminal (xterm.js + pty bridge) was explicitly rejected as disproportionate: nobody
  does real debugging by thumb-typing into a phone.
- **`CONTEXT.md`, not an initial CLI prompt.** No documented way to combine `claude -p` (runs one
  task, exits) with `--remote-control` (stays alive, needs a live human) — an agent-supplied or
  human-supplied context file gets written into the session's working directory instead, and the
  human's first message once connected is effectively "read this."

## Verified live vs. only mock/unit-tested

Be honest about this distinction when extending things — a lot of the design got walked through
real processes, but a few load-bearing pieces have never touched the actual target
infrastructure:

**Verified against a real running process (not mocks):**
- `docker build` + `docker compose up`: real image, real container, `/mcp` 401→200 auth,
  `/breakglass` 403→200, all hit with `curl` against the actual built container.
- The full login flow (`argus serve` run for real): first-visit password generation/reveal,
  no re-reveal on second visit, wrong password rejected, correct password issues a working
  session cookie, unauthenticated access redirects to `/login`.
- `argus-breakglass-launch` (the host script): run for real with a fake `claude` shell script
  standing in for the real CLI — confirmed it writes `CONTEXT.md`, wraps in `timeout`, and truly
  detaches (the launched process keeps running after the launcher script's own process exits).
- `docker compose config` merge of the base + theseus deploy overlay (labels added, dev port
  dropped via `!reset []`, `DEPLOY_`-prefixed secrets correctly override).
- go-task's dotenv layering (`.env` + `.env.deploy` both load for a task with its own `dotenv:`
  — empirically confirmed in an isolated test, not assumed).
- `Taskfile.yaml` itself (`task --list`, `task test`, `task lint`).

**Not yet verified against the real thing:**
- An actual Hermes Agent instance connecting to `/mcp` and round-tripping an elicitation
  prompt. Everything here rests on Hermes's *documented* behavior, not an observed connection.
- The SSH launcher against a real host with a real forced-command `authorized_keys` entry —
  `SshHostLauncher` is unit-tested with a mocked subprocess only.
- The real `claude remote-control` CLI (only ever exercised against a fake shim). In particular,
  whether workspace trust / login state genuinely survives being invoked non-interactively via a
  forced SSH command the way the README assumes.
- Any real deployment to theseus (no `docker-compose.deploy.yml` has ever existed on disk here).
- The `docker` provider against a real Docker daemon with real containers (tests mock the SDK
  client entirely).

## Explicitly not built (roadmap, same `Provider` shape as `docker`/`breakglass`)

- `systemd` provider (unit status, journal logs, restart unit)
- `disk`/SMART provider (health, storage usage)
- `network` provider (ping/probe a host)
- SEP-2322 guard-pattern elicitation (for a client on the newer MCP protocol era)
- Async approval-queue backend for MUTATE tools with no live human in the loop at all
  (`ApprovalBackend` in `approval/__init__.py` is already shaped to allow this without touching
  provider code)

Explicitly **out of scope, not a gap**: nothing about the Notion "Digital Home" inventory belongs
in this repo — that's context the operating agent reaches through its own separate Notion
connection.

## Config / secrets reference

- `ARGUS_MCP_TOKEN` — required for `--transport http` (the default). Bearer token for `/mcp`.
- `ARGUS_NTFY_TOPIC_URL` — optional, break-glass push notifications.
- No more `ARGUS_BREAKGLASS_TOKEN` — replaced by the generated admin login.
- `ARGUS_BREAKGLASS_SESSIONS_DIR` — env var read by `host_launch.py` on the *target host*, not
  by `argus serve` itself. Defaults to `~/.argus/breakglass-sessions`.
- Full config schema: `examples/argus.example.yaml` (annotated) and
  `examples/theseus.argus.yaml` (real-shaped).

## Immediate next steps

1. Actually deploy to theseus: `cp docker-compose.deploy.yml.example docker-compose.deploy.yml`
   + `cp .env.deploy.example .env.deploy`, fill in real values, `task deploy`. This exercises the
   Traefik/Homepage labels and the theseus config for the first time for real.
2. Point a real Hermes instance at `/mcp` and confirm an elicitation round-trip actually works
   end to end — the whole approval mechanism's correctness currently rests on documentation, not
   an observed connection.
3. If/when the session launcher is wanted for real: set up the forced-command SSH key on
   theseus per the README, and confirm `claude remote-control` genuinely stays alive and
   reachable when started that way (the workspace-trust/login-persistence assumption is untested).
4. Consider CI (theseus has `.woodpecker.yml`; this repo has none yet) — at minimum, run
   `pytest` + `ruff` on push.

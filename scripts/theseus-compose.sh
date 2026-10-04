#!/usr/bin/env bash
# `docker compose` against theseus with the deploy overlay. The daemon is remote, so a bind
# mount or a `file:` secret would resolve on theseus, not here; instead the config and the
# SSH files are read here into env vars, which the overlay injects as configs and secrets.
# Expects .env and .env.deploy to be loaded (the Taskfile's deploy tasks do that).
set -euo pipefail

read_file() { [[ -r $1 ]] || { echo "Cannot read $1" >&2; exit 1; }; cat "$1"; }

# $(...) drops the final newline, and ssh rejects a private key without one: add it back.
# A Compose config, not a secret: its ${VAR} references reach the container unexpanded.
ARGUS_CONFIG_YAML="$(read_file "${CONFIG_FILE:?}")"$'\n'
# Secrets.
ARGUS_LAUNCHER_KEY="$(read_file "${ARGUS_LAUNCHER_KEY_PATH:?}")"$'\n'
ARGUS_SSH_KEY="$(read_file "${ARGUS_SSH_KEY_PATH:?}")"$'\n'
ARGUS_KNOWN_HOSTS="$(read_file "${ARGUS_KNOWN_HOSTS_PATH:?}")"$'\n'
# Home Assistant API token: a secret, like the keys; required, so a deploy without it fails
# here rather than at config load in the container. The overlay passes it as an env var.
ARGUS_HA_TOKEN="${DEPLOY_ARGUS_HA_TOKEN:?set DEPLOY_ARGUS_HA_TOKEN in .env.deploy}"
export ARGUS_CONFIG_YAML ARGUS_LAUNCHER_KEY ARGUS_SSH_KEY ARGUS_KNOWN_HOSTS ARGUS_HA_TOKEN

exec docker --context theseus compose --profile app \
  -f docker-compose.yml -f docker-compose.deploy.yml "$@"

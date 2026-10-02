#!/usr/bin/env bash
# Merge local and theseus state both ways, deleting nothing. Database rows (conversations,
# checkpoints, break-glass requests) missing on one side are copied from the other; a row
# both sides have keeps each side's version. Workspace files missing on one side are copied
# too; where both have a file with different contents, local wins (listed before you
# confirm). Theseus keeps its own admin account. Run through `task deploy:sync`.
set -euo pipefail
cd "$(dirname "$0")/.."

WORKSPACE=.argus/workspace
REMOTE_WORKSPACE=/var/lib/argus/agent-workspace
# Not copied: theseus's own login, and the two schema version markers (compared instead).
KEEP=(admin_account alembic_version checkpoint_migrations)

remote() { scripts/theseus-compose.sh "$@"; }
# rsync runs `$RSH argus rsync --server ...`: the far end is the argus container itself.
RSH="scripts/theseus-compose.sh exec -T"
sync_files() { rsync -rlt -e "$RSH" "$@"; }
local_pg() { docker compose exec -T postgres "$@"; }
remote_pg() { remote exec -T postgres "$@"; }
local_sql() { local_pg psql -U argus -d argus -Atc "$1"; }
remote_sql() { remote_pg psql -U argus -d argus -Atc "$1"; }

docker compose up -d --wait postgres >/dev/null

versions="select (select version_num from alembic_version) || ' ' || (select max(v) from checkpoint_migrations)"
if [[ $(local_sql "$versions") != $(remote_sql "$versions") ]]; then
  echo "Schema versions differ (local: $(local_sql "$versions"), theseus: $(remote_sql "$versions"))." >&2
  echo "Migrate local (task backend:migrate) or redeploy (task deploy) first." >&2
  exit 1
fi

overwritten=$(sync_files --checksum --existing --dry-run --out-format=%n \
  "$WORKSPACE/" "argus:$REMOTE_WORKSPACE/" | grep -v '/$' || true)

count="select count(*) from threads"
echo "Conversations: local $(local_sql "$count"), theseus $(remote_sql "$count"); each gets the other's."
if [[ -n $overwritten ]]; then
  echo "Workspace files on theseus to be replaced by the local version:"
  echo "$overwritten" | sed 's/^/  /'
fi
read -rp "Continue? [y/N] " answer
[[ $answer == [yY] ]] || exit 1

# INSERT ... ON CONFLICT DO NOTHING adds only missing rows. --disable-triggers lets rows load
# in any order despite foreign keys (argus is a superuser). Then the one sequence moves past
# every chat_order either side has used, whatever the dump's own setval said.
dump_args=(-U argus -d argus --data-only --schema=public --inserts --on-conflict-do-nothing
  --disable-triggers "${KEEP[@]/#/--exclude-table-data=}")
load_args=(psql -U argus -d argus -q -v ON_ERROR_STOP=1 --single-transaction)
fix_seq="select setval('public.messages_chat_order_seq', (select coalesce(max(chat_order), 1) from public.messages));"

# Files first, while argus runs: rsync goes through its container.
echo "Merging workspaces..."
sync_files --ignore-existing "argus:$REMOTE_WORKSPACE/" "$WORKSPACE/"
sync_files --checksum "$WORKSPACE/" "argus:$REMOTE_WORKSPACE/"

remote stop argus
trap 'remote start argus' EXIT

echo "Merging theseus's database into local..."
{ remote_pg pg_dump "${dump_args[@]}"; echo "$fix_seq"; } | local_pg "${load_args[@]}" >/dev/null
echo "Merging local's database into theseus..."
{ local_pg pg_dump "${dump_args[@]}"; echo "$fix_seq"; } | remote_pg "${load_args[@]}" >/dev/null

echo "Done: local $(local_sql "$count") conversations, theseus $(remote_sql "$count")."

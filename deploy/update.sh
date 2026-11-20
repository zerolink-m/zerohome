#!/usr/bin/env bash
# Run as the dedicated Docker operator/root. Requires Bash, flock, gzip, Python 3.
# ZEROHOME_EXTERNAL_DB=1 selects compose.external-db.yaml: no db container and no
# automatic backup (the operator backs up that database). Migration still runs
# under the MySQL lock when this node is the migration node.
# ZEROHOME_MIGRATE=0 updates only this node's app container and waits for the
# lock. Unset (or 1) keeps the single-node behavior: backup when the database
# is the Compose db service, then migrate, then restart.
set -Eeuo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${ZEROHOME_DEPLOY_CONFIG:-/etc/zerohome/deployment.conf}"
BACKUPS="${ZEROHOME_BACKUP_DIR:-/var/backups/zerohome}"
mkdir -p "$BACKUPS"
exec 9>"$BACKUPS/update.lock"
flock -n 9 || exit 0
cd "$ROOT"

external=0
case "${ZEROHOME_EXTERNAL_DB:-}" in
    1|true|TRUE|yes|YES) external=1 ;;
esac
if [[ "$external" == 1 ]]; then
    compose_file="$ROOT/compose.external-db.yaml"
else
    compose_file="$ROOT/compose.yaml"
fi
compose=(docker compose --env-file "$CONFIG" -f "$compose_file")

# Unset means "this node migrates", including a first install and the existing
# built-in updater. Additional nodes must set ZEROHOME_MIGRATE=0 explicitly.
migrate_role=1
case "${ZEROHOME_MIGRATE:-}" in
    0|false|FALSE|no|NO) migrate_role=0 ;;
esac

# Never source the config as shell code or print its secrets.
image="$("${compose[@]}" config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["app"]["image"])')"
docker pull "$image"
new_id="$(docker image inspect --format '{{.Id}}' "$image")"
container="$("${compose[@]}" ps -aq app)"
if [[ -n "$container" && "${1:-}" != --force ]]; then
    old_id="$(docker inspect --format '{{.Image}}' "$container")"
    healthy="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container")"
    if [[ "$old_id" == "$new_id" && "$healthy" == healthy ]]; then
        printf '%s\n' 'Stable image unchanged; nothing to do (use --force for configuration changes).'
        exit 0
    fi
fi
# Both migration and app use the identical immutable manifest, even if stable moves.
export ZEROHOME_IMAGE
ZEROHOME_IMAGE="$(docker image inspect --format '{{index .RepoDigests 0}}' "$image")"
[[ "$ZEROHOME_IMAGE" == *@sha256:* ]] || { printf '%s\n' 'No immutable registry digest found.' >&2; exit 1; }
# Never pull/recreate/upgrade an existing database container in this updater.
# For first install, the operator must explicitly pull db first.
# External database: there is no db service to start.
if [[ "$external" == 0 ]]; then
    "${compose[@]}" up -d --no-deps --no-recreate --pull never --wait --wait-timeout 360 db
fi

if [[ "$migrate_role" == 0 ]]; then
    # Wait before stopping the app. GET_LOCK blocks while the migration node
    # holds zerohome_migrate, then this process releases it without alembic.
    # If this runs before the migration node takes the lock, the wait returns
    # immediately: start that node first.
    printf '%s\n' 'Not the migration node (ZEROHOME_MIGRATE=0): waiting until the migration lock is free, then updating only this app container.'
    "${compose[@]}" run --rm --no-deps -e ZEROHOME_MIGRATE_WAIT=1 migrate
    "${compose[@]}" stop -t 60 app
    "${compose[@]}" up -d --no-deps --pull never --wait --wait-timeout 180 app
    printf 'Deployment healthy: %s\n' "$ZEROHOME_IMAGE"
    exit 0
fi

"${compose[@]}" stop -t 60 app
trap 'printf "%s\n" "Deployment failed. App may be stopped; inspect logs and schema before manual recovery. No automatic schema rollback." >&2' ERR
if [[ "$external" == 1 ]]; then
    printf '%s\n' 'External database: refusing automatic backup. This script will not run mysqldump and will not start a database container. Back up the external database yourself; schema migration still runs under the MySQL lock.'
else
    backup="$BACKUPS/$(date -u +%Y%m%dT%H%M%SZ)-$$.sql.gz"
    # Stop writes before taking an InnoDB-consistent logical backup. Password stays
    # inside the db container; pipefail prevents partial backups counting as success.
    "${compose[@]}" exec -T db sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysqldump --protocol=socket -uroot --single-transaction --routines --events --triggers --no-tablespaces --set-gtid-purged=OFF --databases "$MYSQL_DATABASE"' | gzip > "$backup.partial"
    gzip -t "$backup.partial"
    mv "$backup.partial" "$backup"
    printf 'Database backup saved to %s\n' "$backup"
fi
"${compose[@]}" run --rm --no-deps migrate
"${compose[@]}" up -d --no-deps --pull never --wait --wait-timeout 180 app
printf 'Deployment healthy: %s\n' "$ZEROHOME_IMAGE"

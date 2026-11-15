#!/usr/bin/env bash
# Run as the dedicated Docker operator/root. Requires Bash, flock, gzip, Python 3.
set -Eeuo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${ZEROHOME_DEPLOY_CONFIG:-/etc/zerohome/deployment.conf}"
BACKUPS="${ZEROHOME_BACKUP_DIR:-/var/backups/zerohome}"
mkdir -p "$BACKUPS"
exec 9>"$BACKUPS/update.lock"
flock -n 9 || exit 0
cd "$ROOT"
compose=(docker compose --env-file "$CONFIG" -f "$ROOT/compose.yaml")
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
"${compose[@]}" up -d --no-deps --no-recreate --pull never --wait --wait-timeout 360 db
"${compose[@]}" stop -t 60 app
trap 'printf "%s\n" "Deployment failed. App may be stopped; inspect logs and schema before manual recovery. No automatic schema rollback." >&2' ERR
backup="$BACKUPS/$(date -u +%Y%m%dT%H%M%SZ)-$$.sql.gz"
# Stop writes before taking an InnoDB-consistent logical backup. Password stays
# inside the db container; pipefail prevents partial backups counting as success.
"${compose[@]}" exec -T db sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysqldump --protocol=socket -uroot --single-transaction --routines --events --triggers --no-tablespaces --set-gtid-purged=OFF --databases "$MYSQL_DATABASE"' | gzip > "$backup.partial"
gzip -t "$backup.partial"
mv "$backup.partial" "$backup"
printf 'Database backup saved to %s\n' "$backup"
"${compose[@]}" run --rm --no-deps migrate
"${compose[@]}" up -d --no-deps --pull never --wait --wait-timeout 180 app
printf 'Deployment healthy: %s\n' "$ZEROHOME_IMAGE"

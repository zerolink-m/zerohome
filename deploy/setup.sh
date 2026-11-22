#!/usr/bin/env bash
# Fresh Debian/Ubuntu amd64 host bootstrap. This script is deliberately fail-safe:
# it refuses existing deployments/configuration and never removes Docker data.
set -Eeuo pipefail
umask 077

INSTALL_DIR=/opt/zerohome
CONFIG_DIR=/etc/zerohome
SERVICE=/etc/systemd/system/zerohome-update.service
TIMER=/etc/systemd/system/zerohome-update.timer
EXTERNAL_OVERRIDE=/etc/systemd/system/zerohome-update.service.d/external-db.conf
DRY_RUN=0

usage() {
    cat <<'EOF'
Usage: sudo bash deploy/setup.sh [--install-dir ABSOLUTE_PATH] [--config-dir ABSOLUTE_PATH] [--dry-run] [--help]

Installs ZeroHome on a fresh Debian/Ubuntu amd64 host. It does not migrate an
existing installation, remove packages, or delete Docker volumes/data.
Defaults: --install-dir /opt/zerohome --config-dir /etc/zerohome.
EOF
}

while (($#)); do
    case "$1" in
        --help|-h) usage; exit 0 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --install-dir)
            (($# >= 2)) || { printf '%s\n' '--install-dir requires a path' >&2; exit 2; }
            INSTALL_DIR=$2; shift 2 ;;
        --config-dir)
            (($# >= 2)) || { printf '%s\n' '--config-dir requires a path' >&2; exit 2; }
            CONFIG_DIR=$2; shift 2 ;;
        *) printf 'Unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done

validate_target_path() {
    local label=$1 path=$2
    [[ "$path" == /* && "$path" != / && "$path" =~ ^/[A-Za-z0-9._/-]+$ ]] \
        || fail "$label must be an absolute path containing only letters, digits, '.', '_', '-', and '/' (no spaces)"
    [[ "/$path/" != */../* ]] || fail "$label must not contain a '..' path component"
}

fail() { printf 'ZeroHome setup stopped: %s\n' "$*" >&2; exit 1; }
validate_target_path "--install-dir" "$INSTALL_DIR"
validate_target_path "--config-dir" "$CONFIG_DIR"
INSTALL_DIR="${INSTALL_DIR%/}"
CONFIG_DIR="${CONFIG_DIR%/}"
ask_yes() {
    local answer
    read -r -p "$1 [yes/no]: " answer
    [[ "$answer" == yes || "$answer" == YES ]]
}

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
[[ -f "$SOURCE_DIR/.git/HEAD" ]] || git -C "$SOURCE_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
    || fail "run this from a cloned Git checkout"
[[ -f "$SOURCE_DIR/templates/compose.yaml" && -x "$SOURCE_DIR/deploy/update.sh" ]] \
    || fail "checkout is missing the supported Compose templates or deploy/update.sh"

if [[ "$DRY_RUN" == 1 ]]; then
    printf 'Dry run only. Source checkout: %s\n' "$SOURCE_DIR"
    printf 'Install directory: %s\nConfiguration directory: %s\n' "$INSTALL_DIR" "$CONFIG_DIR"
    printf 'Would validate Debian/Ubuntu amd64, inspect Docker conflicts, optionally install Docker from its official apt repository, create root-only config, deploy via deploy/update.sh, bootstrap the first admin, and enable updates when approved. No changes made.\n'
    exit 0
fi
[[ "$EUID" == 0 ]] || fail "run with sudo or as root"
[[ -t 0 ]] || fail "interactive TTY required; run from a terminal"

[[ -r /etc/os-release ]] || fail "cannot identify operating system"
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}" in debian|ubuntu) ;; *) fail "supported hosts are Debian or Ubuntu" ;; esac
[[ "$(dpkg --print-architecture 2>/dev/null || true)" == amd64 ]] || fail "only amd64 hosts are supported by this release image"
[[ -f /etc/debian_version ]] || fail "Debian-family host required"

if [[ "$SOURCE_DIR" != "$INSTALL_DIR" ]]; then
    [[ ! -e "$INSTALL_DIR" ]] || fail "$INSTALL_DIR already exists; refusing to overwrite or merge an installation"
fi
for path in "$CONFIG_DIR/deployment.conf" "$CONFIG_DIR/app.conf" "$SERVICE" "$TIMER" "$EXTERNAL_OVERRIDE"; do
    [[ ! -e "$path" ]] || fail "$path already exists; inspect the existing deployment and migrate it manually"
done
[[ ! -L "$CONFIG_DIR" ]] || fail "$CONFIG_DIR is a symlink; refusing to write configuration through it"

if command -v docker >/dev/null 2>&1; then
    docker compose version >/dev/null 2>&1 || fail "Docker exists but Compose plugin is unavailable; install/repair it without removing existing Docker packages"
    if ! docker info >/dev/null 2>&1; then
        systemctl is-active --quiet docker && fail "Docker service is active but the Engine is not accessible to root"
        ask_yes "Docker is installed but stopped. Enable and start the existing Docker service now?" \
            || fail "Docker was not started; no deployment changes were made"
        systemctl enable --now docker
        docker info >/dev/null 2>&1 || fail "Docker Engine did not become operational"
    fi
    active="$(docker ps -a --filter label=com.docker.compose.project=zerohome --format '{{.ID}}')"
    [[ -z "$active" ]] || fail "active Compose project 'zerohome' detected; refusing a fresh install"
    existing_volumes="$(docker volume ls -q --filter label=com.docker.compose.project=zerohome)"
    [[ -z "$existing_volumes" ]] || fail "existing Compose data volumes for 'zerohome' detected; refusing a fresh install"
    INSTALL_DOCKER=0
else
    INSTALL_DOCKER=1
    for package in docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc; do
        if dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'install ok installed'; then
            fail "conflicting package $package is installed; resolve it manually, setup will not purge or replace packages"
        fi
    done
fi

if [[ -d "$CONFIG_DIR" ]] && find "$CONFIG_DIR" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
    fail "$CONFIG_DIR is not empty; refusing to overwrite existing configuration"
fi

printf '%s\n' 'Select database:' '  1) Built-in MySQL 8.4.8 container' '  2) Existing external MySQL'
read -r -p 'Choice [1/2]: ' DB_CHOICE
case "$DB_CHOICE" in
    1) DB_MODE=builtin ;;
    2) DB_MODE=external ;;
    *) fail "choose 1 or 2" ;;
esac
if [[ "$DB_MODE" == external ]]; then
    ask_yes "Confirm external DB backup and approval to run schema migrations?" \
        || fail "external DB migration was not approved"
    ask_yes "Enable unattended updates/migrations for the external DB? You confirmed its backup and maintenance process." \
        && ENABLE_TIMER=1 || ENABLE_TIMER=0
else
    ENABLE_TIMER=1
fi

if [[ "$INSTALL_DOCKER" == 1 ]]; then
    for path in /etc/apt/sources.list.d/docker.list /etc/apt/keyrings/docker.asc; do
        [[ ! -e "$path" ]] || fail "$path already exists; refusing to replace Docker repository configuration"
    done
    apt-get update
    apt-get install -y ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    arch="$(dpkg --print-architecture)"
    codename="${VERSION_CODENAME:-}"
    [[ -n "$codename" ]] || fail "could not determine distro codename for Docker's official apt repository"
    printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/%s %s stable\n' \
        "$arch" "$ID" "$codename" > /etc/apt/sources.list.d/docker.list
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
fi
docker compose version >/dev/null 2>&1 || fail "Docker Compose plugin is not operational"
docker info >/dev/null 2>&1 || fail "Docker Engine is not operational after setup"
active="$(docker ps -a --filter label=com.docker.compose.project=zerohome --format '{{.ID}}')"
[[ -z "$active" ]] || fail "active Compose project 'zerohome' detected; refusing a fresh install"
existing_volumes="$(docker volume ls -q --filter label=com.docker.compose.project=zerohome)"
[[ -z "$existing_volumes" ]] || fail "existing Compose data volumes for 'zerohome' detected; refusing a fresh install"

if [[ "$SOURCE_DIR" != "$INSTALL_DIR" ]]; then
    install -d -o root -g root -m 0755 "$INSTALL_DIR"
    # Copy the committed public checkout only; this avoids carrying ignored local
    # files such as .env, caches or credentials into the deployment directory.
    git -C "$SOURCE_DIR" archive --format=tar HEAD | tar -xf - -C "$INSTALL_DIR"
fi
install -d -o root -g root -m 0700 "$CONFIG_DIR"
python3 "$INSTALL_DIR/deploy/setup_config.py" --mode "$DB_MODE" --config-dir "$CONFIG_DIR"

if [[ "$DB_MODE" == builtin ]]; then
    export COMPOSE_DISABLE_ENV_FILE=1
    docker compose --env-file "$CONFIG_DIR/deployment.conf" -f "$INSTALL_DIR/templates/compose.yaml" pull db
    (cd "$INSTALL_DIR" && ZEROHOME_DEPLOY_CONFIG="$CONFIG_DIR/deployment.conf" bash ./deploy/update.sh)
else
    export ZEROHOME_EXTERNAL_DB=1
    (cd "$INSTALL_DIR" && ZEROHOME_DEPLOY_CONFIG="$CONFIG_DIR/deployment.conf" ZEROHOME_EXTERNAL_DB=1 bash ./deploy/update.sh)
fi

if ! python3 -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/v1/health", timeout=5).read()' >/dev/null 2>&1; then
    fail "app health check failed; deployment files/data were preserved; inspect docker compose logs"
fi

printf '\n%s\n' 'Create the first administrator now. Password input is hidden.'
if [[ "$DB_MODE" == builtin ]]; then
    compose_file="$INSTALL_DIR/templates/compose.yaml"
else
    compose_file="$INSTALL_DIR/templates/compose.external-db.yaml"
fi
docker compose --env-file "$CONFIG_DIR/deployment.conf" -f "$compose_file" \
    exec -it app python -m deploy.bootstrap_admin

if [[ "$ENABLE_TIMER" == 1 ]]; then
    [[ ! -e "$SERVICE" ]] || fail "$SERVICE appeared during setup; refusing to overwrite it"
    [[ ! -e "$TIMER" ]] || fail "$TIMER appeared during setup; refusing to overwrite it"
    # Exclusive shell redirections keep a file that appears after preflight
    # from being truncated or replaced.
    set -o noclobber
    cat > "$SERVICE" <<EOF
[Unit]
Description=Backup, migrate and update ZeroHome stable image
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$INSTALL_DIR
Environment=ZEROHOME_DEPLOY_CONFIG=$CONFIG_DIR/deployment.conf
ExecStart=$INSTALL_DIR/deploy/update.sh
TimeoutStartSec=30min
UMask=0077
EOF
    chmod 0644 "$SERVICE"
    cat > "$TIMER" <<'EOF'
[Unit]
Description=Check for a new ZeroHome stable image every 15 minutes

[Timer]
OnBootSec=10min
OnUnitInactiveSec=15min
RandomizedDelaySec=2min
Unit=zerohome-update.service

[Install]
WantedBy=timers.target
EOF
    chmod 0644 "$TIMER"
    if [[ "$DB_MODE" == external ]]; then
        install -d -m 0755 /etc/systemd/system/zerohome-update.service.d
        [[ ! -e "$EXTERNAL_OVERRIDE" ]] || fail "$EXTERNAL_OVERRIDE appeared during setup; refusing to overwrite it"
        cat > "$EXTERNAL_OVERRIDE" <<'EOF'
[Service]
Environment=ZEROHOME_EXTERNAL_DB=1
EOF
        chmod 0644 "$EXTERNAL_OVERRIDE"
    fi
    systemctl daemon-reload
    systemctl enable --now zerohome-update.timer
else
    printf '%s\n' 'Automatic updates are disabled for the external DB. Run deploy/update.sh manually after a verified backup.'
fi

printf '\nZeroHome is healthy at http://127.0.0.1:8000\n'
printf 'Admin API: POST /v1/auth/login; Runtime configuration: GET/PATCH /v1/config/runtime\n'
printf '%s\n' 'The app is bound to loopback. Configure a reverse proxy and TLS before exposing it; do not expose the raw app port publicly.'

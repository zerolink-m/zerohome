# Docker deployment and background tasks

This is an **opt-in deployment to a Docker host**, not an automatic conversion of an existing systemd installation or database. Nothing here installs a timer, moves data, or starts containers on the current server. Keep the existing production service until a separately planned migration/cutover.

## Image and publication

- Image: `ghcr.io/zerolink-m/zerohome:stable` (public GHCR package owned by the **user** `zerolink-m`).
- `.github/workflows/docker-release.yml` builds `linux/amd64` on Ubuntu 24.04. `workflow_dispatch` and pushes to `release/docker-task-controls` validate builds **without publishing**. GitHub requires a workflow to be on the default branch before it can be manually dispatched; the branch-push trigger permits first-build validation before merging.
- A published GitHub Release builds and pushes `sha-<full-commit-sha>`, verifies the package is public, then promotes that exact digest to the lower-case release tag. A release not marked prerelease moves `stable`; prereleases get a version tag but never `stable`.
- Releases are serialized, not cancelled mid-publication. Publishing an older release later deliberately moves `stable` to that release; publish in intended deployment order. A failed build/privacy check never promotes `stable`.
- Publishing uses only the workflow's `GITHUB_TOKEN` with `contents: read` and `packages: write`; no publishing PAT or deployment-server secrets are required. The OCI source label links the image to `https://github.com/zerolink-m/zerohome`. If an existing package does not grant this repository Actions access, grant it in package settings rather than bypassing the check.

### Package visibility is checked, never changed automatically

Before publishing tags, the workflow reads `GET /users/zerolink-m/packages/container/zerohome` and requires `visibility: public`. Missing packages, non-public visibility, authorization failures and unexpected responses stop publication. The workflow never changes package visibility. If readback is temporarily unavailable, it fails closed; investigate and rerun, rather than assuming success.

The image can be pulled anonymously. If an administrator changes visibility away from public, the next workflow refuses to publish; this check cannot undo an external visibility change.

### Build characteristics

`docker build --platform linux/amd64 --build-arg VERSION=v4.5.0-Release -t zerohome:local .`

The deny-by-default build context and explicit COPY list exclude environment files, Git history, venvs, logs, caches, motion photos, tests and local model files. The image uses Python 3.14.4, FFmpeg/ffprobe 8.0.1 (amd64 digest pinned), and exact Python dependency versions. CUDA/NVIDIA/triton entries from the workstation freeze are omitted; torch 2.14.1 and torchvision 0.29.1 come from the CPU-only PyTorch index. Build checks include `pip check`, CPU-only verification and imports of native ML/media libraries. YOLOv8n weights are downloaded from a versioned upstream release and checksum-verified at build time. EasyOCR downloads its models on first use and stores them in the persistent models volume; allow outbound access or preseed that volume before an offline deployment.

This is a CPU image, not a GPU-enabled or ARM build. Native dependencies and ML models still make it substantial. Python/MySQL version tags and Debian security packages are not a bit-for-bit reproducible snapshot; deploy immutable published image digests when exact reproducibility is required. Updating base pins and rebuilding is an explicit maintenance task.

## First deployment on the target host

For a genuinely fresh Debian/Ubuntu amd64 host, the supported interactive path is:

```sh
git clone https://github.com/zerolink-m/zerohome.git
cd zerohome
sudo bash deploy/setup.sh
```

The script fails closed if `/opt/zerohome`, ZeroHome config files/systemd updater units, or an active Compose project already exists. It does not merge or overwrite an existing deployment. `--dry-run` prints the planned work without changing the host. It installs Docker from Docker's official distro-specific apt repository only when Docker is absent; conflicting packages are left untouched and require operator resolution. Only Debian/Ubuntu amd64 is supported by the published image. The setup itself has not been exercised on a separate clean host; do not treat this documentation as evidence of a live fresh-host installation.

The setup is fail-safe rather than a transactional host rollback: if it stops after creating `/opt/zerohome` or `/etc/zerohome` (for example, because an external DB credential was wrong), it leaves those files and any initialized volumes intact. Do not delete them and blindly rerun. Correct the root-only config after reviewing it, then continue with the matching Compose command and `deploy/update.sh`; if a migration may have started, inspect its status before retrying.

The installer prompts for built-in MySQL or an external MySQL service, creates `/etc/zerohome/deployment.conf` and `app.conf` with mode `0600`, and never prints generated/database passwords. It copies committed checkout files to `/opt/zerohome` (not ignored local files), then uses the shipped `deploy/update.sh`; it does not invoke bare `docker compose up` or bypass the migration lock. Built-in MySQL's update timer is enabled only after migration, health check, and first-admin creation succeed. For an external DB, initial migration requires explicit confirmation of a backup. Unattended updates/migrations remain disabled unless the operator separately confirms the external backup and maintenance process.

After the app health check, the installer runs `docker compose exec -it app python -m deploy.bootstrap_admin`. Enter the first administrator details in the terminal; the password is hidden and never supplied as a command-line argument. This is an operator-only command inside the already-running app container, not a public endpoint. It serializes against other bootstrap attempts using `system_settings` and refuses to run if an admin exists. Registration continues to create only ordinary users. Keep the service on its default loopback bind until a reverse proxy with TLS and WebSocket support is ready.

For manual deployment or an already-prepared host, continue with the detailed steps below.

Requirements: Linux amd64, Docker Engine + modern Compose v2/v5 (`up --wait`), Bash, `flock`, gzip and Python 3. Docker access is root-equivalent. Use a dedicated trusted operator. Put this checkout (including `templates/compose.yaml` and `deploy/`) at `/opt/zerohome`; if you use another location, adjust the service unit accordingly.

1. The published `ghcr.io/zerolink-m/zerohome` package is public, so a fresh installation can pull it anonymously. For a private fork/package, authenticate as the same OS user that runs the updater, using a GitHub PAT with `read:packages`; do not place that token in this repository or a command-line argument:

   ```sh
   read -rs GHCR_TOKEN
   printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u YOUR_GITHUB_LOGIN --password-stdin
   unset GHCR_TOKEN
   ```

   Docker stores credentials in that user's Docker configuration; configure a credential helper or restrict access. Logging in as an interactive non-root user does not authenticate the root systemd service.

2. Create **host-managed**, mode-0600 files under `/etc/zerohome` (not tracked and never baked into the image). Generate independent long random secrets. `/etc/zerohome/deployment.conf` is read by Compose, not sourced as shell code:

   ```ini
   DB_NAME=zerocams
   DB_USER=zerocams
   DB_PASSWORD=REPLACE_WITH_RANDOM_DATABASE_PASSWORD
   MYSQL_ROOT_PASSWORD=REPLACE_WITH_DIFFERENT_RANDOM_ROOT_PASSWORD
   ZEROHOME_CONFIG=/etc/zerohome/app.conf
   ZEROHOME_BIND=127.0.0.1
   ZEROHOME_IMAGE=ghcr.io/zerolink-m/zerohome:stable
   ```

   `/etc/zerohome/app.conf` is passed into the container as process environment. Application runtime configuration is stored in the database; these two explicitly named bootstrap values import existing signing keys once when the database does not yet contain them:

   ```ini
   BOOTSTRAP_JWT_SECRET_SESSION=EXISTING_SESSION_SIGNING_SECRET_IF_MIGRATING
   BOOTSTRAP_JWT_SECRET_SHARE=EXISTING_SHARE_SIGNING_SECRET_IF_MIGRATING
   ```

   These examples are documentation, not generated environment files. Compose sets the internal database host/password and port explicitly; MySQL root credentials are not injected into the app. Use Compose-compatible quoting if values contain special characters. Remove the bootstrap signing values after the first successful initialization; the DB copy is then authoritative. `VERSION` is removed from the app process environment; `APP_VERSION` comes from `app/version.py`. Never copy actual secret values into documentation, command output, or logs.

### Перенос настроек и ключей

The application never reads a legacy `.env` automatically. To explicitly transfer runtime settings from an existing dotenv-format file, first complete the database migration and start the app once so its persisted node identity is registered. From `/opt/zerohome`, use the same Compose env file and definition as the deployment commands above. Set `LEGACY_FILE` to the protected legacy file's host path; do not change its permissions or copy it over the deployment `.env`. `docker cp` can read a root-only source through the Docker daemon, and the temporary copy is handled only inside the container as root. The app normally runs as UID 10001.

```sh
cd /opt/zerohome
compose=(docker compose --env-file /etc/zerohome/deployment.conf -f templates/compose.yaml)
LEGACY_FILE=/path/to/protected/legacy.env
app_id=$("${compose[@]}" ps -q app)
test -n "$app_id"
docker cp "$LEGACY_FILE" "$app_id":/tmp/zerohome-legacy-import.env
"${compose[@]}" exec --user 0 -T app python -m deploy.import_legacy_settings --source /tmp/zerohome-legacy-import.env
```

Review the dry-run counts and setting names; the command does not print values. If they are correct, apply and remove the temporary copy even if the import fails:

```sh
cd /opt/zerohome
compose=(docker compose --env-file /etc/zerohome/deployment.conf -f templates/compose.yaml)
app_id=$("${compose[@]}" ps -q app)
test -n "$app_id"
set +e
"${compose[@]}" exec --user 0 -T app python -m deploy.import_legacy_settings --source /tmp/zerohome-legacy-import.env --apply
import_status=$?
"${compose[@]}" exec --user 0 -T app rm -f /tmp/zerohome-legacy-import.env
cleanup_status=$?
if [ "$cleanup_status" -ne 0 ]; then
  echo "Could not remove temporary legacy import copy; remove /tmp/zerohome-legacy-import.env from app container $app_id as root." >&2
fi
if [ "$import_status" -ne 0 ]; then exit "$import_status"; fi
if [ "$cleanup_status" -ne 0 ]; then exit "$cleanup_status"; fi
```

The importer inherits the app process's `DB_*` environment, validates values through `Settings`, and ignores database, listener, unknown, and other bootstrap fields. It refuses to apply if `system_settings.revision` is nonzero; that revision advances to `1` after a successful import, so an import cannot be repeated or overwrite configuration after an API edit. Keep the protected host source as your backup; the temporary container copy is removed by the command above. Restart the app after an applied import to load the database values. Do not set runtime fields as environment variables as a substitute: normal startup ignores them.

The first user created by registration has role `user`; create the first administrator only through `docker compose exec -it app python -m deploy.bootstrap_admin` after the app health check. This operator-only command prompts for the password and refuses to run if an admin already exists. No public self-promotion flow is provided.

3. Explicitly pull MySQL for first installation, then use the migration-aware deploy script:

   ```sh
   cd /opt/zerohome
   docker compose --env-file /etc/zerohome/deployment.conf -f templates/compose.yaml pull db
   sudo ./deploy/update.sh
   docker compose --env-file /etc/zerohome/deployment.conf -f templates/compose.yaml ps
   curl --fail http://127.0.0.1:8000/v1/health
   ```

   Run all Docker commands under the authenticated operator (omit `sudo` if already root). The updater pulls the application, pins its digest, starts/waits for the existing or freshly initialized DB without upgrading it, stops the app, backs up the database, runs `alembic upgrade head` while holding MySQL `GET_LOCK('zerohome_migrate')` (`deploy/migrate_locked.py`) in a single disposable `migrate` container, and only then starts/waits for the app. Initial deployment also takes a backup of the newly initialized empty database. The app image itself never runs migrations at startup. **Use the script, not a bare `docker compose up -d app`, to deploy or update.** The maintenance-profile migrate service is not a continuously running replica.

The built-in file runs one app container. More than one node is the external-database layout below; camera work is then leased instead of started for every camera on every node. The container runs as UID/GID 10001, drops capabilities and uses an init process for child reaping. Named volumes persist MySQL data, motion event photos, OCR/model state, and node identity (`/data/zerocams`) independently. Cache/process registries and temporary media are disposable tmpfs. Never run `docker compose down -v` on a live installation. A healthcheck is HTTP **liveness**, not proof of camera reachability or database correctness; DB health and migration success are checked separately by deployment.

The HTTP port is loopback-only by default; terminate HTTPS in a reverse proxy with WebSocket support and appropriate streaming timeouts. MySQL has no published host port. Camera RTSP/ONVIF addresses must be reachable from the Docker bridge network. WebRTC needs explicit UDP routing: choose `WEBRTC_UDP_PORT_MIN/MAX`, set `WEBRTC_PUBLIC_IP` and publish/forward the same UDP range in a local Compose override; HTTP proxying alone is insufficient. Supply that override explicitly in the updater's Compose command if used (the shipped script reads `templates/compose.yaml`, or `templates/compose.external-db.yaml` when `ZEROHOME_EXTERNAL_DB=1`, and no further file). Review firewall/NAT and STUN/TURN client settings for the target host; do not assume the existing host configuration automatically transfers into bridge networking.

## One node, an external database, or a second node

Node identity, cluster settings, storage integration and node capabilities are DB-managed. Compose only supplies database bootstrap credentials, the listener bind (`HOST`/`PORT`) and initial container paths that seed the first node's local settings. Later edits to those paths belong in the database. The named identity volume preserves the generated node ID across container recreation. `MIGRATE_LOCK_TIMEOUT_SECONDS` (default 300) is a migration-tool option, not an app runtime setting.

### One node, built-in database

Leave the variables above unset and run `./deploy/update.sh` with `templates/compose.yaml`, as in the first-deployment steps. The script starts the existing `db` container without recreating it, stops the app, takes the logical backup, migrates, and starts the app. `ZEROHOME_MIGRATE` stays unset: this node is the migration node. Do not point a second server at this Compose database; MySQL has no published port.

### External database

`templates/compose.external-db.yaml` is a separate file, not an overlay. Do not combine it with `templates/compose.yaml`. An overlay would still interpolate the built-in file, still require `MYSQL_ROOT_PASSWORD`, and still describe a `db` service. The external file has `app` and `migrate` only: no `db` service and no `depends_on`. The project name stays `zerohome`, so the motion and model volumes keep their names. Do not `up` both files at once.

The app reads `DB_HOST`, `DB_PORT` (default 3306), `DB_USER`, `DB_PASSWORD` and `DB_NAME`. `DB_READ_TIMEOUT_SECONDS` (default 30) is passed to asyncmy as `read_timeout` on every pooled connection, including pool pre-ping, so a silent MySQL fails the read instead of holding a pool slot until the pool is exhausted; `pool_pre_ping` stays on and `pool_recycle` stays 3600. `DB_WRITE_TIMEOUT_SECONDS` (default 30) is passed only when this asyncmy accepts `write_timeout` (the installed driver does not). There is no second set of names and no `MYSQL_ROOT_PASSWORD`. `DB_HOST` must be reachable from the Compose network. `127.0.0.1` inside the container is the container, not the host; for MySQL on the same machine use a reachable address, or a local override with `extra_hosts: ["host.docker.internal:host-gateway"]` and `DB_HOST=host.docker.internal`.

```sh
ZEROHOME_EXTERNAL_DB=1 sudo -E ./deploy/update.sh
```

The script then uses `templates/compose.external-db.yaml`. It does not start a database container and it does not run `mysqldump`. That refusal is deliberate: back up the external database yourself before the run. Schema migration still runs, once, under the MySQL lock. The unattended timer is for the built-in single-node stack; do not enable it here unless backups are handled outside the script.

### Second node

Every node uses the external file and the same writable database. Replication is the operator's job. The application needs one writer for leases, notification jobs and sessions. It does not support multi-master writes. Read replicas are not used.

After first startup, configure cluster mode and that node's own `NODE_ADDRESS` (`host`, `host:port` or `[ipv6]:port`) in the DB. The identity is generated and persisted in that node's dedicated named volume. Set the shared `CLUSTER_SECRET` in DB configuration when the media listener is not loopback; it authenticates the handshake but does not encrypt. Publish the fMP4 port for the other nodes and the HTTP port for clients. Neither Compose file opens 4700. Give each node its own cache volume. Do not share one.

Exactly one node migrates. On that node leave `ZEROHOME_MIGRATE` unset or set it to `1`, with `ZEROHOME_EXTERNAL_DB=1`. On every other node:

```sh
ZEROHOME_EXTERNAL_DB=1 ZEROHOME_MIGRATE=0 sudo -E ./deploy/update.sh
```

Those nodes do not back up and do not run Alembic. They wait until `zerohome_migrate` is free, then replace only their app container. Start them after the migrating node has taken the lock, or after it has finished. A follower that starts earlier can pass the wait before the lock exists. Raise `MIGRATE_LOCK_TIMEOUT_SECONDS` if a migration can still be running when followers start waiting. The lock is one database session held for the whole `alembic upgrade head`, then released. Do not enable `zerohome-update.timer` on more than one host.

## Automatic stable updates (opt-in)

The supplied timer checks for a changed stable digest about every 15–17 minutes. It does not depend on an external updater image and never upgrades MySQL. All Compose services have `com.centurylinklabs.watchtower.enable=false` so a label-respecting Watchtower cannot bypass this migration-aware sequence. Do not run any other updater against these containers.

After a successful manual deployment on the **target host**:

```sh
sudo install -m 0644 deploy/zerohome-update.service deploy/zerohome-update.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now zerohome-update.timer
sudo systemctl list-timers zerohome-update.timer
sudo journalctl -u zerohome-update.service
```

Disable with `sudo systemctl disable --now zerohome-update.timer`. No timer is installed/enabled by this repository or by an image build. A healthy container already using the pulled image is left untouched. To apply host configuration changes with an unchanged image, run `sudo ./deploy/update.sh --force`. A `flock` lock prevents overlapping runs. To pin a deployment, set `ZEROHOME_IMAGE` to a release tag or immutable digest in the host config and disable the timer if no polling is wanted.

### Downtime, backups and failures

Updates include downtime: the app is stopped before backup and remains stopped through schema migration and restart. Backups are InnoDB-consistent logical dumps of the application database (tables, triggers, routines and events), written with restricted permissions to `/var/backups/zerohome/*.sql.gz`. Passwords stay inside the DB container's environment, not in backup commands/logs. A failed dump, compression or gzip validation prevents migration and app startup; incomplete backups keep `.partial`. A failed migration also prevents starting the new app. No image or schema rollback is automatic: MySQL DDL can commit partially and old application code may not support the new schema.

Monitor the service journal and backup disk space. Backups are **not** automatically deleted; define retention, off-host storage/encryption and restore drills. Motion/model volumes and host configuration need separate backups; the SQL dump does not contain photos. Do not permit other writers during the maintenance window. For large databases, replace the backup step with a tested, fail-closed backup mechanism appropriate to your recovery objectives. On failure, disable the timer before investigating (otherwise the next poll retries), inspect migration/app logs, and plan a manual schema repair or restoration together with the matching prior image and photo backup. Restoring a backup loses later writes. MySQL upgrades are separate operator-managed maintenance, with their own compatibility tests and backups.

Fresh MySQL installs create Alembic's version table with a 128-character revision column because several historical revision IDs exceed Alembic's 32-character default. Historical IDs are unchanged. This creation hook does not alter an existing version table; when upgrading an older/partially initialized database with `VARCHAR(32)`, inspect its schema and arrange a backed-up manual widening before crossing those historical revisions. Do not blindly rerun failed nontransactional migrations.

Moving an existing production DB into the Compose volume is **not** performed by this setup. Plan and rehearse a stopped-writer export/import, preserve credentials and signing keys as appropriate, verify the Alembic revision, migrate, and cut over explicitly. Never point the fresh MySQL service at arbitrary existing host database files.

## Existing `/opt/zerocams` host: systemd to Docker, host MySQL retained

`compose.host.yaml` is a **standalone, host-specific alternative**, not an override to merge with `templates/compose.yaml`. It has only `app`: no database container, migration service, automatic schema changes or image pulls. **The shipped `deploy/update.sh` and `zerohome-update.timer` are NOT compatible with this deployment**: they assume Compose-managed MySQL and run migrations. Do not install/enable them; ensure no previously installed updater is active before cutover. Watchtower is explicitly excluded.

Inspected host settings: systemd runs `/opt/zerocams/main.py` as root, with working directory `/opt/zerocams` and `KillMode=control-group`; HTTP binds `0.0.0.0:3805`, MySQL is `127.0.0.1:3306`, WebRTC uses UDP `49152–65535`. Host networking retains these routes and the existing firewall/NAT rules; Docker port publishing is deliberately absent. It also exposes the container to host-network services: this is not bridge-network isolation.

The existing `/opt/zerocams/.env` is not read by the new Settings source. Transfer DB connection values to the host-managed container environment, and import existing signing keys with the `BOOTSTRAP_JWT_SECRET_*` names before the new app's first startup against a database without runtime settings (see above). Runtime settings move to the DB. If the host bind/port is deliberately changed later, update both Compose environment and healthcheck.

### Storage and privilege choices

- `/opt/zerocams/motion_files` is bind-mounted read/write at `/data/motion_files`. Existing photos and ownership remain unchanged; normal application retention cleanup still applies after startup.
- `/opt/zerocams/caching` is **not mounted or copied**. Snapshots are regenerable; all ingest/transcode/HLS/WebRTC/snapshot/motion PID registries go into private `/data/cache` tmpfs. HLS segments use private `/tmp/zerocams/hls` tmpfs. Never share the host PID namespace or copy its process registries. These caches disappear on container stop.
- Existing `/root/.EasyOCR` is narrowly mounted **read-only** at `/data/models/.EasyOCR`; no other `/root` files are exposed. The inspected cache contains `model/craft_mlt_25k.pth`, `model/cyrillic_g2.pth` and `user_network/`, matching the app's English/Russian EasyOCR reader. A missing/corrupt/incompatible model cannot be downloaded over this read-only mount: test OCR after cutover. If replacement models are needed, prepare a separate container-owned copy and change only this bind source, rather than modifying production cache ownership.
- The fresh `zerohome-host_models` volume holds Ultralytics settings and other model-home caches. `nocopy` avoids inheriting UID 10001 ownership from the image. Existing `/root/.config/Ultralytics` is not required/copied; it is library configuration, not the app's detection model. YOLO loads `/app/yolov8n.pt`, the image's pinned/checksummed weights, not the host's untracked `yolov8n.pt`.
- `frontend_versions.json` is read-only application input shipped in the image. Logging is stdout with bounded Docker log rotation; the host's existing `app.log` is not mounted. Camera recordings remain on the cameras; no additional local recording archive path was found.
- The host-only config runs as `0:0`, matching systemd and allowing writes to root-owned motion data **without chmod/chown**. It drops all capabilities and uses `no-new-privileges` and a private PID namespace, but root plus host networking is weaker isolation than the default non-root deployment. Do not add Docker-socket or host-root mounts. Run only one app instance.

### Operator-only preparation and cutover (not automatic)

First verify a current recoverable host-MySQL backup plus photos/config backup, and that the chosen release is compatible with the existing schema **without migration**. If not, stop here and plan a separate schema change. Build the reviewed final release commit into `zerohome-host:v4.5.0-release`, with `--build-arg VERSION=v4.5.0-Release`, from an isolated checkout of that immutable commit. Do not merely retag `zerohome-release-check:v4.5.0-release`: it may predate the final fixes. Record the source commit and resulting image ID; do not overwrite the tag during cutover.

Use the explicit file and an empty Compose interpolation env file every time. The service's `env_file` injects `/opt/zerocams/.env` into the process, but the new Settings source intentionally ignores ordinary application runtime keys from it. Do not print `docker compose config`, container environment, raw logs, SQL rows or camera URLs; use quiet validation or narrow output instead. Do not source `.env` in a shell. Before stopping systemd, transfer required bootstrap values using protected host-managed configuration and keep signing-key values out of output.

Read-only preflight, safe output:

```sh
compose=(docker compose --env-file /dev/null -f /opt/zerocams/compose.host.yaml)
"${compose[@]}" config --quiet
"${compose[@]}" config --services  # must list only app
# Inspect only identity/version, never the full image/container environment.
docker image inspect --format '{{.Id}} {{index .Config.Labels "org.opencontainers.image.version"}}' zerohome-host:v4.5.0-release
systemctl is-active zerocams
systemctl is-enabled zerocams
systemctl is-active zerohome-update.timer zerohome-update.service
```

Record the original systemd enablement for rollback. An absent/inactive updater is expected; these status commands may return nonzero. Check for any other automation outside these units. After approval, the operator runs the following **state-changing cutover**; no migration or DB service operation is included:

```sh
# Run in the same Bash shell as the compose array above.
systemctl stop zerocams
# Must report inactive before continuing; never run both app instances.
systemctl is-active zerocams
# Guard against a remaining HTTP listener before starting Docker.
python3 -I - <<'PY'
import socket
with socket.socket() as s:
    s.bind(('0.0.0.0', 3805))
print('HTTP port available')
PY
# Only after both checks succeed:
"${compose[@]}" up -d --no-deps --pull never --wait --wait-timeout 180 app
```

The `is-active` result `inactive` has a nonzero exit status; inspect it deliberately, not in a blindly pasted `set -e` chain. A healthcheck alone could hit the old process with host networking, hence the explicit stop/port checks. Startup resumes existing workers and their ordinary database writes/retention; “migration-free” does not mean the application is read-only.

Safe-output smoke checks:

```sh
"${compose[@]}" ps
curl --fail --silent --show-error --output /dev/null --write-out 'health HTTP %{http_code}\n' http://127.0.0.1:3805/v1/health
curl --fail --silent --show-error --output /dev/null --write-out 'time HTTP %{http_code}\n' http://127.0.0.1:3805/v1/config/time
"${compose[@]}" exec -T app python -c 'import os; from pathlib import Path; print("release", Path("/usr/local/share/zerohome-version").read_text().strip()); assert os.access("/data/motion_files", os.W_OK); assert Path("/data/models/.EasyOCR/model/craft_mlt_25k.pth").is_file(); assert Path("/data/models/.EasyOCR/model/cyrillic_g2.pth").is_file(); print("storage paths OK")'
```

HTTP health is liveness, **not** DB/camera readiness. Through the existing authenticated UI verify login/data reads (DB), access to an old motion photo, new motion capture, OCR, and WebRTC video **and audio** from the normal external client network. Verify HLS only if enabled. Review logs locally with credentials/addresses redacted before sharing. After successful smoke checks, `systemctl disable zerocams` prevents the old service competing after reboot; Docker's `unless-stopped` policy supplies restart behavior (also confirm Docker is enabled at boot).

If startup or smoke checks fail, first `"${compose[@]}" stop -t 60 app` and verify `"${compose[@]}" ps --status running` shows no app before `systemctl start zerocams`. Restore the recorded original systemd enablement if it was changed. Leave MySQL, `.env`, photos and the model volume untouched; never use `down -v`. This rollback assumes the explicitly verified unchanged-schema compatibility; normal writes made during the Docker trial remain in the same DB.

## Per-task controls

Settings are loaded from the database at process startup. Admins can read global settings with `GET /v1/system-settings` or `GET /v1/system-settings/{key}`, and update an existing setting with `PATCH /v1/system-settings/{key}`. Nodes use `GET /v1/nodes`, `GET /v1/nodes/{node_id}`, and `PATCH /v1/nodes/{node_id}`. These resources support reading and PATCH updates only; they do not expose create or delete operations. Changes require restarting the affected app process. Runtime reads redact all secret-like settings, including deprecated keys. Assignment state is available through read-only `GET /v1/assignments` and `GET /v1/assignments/{function}/{resource_id}`; clients cannot create, update, or delete leases through this API. A worker runs only when its global switch and the local node capability are both enabled. Newly registered nodes start with all implemented capabilities enabled; this does not mean that every worker is assigned a cluster lease. The first administrator is created with the `deploy.bootstrap_admin` operator command described above. The settings map is:

| Setting | Controls | Independent of |
| --- | --- | --- |
| DB global setting | Local node capability | Worker/effect | Cluster ownership |
| --- | --- | --- | --- |
| `SESSION_CLEANUP_ENABLED` | `session_cleanup` | Expired session row cleanup | Runs on each capable node; idempotent |
| `HEALTHCHECK_ENABLED` | `healthcheck` | Periodic camera reachability checks/status notifications | One `healthcheck` lease in cluster mode |
| `SWITCH_SYNC_ENABLED` | `switch_sync` | Switch polling | One `switch_sync` lease in cluster mode |
| `CONNECTION_SYNC_ENABLED` | `connection_sync` | Connection/device synchronization polling | One `connection_sync` lease in cluster mode |
| `TUYA_EVENTS_ENABLED` | `tuya_events` | Real-time Tuya/Pulsar consumers | One `tuya_events` lease in cluster mode |
| `MOTION_DETECTION_ENABLED` | `motion` | Motion detection startup and API start requests | Per-camera leases in cluster mode |
| `MOTION_CLEANUP_ENABLED` | `motion_cleanup` | Expired motion event/photo removal | Runs on each capable node |
| `INGEST_ENABLED` | `ingest` | Persistent RTSP ingest | Per-camera leases in cluster mode |
| `HLS_ENABLED` | `hls` | HLS cleanup and local HLS capability | The viewing node serves HLS |
| `WEBRTC_ENABLED` | `webrtc` | WebRTC decoder cleanup and local WebRTC capability | The viewing node serves signaling and media |
| `NOTIFY_ENABLED` | `notify` | Notification delivery | One `notify` lease in cluster mode |
| `CLUSTER_ENABLED` | — | Enables leases and the fMP4 relay | Global mode switch |
| `STARTUP_WARMUP_ENABLED` / `STARTUP_WARMUP_TIMEOUT_SECONDS` | — | Bounded in-process DB/HTTP warm-up; failure/timeout never blocks startup | Local process |
| `STORAGE_BACKEND`, `S3_ENDPOINT`, `S3_BUCKET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_REGION` | — | Global photo storage integration; secrets are redacted | Shared configuration |

The values in this table live in the database, not in the application `.env`. Each worker requires both its global setting and the node capability to be enabled. Node paths, `NODE_ADDRESS`, cluster listener/proxy options and resource limits live in that node's DB configuration. The database connection plus listener bind (`HOST`/`PORT`) are bootstrap environment values. The identity volume is local persistent state and is never shared between nodes.

### Multi-node mode (experimental)

With `CLUSTER_ENABLED=true` a node no longer starts ingest and motion detection for every camera at startup. Instead it registers itself in the shared database (`nodes` table, refreshed every `NODE_HEARTBEAT_INTERVAL_SECONDS`) and, every `LEASE_RENEW_INTERVAL_SECONDS`, renews or acquires per-camera leases (`assignments` table) for `ingest` and `motion`. Local work for a camera runs only while the node holds its lease. Cameras are split between live nodes whose global setting and matching node capability are enabled; if a node dies, its leases expire after `LEASE_TTL_SECONDS` and another eligible node takes its cameras over. A node that cannot reach the database for `LEASE_TTL_SECONDS - LEASE_RENEW_INTERVAL_SECONDS` stops its per-camera work on its own and resumes after the database is back. Already owned cameras are not rebalanced when a node joins later. With `CLUSTER_ENABLED=false` (default) a node starts only the workers enabled both globally and for that node.

Requirements for every node:

- One shared writable database (all nodes use the same `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME`). Replication is the operator's job; multi-master writes are not supported. One node applies migrations under the MySQL lock; the others wait. See "One node, an external database, or a second node" above.
- Every node gets a stable generated identity in its own persistent identity volume. Do not share that volume between nodes or delete it during a container recreate.
- Each node has its own DB-managed `CACHE_DIR` and `INGEST_REGISTRY_DIR` (never share these between nodes).
- With cluster mode and no explicit `INGEST_REGISTRY_DIR` / `HLS_TEMP_DIR`, the process registries and the HLS temp directory are placed in node-specific subdirectories derived from the persistent node identity, so two nodes on one host never reap each other's ffmpeg or clean each other's HLS files. Explicit DB values are used as given. Still give each node its own volume or `CACHE_DIR`; on different hosts or containers never share one.
- Motion photos for new events are an object key `motion/<cam_id>/<filename>` plus a state (`uploading`, `ready`, `failed`). `STORAGE_BACKEND=local` (default) writes that key under `MOTION_FILES_DIR` by a temp file and `rename` in the same directory. Events saved before this change have no key and are still read and deleted from `<MOTION_FILES_DIR>/<cam_id>/<photo_filename>`. `STORAGE_BACKEND=s3` stores the same keys in a private S3-compatible bucket; `motion_cleanup` deletes them from the bucket. The API still returns `photo_filename` and does not return the object key.
- Notifications are rows in `notification_jobs`, inserted in the same database transaction as the event that caused them. With cluster mode off, a node sends only when its global `NOTIFY_ENABLED` and local `notify` capability are both on. With cluster mode on, only an eligible node that holds the `notify` lease (`resource_id` 0) sends. Delivery to Telegram can be repeated if a claim is lost before the provider answers. `sent` and `failed` jobs older than `NOTIFY_JOB_RETENTION_DAYS` are deleted by that loop.
- A node that does not hold the `ingest` lease pulls that camera's fMP4 from the node that does and feeds its own hub. Snapshots, motion detection, `/stream`, HLS and WebRTC can read that local hub. The owning node's ingest code is unchanged: a remote viewer is an ordinary hub subscriber. The pull runs only while something on the pulling node wants the camera, and for `CLUSTER_MEDIA_UNSUBSCRIBE_GRACE_SECONDS` after the last local viewer leaves. One TCP connection to an owner carries every camera pulled from that owner. A slow peer cannot stall the owner's ffmpeg; its per-camera send buffer is capped at 8 MiB and, past that, the camera is reset to the latest init and keyframe. The fMP4 relay is not HTTP and does not carry playlists or WebRTC signaling.
- Each node authenticates its own viewers and serves `/stream`, HLS playlists and segments, and WebRTC signaling and media from its local hub. The ingest owner feeds that hub locally or over the fMP4 relay. In cluster mode these views require an enabled ingest with a live source. Cloud cameras continue to use their provider's HLS.
- The viewing node applies its own HLS and WebRTC capabilities. Configure `WEBRTC_PUBLIC_IP` and forward `WEBRTC_UDP_PORT_MIN` through `WEBRTC_UDP_PORT_MAX` on every node that serves WebRTC.
- Keep each viewer on the same node for a session, including all HLS playlist and segment requests. If the source or its lease changes, the local session may close or regenerate its playlist. The client can reconnect to the same viewing node.
- **No TLS yet.** Cluster media is plaintext TCP. `CLUSTER_SECRET` authenticates the handshake and does not encrypt the stream. Until TLS wraps that socket, do not run the cluster between hosts across a network you do not trust. With an empty `CLUSTER_SECRET` a node listens and connects only on loopback (`127.0.0.1`, `::1`, `localhost`); a non-loopback peer is rejected and a non-loopback dial raises, so two processes on one machine can be tested without a secret and a misconfigured node will not publish cameras onto the network. Startup fails when `CLUSTER_ENABLED=true`, `CLUSTER_MEDIA_HOST` is not loopback, and `CLUSTER_SECRET` is empty. Setting a secret allows a non-loopback listener, but the bytes are still readable by anyone who can reach the port: keep the listener on loopback until TLS exists, unless the network itself is trusted. `NODE_ADDRESS` is `host`, `host:port` or `[ipv6]:port` (the port defaults to `CLUSTER_MEDIA_PORT`).
- `session_cleanup` and `motion_cleanup` are not leased; each runs only on a node where both its global setting and local capability are enabled.

Global functions under a single right (`resource_id` 0): `healthcheck`, `switch_sync`, `connection_sync` and `tuya_events`. With cluster mode enabled, each node whose global setting and matching local capability are on tries to acquire or renew the right; the lease holder runs the loop. If the right is lost, not renewed, or the database was unreachable for `LEASE_TTL_SECONDS - LEASE_RENEW_INTERVAL_SECONDS`, the node stops that loop; on graceful shutdown the rights are released so another eligible node takes over within one renew interval. There is no fair share for these (one right per function), so they can all end up on the same node.

Not under a right: `session_cleanup` and `motion_cleanup` are idempotent, but start only on nodes where both the corresponding global setting and local capability are enabled. `motion_cleanup` deletes through the configured storage layer. `notify` uses a global lease, managed by its own loop.

Limitations:

- `healthcheck` keeps the last known online/offline state of each camera in memory only. After the right moves to another node, the new owner has no previous state: its first check only records the state, so one online/offline change that happened during the handover can go unreported (and no notification is repeated for the old state).
- Camera create/update/delete on any node makes that node run an early reconcile. A camera owned by another node is picked up by its owner on the owner's next cycle (at most `LEASE_RENEW_INTERVAL_SECONDS`): ingest applies a changed URL by restarting ffmpeg, a motion session is restarted when the camera fields change, and a deleted or disabled camera is stopped and its right released. Stopping HLS/WebRTC/transcoder sessions and ONVIF cache invalidation stay local to the node that handled the request.

`GET /v1/cluster` (admin only) shows the state: local `node_id`, `cluster_enabled`, nodes (address, heartbeat age, alive flag, capabilities, version), current rights (function, resource, owner, generation, seconds until expiry, expired flag) and `unassigned` - wanted per-camera and global functions with no live owner (`reason` is `no_capable_node` when no live node has the function enabled). It never returns camera URLs or credentials. With `CLUSTER_ENABLED=false` it returns `cluster_enabled=false` and empty lists.

When the global `MOTION_DETECTION_ENABLED` setting or this node's `motion` capability is off, motion-start requests return `started=false` without loading YOLO or launching a detector. Camera/push-settings changes cannot override it. User motion preferences remain editable and are retained for a later restart with detection enabled. Existing permissions and per-camera checks still apply when enabled. Orphan-process reaping and shutdown remain available even with detection off.

Existing `INGEST_ENABLED`, `HLS_ENABLED`, and `WEBRTC_ENABLED` switches are reused and retain their defaults (`false`). HLS cleanup follows HLS activation. Rate-limit bucket cleanup is deliberately **always enabled**: disabling memory-maintenance housekeeping would leak state and is not a useful functionality switch. Disabling session or motion cleanup does not invalidate existing data, but it allows expired rows/files to accumulate; provide another retention mechanism if disabling long-term.

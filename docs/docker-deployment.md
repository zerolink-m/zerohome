# Private Docker deployment and background tasks

This is an **opt-in deployment to a Docker host**, not an automatic conversion of an existing systemd installation or database. Nothing here installs a timer, moves data, or starts containers on the current server. Keep the existing production service until a separately planned migration/cutover.

## Image and publication

- Image: `ghcr.io/zerolink-m/zerohome:stable` (private GHCR package owned by the **user** `zerolink-m`).
- `.github/workflows/docker-release.yml` builds `linux/amd64` on Ubuntu 24.04. `workflow_dispatch` and pushes to `release/docker-task-controls` validate builds **without publishing**. GitHub requires a workflow to be on the default branch before it can be manually dispatched; the branch-push trigger permits first-build validation before merging.
- A published GitHub Release builds and pushes `sha-<full-commit-sha>`, verifies package privacy, then promotes that exact digest to the lower-case release tag. `v4.5.0-Release` becomes `v4.5.0-release`; the image's `VERSION` retains the original release string. Only a release not marked prerelease moves `stable`. A prerelease gets its version tag but never `stable`.
- Releases are serialized, not cancelled mid-publication. Publishing an older release later deliberately moves `stable` to that release; publish in intended deployment order. A failed build/privacy check never promotes `stable`.
- Publishing uses only the workflow's `GITHUB_TOKEN` with `contents: read` and `packages: write`; no publishing PAT or deployment-server secrets are required. The OCI source label links the image to `https://github.com/zerolink-m/zerohome`. If an existing package does not grant this repository Actions access, grant it in package settings rather than bypassing the check.

### Privacy is checked, never changed automatically

Before pushing, the workflow reads `GET /users/zerolink-m/packages/container/zerohome`. Existing packages must be `private`; public/internal packages, authorization failures and unexpected responses stop publication. A real 404 allows initial SHA-only publication only after verifying the source repository is private, under [GHCR's documented rule that first-published packages default to private](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry). Immediately after publication, a successful API read reporting `visibility: private` is mandatory before creating release or stable tags. The workflow never requests a visibility change. If readback is temporarily unavailable, it fails closed; investigate and rerun, rather than assuming success.

After the first release, the owner should independently confirm **Private** in the package settings before deploying. Repository privacy alone is not proof of an existing package's privacy. If an administrator later changes visibility, the next workflow refuses to publish; that cannot retroactively undo an external visibility change.

### Build characteristics

`docker build --platform linux/amd64 --build-arg VERSION=v4.5.0-Release -t zerohome:local .`

The deny-by-default build context and explicit COPY list exclude environment files, Git history, venvs, logs, caches, motion photos, tests and local model files. The image uses Python 3.14.4, FFmpeg/ffprobe 8.0.1 (amd64 digest pinned), and exact Python dependency versions. CUDA/NVIDIA/triton entries from the workstation freeze are omitted; torch 2.14.1 and torchvision 0.29.1 come from the CPU-only PyTorch index. Build checks include `pip check`, CPU-only verification and imports of native ML/media libraries. YOLOv8n weights are downloaded from a versioned upstream release and checksum-verified at build time. EasyOCR downloads its models on first use and stores them in the persistent models volume; allow outbound access or preseed that volume before an offline deployment.

This is a CPU image, not a GPU-enabled or ARM build. Native dependencies and ML models still make it substantial. Python/MySQL version tags and Debian security packages are not a bit-for-bit reproducible snapshot; deploy immutable published image digests when exact reproducibility is required. Updating base pins and rebuilding is an explicit maintenance task.

## First deployment on the target host

Requirements: Linux amd64, Docker Engine + modern Compose v2/v5 (`up --wait`), Bash, `flock`, gzip and Python 3. Docker access is root-equivalent. Use a dedicated trusted operator. Put this checkout (including `compose.yaml` and `deploy/`) at `/opt/zerohome`; if you use another location, adjust the service unit accordingly.

1. Authenticate **as the same OS user that runs the updater**. Use an account with package access and a GitHub PAT (classic) with `read:packages`, SSO-authorized if applicable. Do not place the token in this repository or a command-line argument:

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

   `/etc/zerohome/app.conf` supplies application settings (choose appropriate TTLs and preserve existing signing secrets when migrating an installation):

   ```ini
   JWT_SECRET_SESSION=REPLACE_WITH_RANDOM_SESSION_SIGNING_SECRET
   JWT_SECRET_SHARE=REPLACE_WITH_DIFFERENT_RANDOM_SHARE_SIGNING_SECRET
   SHARE_TOKEN_MIN_TTL_SECONDS=60
   SHARE_TOKEN_MAX_TTL_SECONDS=86400
   MOTION_DETECTION_ENABLED=true
   MOTION_CLEANUP_ENABLED=true
   ```

   These examples are documentation, not generated environment files. Compose sets the internal database host/password and port explicitly; MySQL root credentials are not injected into the app. Use Compose-compatible quoting if values contain special characters. The container entrypoint always restores `VERSION` from a build-time version file, so even a stale `VERSION` copied from an existing host configuration cannot override the running image's release version.

3. Explicitly pull MySQL for first installation, then use the migration-aware deploy script:

   ```sh
   cd /opt/zerohome
   docker compose --env-file /etc/zerohome/deployment.conf pull db
   sudo ./deploy/update.sh
   docker compose --env-file /etc/zerohome/deployment.conf ps
   curl --fail http://127.0.0.1:8000/v1/health
   ```

   Run all Docker commands under the authenticated operator (omit `sudo` if already root). The updater pulls the application, pins its digest, starts/waits for the existing or freshly initialized DB without upgrading it, stops the app, backs up the database, runs `alembic upgrade head` in a single disposable `migrate` container, and only then starts/waits for the app. Initial deployment also takes a backup of the newly initialized empty database. The app image itself never runs migrations at startup. **Use the script, not a bare `docker compose up -d app`, to deploy or update.** The maintenance-profile migrate service is not a continuously running replica.

One app process/replica is supported: camera ownership and background jobs are in-process, not a distributed worker system. The container runs as UID/GID 10001, drops capabilities and uses an init process for child reaping. Named volumes persist MySQL data, motion event photos and OCR/model state. Cache/process registries and temporary media are disposable tmpfs. Never run `docker compose down -v` on a live installation. A healthcheck is HTTP **liveness**, not proof of camera reachability or database correctness; DB health and migration success are checked separately by deployment.

The HTTP port is loopback-only by default; terminate HTTPS in a reverse proxy with WebSocket support and appropriate streaming timeouts. MySQL has no published host port. Camera RTSP/ONVIF addresses must be reachable from the Docker bridge network. WebRTC needs explicit UDP routing: choose `WEBRTC_UDP_PORT_MIN/MAX`, set `WEBRTC_PUBLIC_IP` and publish/forward the same UDP range in a local Compose override; HTTP proxying alone is insufficient. Supply that override explicitly in the updater's Compose command if used (the shipped script intentionally reads only `compose.yaml`). Review firewall/NAT and STUN/TURN client settings for the target host; do not assume the existing host configuration automatically transfers into bridge networking.

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

## Per-task controls

Settings are read at process startup. Edit the host's application configuration and restart/redeploy to change them; there is no unauthenticated runtime administration endpoint. New flags default to `true`, preserving existing behavior:

| Setting | Controls | Independent of |
| --- | --- | --- |
| `SESSION_CLEANUP_ENABLED` | Expired session row cleanup | Authentication and expiry checks |
| `HEALTHCHECK_ENABLED` | Periodic camera reachability checks/status notifications | HTTP container liveness check |
| `SWITCH_SYNC_ENABLED` | Switch polling | Tuya event consumer |
| `CONNECTION_SYNC_ENABLED` | Connection/device synchronization polling | Switch polling and Tuya event consumer |
| `TUYA_EVENTS_ENABLED` | Real-time Tuya/Pulsar consumers | Polling workers |
| `MOTION_DETECTION_ENABLED` | Global motion detection gate: startup and every runtime start | Event/photo cleanup and stored user preferences |
| `MOTION_CLEANUP_ENABLED` | Expired motion event/photo removal | Detection itself |

`MOTION_DETECTION_ENABLED=false` returns `started=false` from otherwise-authorized motion-start requests without querying the detector's database, loading YOLO or launching a detector. Camera/push-settings changes cannot override it. User motion preferences remain editable and are retained for a later restart with detection enabled. Existing permissions and per-camera checks still apply when enabled. Orphan-process reaping and shutdown remain available even with detection off.

Existing `INGEST_ENABLED`, `HLS_ENABLED`, and `WEBRTC_ENABLED` switches are reused and retain their defaults (`false`). HLS cleanup follows HLS activation. Rate-limit bucket cleanup is deliberately **always enabled**: disabling memory-maintenance housekeeping would leak state and is not a useful functionality switch. Disabling session or motion cleanup does not invalidate existing data, but it allows expired rows/files to accumulate; provide another retention mechanism if disabling long-term.

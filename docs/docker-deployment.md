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

## Existing `/opt/zerocams` host: systemd to Docker, host MySQL retained

`compose.host.yaml` is a **standalone, host-specific alternative**, not an override to merge with `compose.yaml`. It has only `app`: no database container, migration service, automatic schema changes or image pulls. **The shipped `deploy/update.sh` and `zerohome-update.timer` are NOT compatible with this deployment**: they assume Compose-managed MySQL and run migrations. Do not install/enable them; ensure no previously installed updater is active before cutover. Watchtower is explicitly excluded.

Inspected host settings: systemd runs `/opt/zerocams/main.py` as root, with working directory `/opt/zerocams` and `KillMode=control-group`; HTTP binds `0.0.0.0:3805`, MySQL is `127.0.0.1:3306`, WebRTC uses UDP `49152–65535`. Host networking retains these routes and the existing firewall/NAT rules; Docker port publishing is deliberately absent. It also exposes the container to host-network services: this is not bridge-network isolation.

The unchanged `/opt/zerocams/.env` is supplied via `env_file`; credentials, signing keys, worker flags and WebRTC settings remain there. Only container storage paths and the inspected HTTP bind are overridden. The image entrypoint restores the release `VERSION` from `/usr/local/share/zerohome-version`, ignoring a stale value in `.env`. If the host bind/port is deliberately changed later, update both Compose environment and healthcheck.

### Storage and privilege choices

- `/opt/zerocams/motion_files` is bind-mounted read/write at `/data/motion_files`. Existing photos and ownership remain unchanged; normal application retention cleanup still applies after startup.
- `/opt/zerocams/caching` is **not mounted or copied**. Snapshots are regenerable; all ingest/transcode/HLS/WebRTC/snapshot/motion PID registries go into private `/data/cache` tmpfs. HLS segments use private `/tmp/zerocams/hls` tmpfs. Never share the host PID namespace or copy its process registries. These caches disappear on container stop.
- Existing `/root/.EasyOCR` is narrowly mounted **read-only** at `/data/models/.EasyOCR`; no other `/root` files are exposed. The inspected cache contains `model/craft_mlt_25k.pth`, `model/cyrillic_g2.pth` and `user_network/`, matching the app's English/Russian EasyOCR reader. A missing/corrupt/incompatible model cannot be downloaded over this read-only mount: test OCR after cutover. If replacement models are needed, prepare a separate container-owned copy and change only this bind source, rather than modifying production cache ownership.
- The fresh `zerohome-host_models` volume holds Ultralytics settings and other model-home caches. `nocopy` avoids inheriting UID 10001 ownership from the image. Existing `/root/.config/Ultralytics` is not required/copied; it is library configuration, not the app's detection model. YOLO loads `/app/yolov8n.pt`, the image's pinned/checksummed weights, not the host's untracked `yolov8n.pt`.
- `frontend_versions.json` is read-only application input shipped in the image. Logging is stdout with bounded Docker log rotation; the host's existing `app.log` is not mounted. Camera recordings remain on the cameras; no additional local recording archive path was found.
- The host-only config runs as `0:0`, matching systemd and allowing writes to root-owned motion data **without chmod/chown**. It drops all capabilities and uses `no-new-privileges` and a private PID namespace, but root plus host networking is weaker isolation than the default non-root deployment. Do not add Docker-socket or host-root mounts. Run only one app instance.

### Operator-only preparation and cutover (not automatic)

First verify a current recoverable host-MySQL backup plus photos/config backup, and that the chosen release is compatible with the existing schema **without migration**. If not, stop here and plan a separate schema change. Build the reviewed final release commit into `zerohome-host:v4.5.0-release`, with `--build-arg VERSION=v4.5.0-Release`, from an isolated checkout of that immutable commit. Do not merely retag `zerohome-release-check:v4.5.0-release`: it may predate the final fixes. Record the source commit and resulting image ID; do not overwrite the tag during cutover.

Use the explicit file and an empty Compose interpolation env file every time. The service's `env_file` still loads `/opt/zerocams/.env`. Do not print `docker compose config`, container environment, raw logs, SQL rows or camera URLs; use quiet validation or narrow output instead. Do not source `.env` in a shell. Before stopping systemd, compare resolved environment values to the existing Python dotenv parser in-memory without printing values (especially if secrets contain `$` or quoting).

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
| `STARTUP_WARMUP_ENABLED` / `STARTUP_WARMUP_TIMEOUT_SECONDS` (default `10.0`) | Bounded in-process DB/HTTP warm-up at startup (no tokens, no writes; failure or timeout never blocks startup) | All workers |
| `CLUSTER_ENABLED` (default `false`) | Multi-node mode switch: per-camera ingest and motion detection and the global workers (health check, switch sync, connection sync, Tuya events) are owned through leases (see below); session and motion cleanup are unchanged | Other workers |
| `NODE_ID` (default empty) | Node identifier; required (non-blank) when `CLUSTER_ENABLED=true`, startup fails otherwise. With `CLUSTER_ENABLED=false` empty means hostname plus a random suffix generated once per process | Leases |
| `NODE_ADDRESS` (default empty) | Where other nodes dial this one for media: `host`, `host:port` or `[ipv6]:port`. No port means `CLUSTER_MEDIA_PORT`. Not an HTTP URL | Leases, media pull |
| `LEASE_TTL_SECONDS` (default `30`) / `LEASE_RENEW_INTERVAL_SECONDS` (default `10`) | Lease lifetime and renewal period; startup fails unless renew interval x 2 <= TTL | Workers |
| `NODE_HEARTBEAT_INTERVAL_SECONDS` (default `10`) | Node heartbeat period | Leases |
| `CLUSTER_MEDIA_HOST` (default `127.0.0.1`) / `CLUSTER_MEDIA_PORT` (default `4700`) | Where this node accepts fMP4 from other nodes. One TCP connection carries many cameras | `CLUSTER_ENABLED=false` (nothing listens) |
| `CLUSTER_SECRET` (default empty) | Shared HMAC for the media handshake. Empty: accept and dial loopback only. It does not encrypt | TLS (not implemented; the socket is plaintext) |
| `CLUSTER_MEDIA_UNSUBSCRIBE_GRACE_SECONDS` (default `10`) | How long a pulled camera stays subscribed after the last local viewer leaves | The ingest lease |
| `NOTIFY_ENABLED` (default `true`) | Push delivery. Off: this node never sends. On, with `CLUSTER_ENABLED=false`: the single process sends. On, with `CLUSTER_ENABLED=true`: only the node holding the `notify` lease sends | Other workers |
| `NOTIFY_JOB_RETENTION_DAYS` (default `7`) | How long `sent` and `failed` notification jobs are kept | Delivery itself |
| `STORAGE_BACKEND` (default `local`) | Where new motion photos are stored: `local` directory or an `s3` bucket. `s3` does not fall back to local | Reading old photos that have no object key |
| `S3_ENDPOINT`, `S3_BUCKET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` | Required together when `STORAGE_BACKEND=s3`. Startup fails if any of them is empty. Path-style requests to the endpoint. Access keys are never logged | `STORAGE_BACKEND=local` |
| `S3_REGION` (default empty) | Region used to sign S3 requests. Empty signs as `us-east-1` | The bucket name |

### Multi-node mode (experimental)

With `CLUSTER_ENABLED=true` a node no longer starts ingest and motion detection for every camera at startup. Instead it registers itself in the shared database (`nodes` table, refreshed every `NODE_HEARTBEAT_INTERVAL_SECONDS`) and, every `LEASE_RENEW_INTERVAL_SECONDS`, renews or acquires per-camera leases (`assignments` table) for `ingest` and `motion`. Local work for a camera runs only while the node holds its lease. Cameras are split evenly between live nodes that have the function enabled (`INGEST_ENABLED`, `MOTION_DETECTION_ENABLED`); if a node dies, its leases expire after `LEASE_TTL_SECONDS` and another node takes its cameras over. A node that cannot reach the database for `LEASE_TTL_SECONDS - LEASE_RENEW_INTERVAL_SECONDS` stops its per-camera work on its own and resumes after the database is back. Already owned cameras are not rebalanced when a node joins later. With `CLUSTER_ENABLED=false` (default) behavior is unchanged.

Requirements for every node:

- One shared database (all nodes point at the same `DATABASE_URL`/MySQL); migrations must be applied before nodes start.
- A unique `NODE_ID` per node, and its own `CACHE_DIR` and `INGEST_REGISTRY_DIR` (never share these between nodes).
- `NODE_ID` is required on every node when `CLUSTER_ENABLED=true`: the settings validation fails at startup if it is empty or blank. A random id would change on every start, so the process registry of the previous run (kept under a directory named after the node id) is not found and ffmpeg processes orphaned by a crash are not reaped.
- With `CLUSTER_ENABLED=true` and no explicit `INGEST_REGISTRY_DIR` / `HLS_TEMP_DIR`, the process registries and the HLS temp directory are placed in `<CACHE_DIR>/ingest/nodes/<NODE_ID>/` and `<HLS_TEMP_DIR>/nodes/<NODE_ID>/` (NODE_ID reduced to `[A-Za-z0-9._-]`), so two nodes on one host never reap each other's ffmpeg or clean each other's HLS files. Explicit values are used as given. Still give each node its own volume or `CACHE_DIR`; on different hosts or containers never share one.
- Motion photos for new events are an object key `motion/<cam_id>/<filename>` plus a state (`uploading`, `ready`, `failed`). `STORAGE_BACKEND=local` (default) writes that key under `MOTION_FILES_DIR` by a temp file and `rename` in the same directory. Events saved before this change have no key and are still read and deleted from `<MOTION_FILES_DIR>/<cam_id>/<photo_filename>`. `STORAGE_BACKEND=s3` stores the same keys in a private S3-compatible bucket; `motion_cleanup` deletes them from the bucket. The API still returns `photo_filename` and does not return the object key.
- Notifications are rows in `notification_jobs`, inserted in the same database transaction as the event that caused them. With `CLUSTER_ENABLED=false` the one process sends them. With `CLUSTER_ENABLED=true` only the node that holds the `notify` lease (`resource_id` 0) sends. That loop takes the lease itself; it is not one of the ClusterRunner global functions, but it is the same kind of right, so two nodes do not send at once. A node with `NOTIFY_ENABLED=false` never takes it. Delivery to Telegram can be repeated if a claim is lost before the provider answers. `sent` and `failed` jobs older than `NOTIFY_JOB_RETENTION_DAYS` are deleted by that loop.
- A node that does not hold the `ingest` lease pulls that camera's fMP4 from the node that does and feeds its own hub. HLS, WebRTC, snapshots, motion detection and `/stream` on the pulling node keep reading that local hub; HTTP requests are still not routed to the owner (that is a later step). The owning node's ingest code is unchanged: a remote viewer is an ordinary hub subscriber. The pull runs only while something on the pulling node wants the camera, and for `CLUSTER_MEDIA_UNSUBSCRIBE_GRACE_SECONDS` after the last local viewer leaves. One TCP connection to an owner carries every camera pulled from that owner. A slow peer cannot stall the owner's ffmpeg; its per-camera send buffer is capped at 8 MiB and, past that, the camera is reset to the latest init and keyframe.
- **No TLS yet.** Cluster media is plaintext TCP. `CLUSTER_SECRET` authenticates the handshake and does not encrypt the stream. Until TLS wraps that socket, do not run the cluster between hosts across a network you do not trust. With an empty `CLUSTER_SECRET` a node listens and connects only on loopback (`127.0.0.1`, `::1`, `localhost`); a non-loopback peer is rejected and a non-loopback dial raises, so two processes on one machine can be tested without a secret and a misconfigured node will not publish cameras onto the network. Startup fails when `CLUSTER_ENABLED=true`, `CLUSTER_MEDIA_HOST` is not loopback, and `CLUSTER_SECRET` is empty. Setting a secret allows a non-loopback listener, but the bytes are still readable by anyone who can reach the port: keep the listener on loopback until TLS exists, unless the network itself is trusted. `NODE_ADDRESS` is `host`, `host:port` or `[ipv6]:port` (the port defaults to `CLUSTER_MEDIA_PORT`).
- Not yet covered: an HTTP view (HLS/WebRTC) that lands on a node with no local consumer of its own is not redirected to the owner. `session_cleanup` and `motion_cleanup` still run on every node (see below).

Global functions under a single right (`resource_id` 0): `healthcheck`, `switch_sync`, `connection_sync` and `tuya_events`. With `CLUSTER_ENABLED=true` the application does not start these four at boot. Each reconcile cycle, a node whose flag is on (`HEALTHCHECK_ENABLED`, `SWITCH_SYNC_ENABLED`, `CONNECTION_SYNC_ENABLED`, `TUYA_EVENTS_ENABLED`) tries to acquire or renew the right; whoever gets it first runs the loop, a node with the flag off never takes it. If the right is lost, not renewed, or the database was unreachable for `LEASE_TTL_SECONDS - LEASE_RENEW_INTERVAL_SECONDS`, the node stops that loop; on a graceful shutdown the rights are released so another node takes over within one renew interval. There is no fair share for these (one right per function), so they can all end up on the same node.

Not under a right (still run on every node whose flag is on): `session_cleanup` is idempotent, so concurrent runs are harmless. `motion_cleanup` deletes through the storage layer (the local directory, or the bucket when `STORAGE_BACKEND=s3`) and is idempotent, so it also stays per node. `notify` is a right, but its loop is separate from the four above.

Limitations:

- `healthcheck` keeps the last known online/offline state of each camera in memory only. After the right moves to another node, the new owner has no previous state: its first check only records the state, so one online/offline change that happened during the handover can go unreported (and no notification is repeated for the old state).
- Camera create/update/delete on any node makes that node run an early reconcile. A camera owned by another node is picked up by its owner on the owner's next cycle (at most `LEASE_RENEW_INTERVAL_SECONDS`): ingest applies a changed URL by restarting ffmpeg, a motion session is restarted when the camera fields change, and a deleted or disabled camera is stopped and its right released. Stopping HLS/WebRTC/transcoder sessions and ONVIF cache invalidation stay local to the node that handled the request.

`GET /v1/cluster` (admin only) shows the state: local `node_id`, `cluster_enabled`, nodes (address, heartbeat age, alive flag, capabilities, version), current rights (function, resource, owner, generation, seconds until expiry, expired flag) and `unassigned` - wanted per-camera and global functions with no live owner (`reason` is `no_capable_node` when no live node has the function enabled). It never returns camera URLs or credentials. With `CLUSTER_ENABLED=false` it returns `cluster_enabled=false` and empty lists.

`MOTION_DETECTION_ENABLED=false` returns `started=false` from otherwise-authorized motion-start requests without querying the detector's database, loading YOLO or launching a detector. Camera/push-settings changes cannot override it. User motion preferences remain editable and are retained for a later restart with detection enabled. Existing permissions and per-camera checks still apply when enabled. Orphan-process reaping and shutdown remain available even with detection off.

Existing `INGEST_ENABLED`, `HLS_ENABLED`, and `WEBRTC_ENABLED` switches are reused and retain their defaults (`false`). HLS cleanup follows HLS activation. Rate-limit bucket cleanup is deliberately **always enabled**: disabling memory-maintenance housekeeping would leak state and is not a useful functionality switch. Disabling session or motion cleanup does not invalidate existing data, but it allows expired rows/files to accumulate; provide another retention mechanism if disabling long-term.

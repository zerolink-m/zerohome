from __future__ import annotations

from pathlib import Path
import os
from urllib.parse import quote_plus, urlsplit

from pydantic import AliasChoices, Field, PrivateAttr, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    """Runtime configuration defaults plus DB connection bootstrap values.

    Runtime fields provide first-start defaults and are then hydrated from the
    database. Environment input is limited to DB connectivity, initial image
    storage paths, and explicitly prefixed one-time secret imports.
    """

    # Runtime fields are hydrated from the database during application startup.
    model_config = SettingsConfigDict(env_file=None, env_prefix="BOOTSTRAP_", extra="ignore")

    _node_capabilities: dict[str, bool] = PrivateAttr(default_factory=dict)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Allow DB values and seed paths only; never load dotenv files."""
        db_fields = {
            "DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME",
            "DB_READ_TIMEOUT_SECONDS", "DB_WRITE_TIMEOUT_SECONDS",
        }
        initial_node_defaults = {"CACHE_DIR", "MOTION_FILES_DIR"}

        def bootstrap_environment() -> dict:
            # Explicit process-only import path. Dotenv sources are deliberately
            # excluded, so legacy secrets are never read from a mounted .env.
            values = {}
            for key in db_fields | initial_node_defaults | {"HOST", "PORT"}:
                value = os.environ.get(f"BOOTSTRAP_{key}", os.environ.get(key))
                if value is not None:
                    values[key] = value
            for key in ("JWT_SECRET_SESSION", "JWT_SECRET_SHARE"):
                value = os.environ.get(f"BOOTSTRAP_{key}")
                if value is not None:
                    values[key] = value
            return values

        return init_settings, bootstrap_environment

    # --- database ---
    DB_HOST: str = Field(default="127.0.0.1", validation_alias=AliasChoices("DB_HOST", "BOOTSTRAP_DB_HOST"))
    DB_PORT: int = Field(default=3306, validation_alias=AliasChoices("DB_PORT", "BOOTSTRAP_DB_PORT"))
    DB_USER: str = Field(default="zerocams", validation_alias=AliasChoices("DB_USER", "BOOTSTRAP_DB_USER"))
    DB_PASSWORD: str = Field(default="", repr=False, validation_alias=AliasChoices("DB_PASSWORD", "BOOTSTRAP_DB_PASSWORD"))
    DB_NAME: str = Field(default="zerocams", validation_alias=AliasChoices("DB_NAME", "BOOTSTRAP_DB_NAME"))
    # Seconds a pooled connection may wait on a silent MySQL. asyncmy.connect
    # accepts read_timeout (queries and pool pre-ping share that read). It does
    # not accept write_timeout; that value is forwarded only if the installed
    # driver grows the argument. See app/db/session.py.
    DB_READ_TIMEOUT_SECONDS: int = Field(
        default=30, gt=0, validation_alias=AliasChoices("DB_READ_TIMEOUT_SECONDS", "BOOTSTRAP_DB_READ_TIMEOUT_SECONDS")
    )
    DB_WRITE_TIMEOUT_SECONDS: int = Field(
        default=30, gt=0, validation_alias=AliasChoices("DB_WRITE_TIMEOUT_SECONDS", "BOOTSTRAP_DB_WRITE_TIMEOUT_SECONDS")
    )

    # --- JWT: share links and previously issued refresh tokens only ---
    JWT_SECRET_SESSION: str = Field(default="", repr=False)
    JWT_SECRET_SHARE: str = Field(default="", repr=False)
    JWT_ALGORITHM: str = "HS256"
    # Deprecated configuration, retained to accept existing deployment env files.
    JWT_SECRET_ACCESS: str = ""
    JWT_SECRET_ACCESS_ADMIN: str = ""

    # --- legacy refresh signing (expiry itself is in the signed JWT) ---
    SESSION_TOKEN_TTL_SECONDS: int = 86400
    SHARE_TOKEN_MIN_TTL_SECONDS: int = 60
    SHARE_TOKEN_MAX_TTL_SECONDS: int = 3600

    # --- push (Telegram) ---
    TELEGRAM_API_BASE: str = "https://api.telegram.org"

    # --- background workers (read at process startup; restart after changing) ---
    SESSION_CLEANUP_ENABLED: bool = True
    HEALTHCHECK_ENABLED: bool = True
    SWITCH_SYNC_ENABLED: bool = True
    CONNECTION_SYNC_ENABLED: bool = True
    TUYA_EVENTS_ENABLED: bool = True
    MOTION_DETECTION_ENABLED: bool = True
    MOTION_CLEANUP_ENABLED: bool = True
    STARTUP_WARMUP_ENABLED: bool = True  # in-process DB/HTTP warm-up before serving; never fails startup
    STARTUP_WARMUP_TIMEOUT_SECONDS: float = 10.0  # hard wall-clock bound for the whole warm-up
    HEALTHCHECK_INTERVAL_SECONDS: int = 60
    HEALTHCHECK_TCP_TIMEOUT_SECONDS: float = 5.0
    STREAM_IDLE_TIMEOUT_SECONDS: int = 30
    ONVIF_TIMEOUT_SECONDS: float = 10.0
    ONVIF_AUTH_BACKOFF_SECONDS: int = 1800
    SWITCH_TIMEOUT_SECONDS: float = 10.0
    SWITCH_SYNC_INTERVAL_SECONDS: int = 300
    CONNECTION_SYNC_INTERVAL_SECONDS: int = 420

    # --- external binaries ---
    FFMPEG_BINARY: str = "ffmpeg"
    FFPROBE_BINARY: str = "ffprobe"

    # --- HLS streaming ---
    HLS_ENABLED: bool = False
    HLS_TEMP_DIR: str = "/tmp/zerocams/hls"
    HLS_SEGMENT_DURATION: int = 2
    HLS_PLAYLIST_SIZE: int = 10
    HLS_CLEANUP_INTERVAL: int = 60
    HLS_SEGMENT_MAX_AGE: int = 300
    HLS_IDLE_TIMEOUT_SECONDS: int = 30
    HLS_START_TIMEOUT_SECONDS: float = 10.0  # ffmpeg must list a first segment within this, else fallback/error
    HLS_STALL_SECONDS: float = 15.0  # no new segment this long -> kill ffmpeg (next request restarts)

    # --- WebRTC streaming ---
    WEBRTC_ENABLED: bool = False
    WEBRTC_STUN_SERVER: str = ""
    WEBRTC_TURN_SERVER: str = ""
    WEBRTC_TURN_SECRET: str = ""
    WEBRTC_ICE_TIMEOUT_SECONDS: int = 10
    WEBRTC_MAX_BITRATE: int = 2000000
    WEBRTC_PUBLIC_IP: str = ""  # advertised as a public candidate (1:1 forwarded UDP ports); needs WEBRTC_UDP_PORT_MIN/MAX
    WEBRTC_UDP_PORT_MIN: int = 0  # local UDP port range for ICE host sockets (0 = OS ephemeral range)
    WEBRTC_UDP_PORT_MAX: int = 0
    WEBRTC_TURNS_SERVER: str = ""  # optional turns:host:5349 for browsers (TLS/TCP fallback)
    WEBRTC_TURN_CREDENTIAL_TTL_SECONDS: int = 3600  # lifetime of temporary TURN credentials
    WEBRTC_HIDE_PRIVATE_CANDIDATES: bool = True  # strip private/loopback/link-local addresses from SDP sent to clients
    WEBRTC_FIRST_FRAME_TIMEOUT_SECONDS: float = 15.0  # no first decoded frame this long -> session ends
    WEBRTC_STALL_SECONDS: float = 10.0  # decoder silent this long mid-stream -> session ends
    WEBRTC_AUDIO_ENABLED: bool = True  # send the camera's audio (Opus, mono 48 kHz) when the stream has any
    WEBRTC_MAX_LATENCY_MS: int = 500  # frames queued beyond this are dropped (live edge wins)

    # --- multi-node: node identity and leases (stage 1; nothing consumes these yet) ---
    CLUSTER_ENABLED: bool = False
    NODE_ADDRESS: str = ""
    LEASE_TTL_SECONDS: int = 30
    LEASE_RENEW_INTERVAL_SECONDS: int = 10
    NODE_HEARTBEAT_INTERVAL_SECONDS: int = 10
    # Plain TCP fMP4 between nodes (stage 3). TLS is not enabled: an empty secret
    # listens and dials on loopback only. See app/services/cluster/media_transport.py.
    CLUSTER_MEDIA_HOST: str = "127.0.0.1"
    CLUSTER_MEDIA_PORT: int = 4700
    CLUSTER_SECRET: str = Field(default="", repr=False)  # media handshake HMAC; does not encrypt
    CLUSTER_MEDIA_UNSUBSCRIBE_GRACE_SECONDS: float = 10.0
    # Close a media connection when nothing (including PING/PONG) arrives for this long.
    CLUSTER_MEDIA_IDLE_TIMEOUT_SECONDS: float = 20.0

    # --- ingest: one persistent ffmpeg (RTSP -> fMP4, -c copy) per enabled camera ---
    # /stream, HLS, WebRTC, live snapshots and motion detection read this hub when it is on.
    INGEST_ENABLED: bool = False
    INGEST_STALL_SECONDS: float = 10.0  # no bytes this long -> kill and restart
    INGEST_RESTART_MIN_MINUTES: float = 30.0  # planned restart window (random per camera)
    INGEST_RESTART_MAX_MINUTES: float = 60.0  # ...done only while nobody is watching
    INGEST_MAX_BUFFER_BYTES: int = 16 * 1024 * 1024  # last-GOP buffer cap per camera
    INGEST_BACKOFF_MAX_SECONDS: float = 30.0  # restart pause grows 1,2,4... up to this
    INGEST_STOP_TIMEOUT_SECONDS: float = 8.0  # overall budget to stop all ingest at shutdown
    INGEST_REGISTRY_DIR: str = ""  # empty -> CACHE_DIR/ingest
    # stage 2: /stream reads the hub through an on-demand transcoder (libx264)
    INGEST_IDLE_TRANSCODE_LINGER_SECONDS: float = 45.0  # transcoder lives this long after the last viewer
    INGEST_HUB_WAIT_SECONDS: float = 5.0  # wait for an online hub before falling back to direct RTSP
    INGEST_TRANSCODE_STALL_SECONDS: float = 15.0  # transcoder output silent this long -> kill it
    # stage 5: live snapshots and motion detection read the hub (needs INGEST_ENABLED)
    INGEST_SNAPSHOT_HUB_WAIT_SECONDS: float = 2.0  # wait for a fed hub with a stored GOP before falling back to RTSP
    INGEST_SNAPSHOT_TIMEOUT_SECONDS: float = 10.0  # local snapshot ffmpeg must finish within this
    INGEST_SNAPSHOT_MAX_PARALLEL: int = 4  # concurrent local snapshot ffmpegs (all cameras together)
    INGEST_MOTION_FIRST_FRAME_TIMEOUT_SECONDS: float = 20.0  # no first decoded frame this long -> fall back to direct RTSP
    INGEST_MOTION_STALL_SECONDS: float = 15.0  # decoder silent this long mid-stream -> restart the detection source
    INGEST_MOTION_MAX_FRAME_AGE_SECONDS: float = 10.0  # decoded frames older than this are never analysed/notified
    INGEST_MOTION_RESTART_ATTEMPTS: int = 5  # consecutive source failures (no frame) before the session gives up

    # --- misc ---
    CACHE_DIR: str = "caching"
    # Motion event photos; a relative path is resolved against the project root.
    MOTION_FILES_DIR: str = "motion_files"
    # local (default) writes under MOTION_FILES_DIR. s3 never falls back to local.
    STORAGE_BACKEND: str = "local"
    S3_ENDPOINT: str = ""
    S3_BUCKET: str = ""
    S3_ACCESS_KEY: str = Field(default="", repr=False)
    S3_SECRET_KEY: str = Field(default="", repr=False)
    S3_REGION: str = ""  # blank is signed as us-east-1
    NOTIFY_ENABLED: bool = True  # capability "notify"; off = this node does not send pushes
    NOTIFY_JOB_RETENTION_DAYS: int = 7
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # --- logging ---
    LOGGING_LEVEL: str = "INFO"  # DEBUG, INFO, WARNING, ERROR, CRITICAL
    LOGGING_SCOPE: str = "general"  # general, motion_detection
    # One INFO line per motion-detection inference (1/s per camera): every YOLO detection before
    # filtering and why it was dropped / whether an event was created. Off by default.
    MOTION_DEBUG_LOG: bool = False
    MOTION_COOLDOWN_SECONDS: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _check_lease_timing(self) -> "Settings":
        if self.CLUSTER_MEDIA_IDLE_TIMEOUT_SECONDS <= 0:
            raise ValueError("CLUSTER_MEDIA_IDLE_TIMEOUT_SECONDS must be > 0")
        if self.LEASE_RENEW_INTERVAL_SECONDS < 1 or self.NODE_HEARTBEAT_INTERVAL_SECONDS < 1:
            raise ValueError("LEASE_RENEW_INTERVAL_SECONDS and NODE_HEARTBEAT_INTERVAL_SECONDS must be >= 1")
        if self.LEASE_RENEW_INTERVAL_SECONDS * 2 > self.LEASE_TTL_SECONDS:
            raise ValueError("LEASE_RENEW_INTERVAL_SECONDS * 2 must be <= LEASE_TTL_SECONDS")
        if self.CLUSTER_ENABLED:
            media_host = (self.CLUSTER_MEDIA_HOST or "").strip()
            if not media_host:
                raise ValueError("CLUSTER_MEDIA_HOST must be set when CLUSTER_ENABLED=true")
            if self.CLUSTER_MEDIA_PORT < 1 or self.CLUSTER_MEDIA_PORT > 65535:
                raise ValueError("CLUSTER_MEDIA_PORT must be between 1 and 65535")
            if self.CLUSTER_MEDIA_UNSUBSCRIBE_GRACE_SECONDS < 0:
                raise ValueError("CLUSTER_MEDIA_UNSUBSCRIBE_GRACE_SECONDS must be >= 0")
            from app.services.cluster.media_protocol import is_loopback_host
            if not is_loopback_host(media_host) and not (self.CLUSTER_SECRET or "").strip():
                raise ValueError(
                    "CLUSTER_SECRET must be set when CLUSTER_ENABLED=true and CLUSTER_MEDIA_HOST "
                    f"({media_host}) is not loopback. TLS is not enabled, so the media stream would be "
                    "sent in the clear; refusing to listen. Bind 127.0.0.1 or ::1 without a secret, "
                    "or set CLUSTER_SECRET before using any other address."
                )
        backend = (self.STORAGE_BACKEND or "").strip().lower()
        if backend not in ("local", "s3"):
            raise ValueError("STORAGE_BACKEND must be 'local' or 's3'")
        self.STORAGE_BACKEND = backend
        if self.NOTIFY_JOB_RETENTION_DAYS < 0:
            raise ValueError("NOTIFY_JOB_RETENTION_DAYS must be >= 0")
        if backend == "s3":
            missing = [
                name for name in ("S3_ENDPOINT", "S3_BUCKET", "S3_ACCESS_KEY", "S3_SECRET_KEY")
                if not str(getattr(self, name) or "").strip()
            ]
            if missing:
                raise ValueError(
                    "STORAGE_BACKEND=s3 requires non-empty settings: " + ", ".join(missing)
                    + ". Refusing to start; not falling back to local storage."
                )
            parts = urlsplit(self.S3_ENDPOINT.strip())
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ValueError(
                    "S3_ENDPOINT must be an absolute http(s) URL, for example http://minio:9000"
                )
            if parts.query or parts.fragment or parts.username is not None or parts.password is not None:
                raise ValueError("S3_ENDPOINT must not include credentials, a query, or a fragment")
        return self

    @property
    def database_url(self) -> str:
        """Single source of the async MySQL URL (asyncmy driver)."""
        return (
            f"mysql+asyncmy://{quote_plus(self.DB_USER)}:{quote_plus(self.DB_PASSWORD)}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}?charset=utf8mb4"
        )

    def _node_scoped(self, base: Path) -> Path:
        """<base>/nodes/<safe node_id> in cluster mode, base unchanged otherwise."""
        if not self.CLUSTER_ENABLED:
            return base
        from app.services.cluster.identity import get_node_id, safe_node_dir_name

        return base / "nodes" / safe_node_dir_name(get_node_id())

    @property
    def ingest_registry_path(self) -> str:
        """Absolute path of the ingest process registry file.

        Every pool (ingest, transcode, webrtc, hls, snapshot, motion) derives its registry
        file from this directory. With CLUSTER_ENABLED and no explicit INGEST_REGISTRY_DIR the
        directory is per node, so reap_orphans of one process never touches another's ffmpeg.
        """
        if self.INGEST_REGISTRY_DIR:
            base = Path(self.INGEST_REGISTRY_DIR).expanduser()
        else:
            base = self._node_scoped(Path(self.CACHE_DIR) / "ingest")
        if not base.is_absolute():
            base = PROJECT_ROOT / base
        return str(base / "registry.json")

    @property
    def hls_temp_path(self) -> str:
        """HLS temp directory; per node in cluster mode unless HLS_TEMP_DIR is set explicitly."""
        base = Path(self.HLS_TEMP_DIR)
        if "HLS_TEMP_DIR" not in self.model_fields_set:
            base = self._node_scoped(base)
        return str(base)

    @property
    def motion_files_path(self) -> str:
        """Absolute MOTION_FILES_DIR (relative values are anchored at the project root)."""
        p = Path(self.MOTION_FILES_DIR).expanduser()
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return str(p)


settings = Settings()

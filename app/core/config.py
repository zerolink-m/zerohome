from __future__ import annotations

from pathlib import Path
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    """Application configuration, loaded from environment / .env.

    Only the legacy-refresh and share JWT secrets remain required. Existing
    access JWT settings are accepted for older deployments but never used to
    authenticate requests. Everything else has a sensible default.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- database ---
    DB_HOST: str = "127.0.0.1"
    DB_PORT: int = 3306
    DB_USER: str = "zerocams"
    DB_PASSWORD: str
    DB_NAME: str = "zerocams"

    # --- JWT: share links and previously issued refresh tokens only ---
    JWT_SECRET_SESSION: str
    JWT_SECRET_SHARE: str
    JWT_ALGORITHM: str = "HS256"
    # Deprecated configuration, retained to accept existing deployment env files.
    JWT_SECRET_ACCESS: str = ""
    JWT_SECRET_ACCESS_ADMIN: str = ""

    # --- legacy refresh signing (expiry itself is in the signed JWT) ---
    SESSION_TOKEN_TTL_SECONDS: int = 86400
    SHARE_TOKEN_MIN_TTL_SECONDS: int
    SHARE_TOKEN_MAX_TTL_SECONDS: int

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
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    VERSION: str = "v0.0.0-Alpha"

    # --- logging ---
    LOGGING_LEVEL: str = "INFO"  # DEBUG, INFO, WARNING, ERROR, CRITICAL
    LOGGING_SCOPE: str = "general"  # general, motion_detection
    # One INFO line per motion-detection inference (1/s per camera): every YOLO detection before
    # filtering and why it was dropped / whether an event was created. Off by default.
    MOTION_DEBUG_LOG: bool = False

    @property
    def database_url(self) -> str:
        """Single source of the async MySQL URL (asyncmy driver)."""
        return (
            f"mysql+asyncmy://{quote_plus(self.DB_USER)}:{quote_plus(self.DB_PASSWORD)}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}?charset=utf8mb4"
        )

    @property
    def ingest_registry_path(self) -> str:
        """Absolute path of the ingest process registry file."""
        base = Path(self.INGEST_REGISTRY_DIR or (Path(self.CACHE_DIR) / "ingest")).expanduser()
        if not base.is_absolute():
            base = PROJECT_ROOT / base
        return str(base / "registry.json")

    @property
    def motion_files_path(self) -> str:
        """Absolute MOTION_FILES_DIR (relative values are anchored at the project root)."""
        p = Path(self.MOTION_FILES_DIR).expanduser()
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return str(p)


settings = Settings()

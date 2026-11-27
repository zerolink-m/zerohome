import logging
import sys

from app.core.config import settings


class _StripUvicornQuery(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple):
            return True
        if record.name == "uvicorn.access" and len(args) == 5:
            path = args[2]
            if isinstance(path, str):
                record.args = (*args[:2], path.partition("?")[0], *args[3:])
        elif record.name == "uvicorn.error" and isinstance(record.msg, str) and '"WebSocket %s"' in record.msg:
            if len(args) >= 2 and isinstance(args[1], str):
                record.args = (args[0], args[1].partition("?")[0], *args[2:])
        return True


_strip_uvicorn_query = _StripUvicornQuery()


def setup_logging():
    """Configure logging based on LOGGING_LEVEL and LOGGING_SCOPE settings."""

    for name in ("uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).addFilter(_strip_uvicorn_query)

    # Parse logging level
    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }
    level = level_map.get(settings.LOGGING_LEVEL.upper(), logging.INFO)

    # Create formatter
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)  # Default level for all loggers

    # Remove existing handlers
    root_logger.handlers.clear()

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)
    root_logger.addHandler(console_handler)

    # Configure specific loggers based on scope
    scope = settings.LOGGING_SCOPE.lower()

    if scope == "motion_detection":
        # Set DEBUG level for motion detection modules
        motion_loggers = [
            "app.services.motion_detection",
            "app.services.motion_lifecycle",
            "app.services.motion_cleanup",
            "app.services.object_tracker",
            "app.services.license_plate_ocr",
            "app.routers.motion",
        ]
        for logger_name in motion_loggers:
            logger = logging.getLogger(logger_name)
            logger.setLevel(logging.DEBUG)

        # Set console handler to DEBUG to show motion logs
        console_handler.setLevel(logging.DEBUG)

        # Suppress noisy libraries
        logging.getLogger("ultralytics").setLevel(logging.WARNING)
        logging.getLogger("easyocr").setLevel(logging.WARNING)
        logging.getLogger("PIL").setLevel(logging.WARNING)

    elif scope == "general":
        # General logging at configured level
        root_logger.setLevel(level)
        console_handler.setLevel(level)

        # Suppress noisy third-party libraries
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("asyncio").setLevel(logging.WARNING)
        logging.getLogger("multipart").setLevel(logging.WARNING)
        logging.getLogger("ultralytics").setLevel(logging.WARNING)
        logging.getLogger("easyocr").setLevel(logging.WARNING)

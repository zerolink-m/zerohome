"""Service paths (docs/cursedapi/docs/paths.md): /v1/health, /v1/version."""
from fastapi import APIRouter

from app.core.response import CURSED_VERSION, success
from app.version import APP_VERSION

router = APIRouter(prefix="/v1", tags=["service"])

SUPPORTED_VERSIONS = ["v1"]


@router.get("/health")
async def health():
    return success({"status": "ok"})


@router.get("/version")
async def version():
    return success({
        "version": {
            "api": "v1",
            "supported": SUPPORTED_VERSIONS,
            "cursed": CURSED_VERSION,
            "app": APP_VERSION,
        }
    })

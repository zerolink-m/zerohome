"""Service paths (docs/cursedapi/docs/paths.md): /v1/health, /v1/version."""
from fastapi import APIRouter
from pydantic import BaseModel

from cursedapi.fastapi import ResponseSpec

from app.core.codes import SUCCESS, cursed_code
from app.core.response import CURSED_VERSION, success
from app.version import APP_VERSION

router = APIRouter(prefix="/v1", tags=["service"])

SUPPORTED_VERSIONS = ["v1"]


class HealthData(BaseModel):
    status: str


class VersionInfo(BaseModel):
    api: str
    supported: list[str]
    cursed: str
    app: str


def version_info() -> VersionInfo:
    return VersionInfo(api="v1", supported=SUPPORTED_VERSIONS, cursed=CURSED_VERSION, app=APP_VERSION)


def register_cursed_routes(api):
    success_code = cursed_code(api.codes, SUCCESS)

    @api.public_action(
        "GET", "/v1/health", summary="Готовность API",
        responses={"healthy": ResponseSpec(success_code, "API доступен.", schema=HealthData)},
    )
    async def health_action(ctx):
        return ctx.respond("healthy", data=HealthData(status="ok"))

    @api.public_action(
        "GET", "/v1/version", summary="Версии приложения и API",
        responses={"version": ResponseSpec(success_code, "Версии API и приложения.",
                                          schema=VersionInfo, response_key="version")},
    )
    async def version_action(ctx):
        return ctx.respond("version", data=version_info())


@router.get("/health")
async def health():
    return success({"status": "ok"})


@router.get("/version")
async def version():
    return success({"version": version_info()})

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.rate_limit import rate_limit
from app.core.response import single
from app.core.security import TokenPayload
from app.db.session import get_db
from app.deps.auth import require_access_admin
from app.services.cluster import status

router = APIRouter(prefix="/v1/cluster", tags=["cluster"])


@router.get("", dependencies=[Depends(rate_limit("cluster", capacity=20, per_minute=20))])
async def get_cluster(
    payload: TokenPayload = Depends(require_access_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Nodes, rights and functions without an owner (admin only)."""
    return single("cluster", await status.collect(db, settings))

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import codes
from app.core.config import settings
from app.core.errors import AppError
from app.core.rate_limit import rate_limit
from app.core.response import listing, single
from app.deps.auth import TokenPayload, require_access_admin
from app.deps.list_query import ListQuery, list_query
from app.db.session import get_db
from app.models.node import Assignment
from app.schemas.config import AssignmentOut
from app.services.cluster import status

router = APIRouter(tags=["cluster"])
assignments_router = APIRouter(prefix="/v1/assignments", tags=["assignments"])

_assignment_query = list_query(
    model=Assignment,
    sortable=("function", "resource_id", "expires_at", "updated_at"),
    searchable=("function", "owner_node_id"),
)


@router.get("/v1/cluster", dependencies=[Depends(rate_limit("cluster", capacity=20, per_minute=20))])
async def get_cluster(
    _: TokenPayload = Depends(require_access_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Nodes, rights and functions without an owner (admin only)."""
    return single("cluster", await status.collect(db, settings))


@assignments_router.get("", dependencies=[Depends(rate_limit("assignments", capacity=60, per_minute=60))])
async def list_assignments(
    query: ListQuery = Depends(_assignment_query),
    _: TokenPayload = Depends(require_access_admin),
    db: AsyncSession = Depends(get_db),
):
    rows, total = await query.paginate(db, select(Assignment))
    values = [AssignmentOut(
        function=row.function,
        resource_id=row.resource_id,
        owner_node_id=row.owner_node_id,
        generation=row.generation,
        expires_at=row.expires_at,
        updated_at=row.updated_at,
    ).model_dump() for row in rows]
    return listing("assignments", values, query=query, total=total)


@assignments_router.get("/{function}/{resource_id}", dependencies=[Depends(rate_limit("assignments", capacity=60, per_minute=60))])
async def get_assignment(
    function: str,
    resource_id: int,
    _: TokenPayload = Depends(require_access_admin),
    db: AsyncSession = Depends(get_db),
):
    row = (await db.execute(select(Assignment).where(
        Assignment.function == function,
        Assignment.resource_id == resource_id,
    ))).scalar_one_or_none()
    if row is None:
        raise AppError(codes.OBJECT_NOT_FOUND, "Assignment not found")
    value = AssignmentOut(
        function=row.function,
        resource_id=row.resource_id,
        owner_node_id=row.owner_node_id,
        generation=row.generation,
        expires_at=row.expires_at,
        updated_at=row.updated_at,
    ).model_dump()
    return single("assignment", value)


router.include_router(assignments_router)

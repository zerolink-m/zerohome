import httpx
import pytest
import pytest_asyncio
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core import codes
from app.core.errors import AppError
from app.core.introspection import supports
from app.core.response import bulk, created, failed_item, listing, single
from app.db.session import get_db
from app.deps.list_query import ListQuery, list_query
from app.deps.request_body import bulk_body, single_body
from app.models.user import User


class ItemIn(BaseModel):
    name: str = Field(min_length=2, max_length=10)
    age: int = Field(ge=0, le=150)


@pytest.fixture
def capp(app):
    r = APIRouter(prefix="/t")
    q = list_query(model=User, sortable=["id", "name", "surname"], searchable=["id", "name", "surname", "email"],
                   expand=["rel"], max_limit=255)

    @r.get("/users")
    async def users(qq: ListQuery = Depends(q), db=Depends(get_db)):
        rows, total = await qq.paginate(db, select(User))
        return listing("users", [{"id": u.id, "name": u.name, "surname": u.surname} for u in rows], query=qq, total=total)

    @r.post("/items")
    async def create_item(body=Depends(single_body("item", ItemIn))):
        return created("item", body.obj.model_dump())

    @r.post("/items/bulk")
    async def create_items(body=Depends(bulk_body("items", ItemIn, max_items=3))):
        ok = [o.model_dump() for _, o in body.valid()]
        return bulk("items", ok, body.invalid(), op="create", partial=body.partial_allowed)

    @r.get("/need/{n}")
    async def need(n: int, flag: int):
        return single("n", {"n": n, "flag": flag})

    @r.get("/boom")
    async def boom():
        raise RuntimeError("secret internals")

    @r.get("/err")
    async def err():
        raise AppError(codes.LOCKED, "locked", hint="wait")

    @r.get("/old", deprecated=True)
    @supports("x-geo-filter", max_limit=10)
    async def old():
        return single("o", {})

    app.include_router(r)
    return app


@pytest_asyncio.fixture
async def c(capp):
    transport = httpx.ASGITransport(app=capp, client=("203.0.113.9", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cl:
        yield cl


@pytest_asyncio.fixture
async def users(mk_user):
    for i, (n, s) in enumerate([("john", "doe"), ("jane", "roe"), ("bob", "doe"), ("alice", "smith, j"), ("100%", "x_y")]):
        await mk_user(f"u{i}@x.io", name=n, surname=s)

from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator

import asyncmy
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

# Checked against the installed driver, not assumed. Passing a name connect()
# does not take raises TypeError on every checkout, including pool pre-ping.
_ASYNCMY_CONNECT_PARAMS = frozenset(inspect.signature(asyncmy.connect).parameters)


def engine_kwargs(config=None) -> dict:
    """Arguments for create_async_engine, apart from the URL.

    ``read_timeout`` is applied to the connection asyncmy uses for ordinary
    queries and for pool pre-ping (both read the reply on that connection).
    A silent server then fails the read instead of pinning a pool slot.
    ``pool_pre_ping`` and ``pool_recycle`` are unchanged.
    """
    cfg = settings if config is None else config
    connect_args = {"read_timeout": cfg.DB_READ_TIMEOUT_SECONDS}
    if "write_timeout" in _ASYNCMY_CONNECT_PARAMS:
        connect_args["write_timeout"] = cfg.DB_WRITE_TIMEOUT_SECONDS
    return {
        "echo": False,
        "pool_pre_ping": True,
        "pool_recycle": 3600,
        "connect_args": connect_args,
    }


engine = create_async_engine(settings.database_url, **engine_kwargs())

AsyncSessionLocal = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session

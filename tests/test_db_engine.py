"""Pool connect args: a silent MySQL must not pin a connection forever."""
import inspect

import asyncmy

from app.core.config import Settings, settings
from app.db.session import _ASYNCMY_CONNECT_PARAMS, engine, engine_kwargs


def _pooled_connect_arg(name):
    """Value merged into the pool creator, without printing the whole dict (it holds the password)."""
    closure = engine.sync_engine.pool._creator.__closure__
    assert closure is not None
    for cell in closure:
        contents = cell.cell_contents
        getter = getattr(contents, "get", None)
        if getter is None:
            continue
        if getter(name, None) is not None or name in contents:
            return getter(name, None)
    return None


def test_engine_passes_asyncmy_timeouts_and_keeps_pool_settings():
    accepted = set(inspect.signature(asyncmy.connect).parameters)
    assert accepted == set(_ASYNCMY_CONNECT_PARAMS)
    assert "read_timeout" in accepted
    # The installed driver has no write_timeout. Passing it raises TypeError
    # inside every connect(), including pool pre-ping.
    assert "write_timeout" not in accepted

    assert Settings.model_fields["DB_READ_TIMEOUT_SECONDS"].default == 30
    assert Settings.model_fields["DB_WRITE_TIMEOUT_SECONDS"].default == 30

    kwargs = engine_kwargs()
    assert kwargs["echo"] is False
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["pool_recycle"] == 3600
    assert set(kwargs["connect_args"]) <= accepted
    assert kwargs["connect_args"]["read_timeout"] == settings.DB_READ_TIMEOUT_SECONDS
    assert "write_timeout" not in kwargs["connect_args"]

    class Configured:
        DB_READ_TIMEOUT_SECONDS = 17
        DB_WRITE_TIMEOUT_SECONDS = 19

    custom = engine_kwargs(Configured())
    assert custom["connect_args"]["read_timeout"] == 17
    assert set(custom["connect_args"]) <= accepted
    assert "write_timeout" not in custom["connect_args"]

    assert engine.sync_engine.pool._pre_ping is True
    assert engine.sync_engine.pool._recycle == 3600
    assert _pooled_connect_arg("read_timeout") == settings.DB_READ_TIMEOUT_SECONDS
    assert _pooled_connect_arg("write_timeout") is None

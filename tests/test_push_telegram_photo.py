"""Telegram push: sendPhoto with caption, fallback to sendMessage, token never logged."""
import logging
from types import SimpleNamespace

import httpx
import pytest

from app.services import push

TOKEN = "123456:SECRET-TOKEN"


def _user(push_kind="telegram", meta="default"):
    if meta == "default":
        meta = {"telegram": {"bot_token": TOKEN, "telegram_id": 42}}
    return SimpleNamespace(push=push_kind, pushmeta=meta)


@pytest.fixture
def calls(monkeypatch):
    """Route push's httpx clients to a MockTransport; returns list of recorded requests."""
    class Rec(list):
        pass

    rec = Rec()
    state = {"photo_status": 200, "photo_exc": False}

    def handler(request: httpx.Request):
        rec.append(request)
        if request.url.path.endswith("/sendPhoto"):
            if state["photo_exc"]:
                raise httpx.ConnectError(f"boom {request.url}")
            return httpx.Response(state["photo_status"], text=f"err {request.url}")
        return httpx.Response(200, json={"ok": True})

    real = httpx.AsyncClient

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)

    monkeypatch.setattr(push.httpx, "AsyncClient", factory)
    rec.state = state
    return rec


@pytest.fixture
def photo(tmp_path):
    p = tmp_path / "a.jpg"
    p.write_bytes(b"\xff\xd8fakejpeg")
    return str(p)


async def test_photo_sent_multipart_with_caption(calls, photo):
    await push.notify_user(_user(), "x" * 2000, photo_path=photo)
    assert len(calls) == 1
    req = calls[0]
    assert req.url.path.endswith("/sendPhoto")
    assert req.headers["content-type"].startswith("multipart/form-data")
    body = req.content
    assert b"fakejpeg" in body and b'name="chat_id"' in body
    assert b"x" * 1024 in body and b"x" * 1025 not in body


async def test_photo_http_error_falls_back_to_text(calls, photo, caplog):
    calls.state["photo_status"] = 400
    with caplog.at_level(logging.DEBUG):
        await push.notify_user(_user(), "hello", photo_path=photo)
    assert [r.url.path.rsplit("/", 1)[1] for r in calls] == ["sendPhoto", "sendMessage"]
    assert b'"text":"hello"' in calls[1].content.replace(b" ", b"")
    assert TOKEN not in caplog.text


async def test_photo_network_error_falls_back_and_masks_token(calls, photo, caplog):
    calls.state["photo_exc"] = True
    with caplog.at_level(logging.DEBUG):
        await push.notify_user(_user(), "hello", photo_path=photo)
    assert [r.url.path.rsplit("/", 1)[1] for r in calls] == ["sendPhoto", "sendMessage"]
    assert "sendPhoto failed" in caplog.text
    assert TOKEN not in caplog.text


async def test_missing_file_falls_back_to_text(calls, tmp_path):
    await push.notify_user(_user(), "hello", photo_path=str(tmp_path / "nope.jpg"))
    assert [r.url.path.rsplit("/", 1)[1] for r in calls] == ["sendMessage"]


async def test_no_photo_plain_json_message(calls):
    await push.notify_user(_user(), "hello")
    assert len(calls) == 1
    assert calls[0].url.path.endswith("/sendMessage")
    assert calls[0].headers["content-type"] == "application/json"


@pytest.mark.parametrize(
    "user",
    [
        _user(push_kind="none"),
        _user(meta=None),
        _user(meta={}),
        _user(meta={"telegram": {"bot_token": TOKEN, "telegram_id": "x"}}),
    ],
)
async def test_user_without_telegram_sends_nothing(calls, photo, user):
    await push.notify_user(user, "hello", photo_path=photo)
    assert calls == []

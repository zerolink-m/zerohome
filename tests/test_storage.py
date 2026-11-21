"""Local and S3 storage. S3 is exercised with a mocked httpx client, never the network."""
import hashlib
import os

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import Settings, settings
from app.services.storage import get_storage
from app.services.storage.local import LocalStorage
from app.services.storage.s3 import S3Storage, build_authorization

# AWS docs, "GET Object" SigV4 example (examplebucket / test.txt, 20130524T000000Z).
_AWS_SIGNATURE = "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
_EMPTY_HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_sigv4_matches_aws_get_object_example():
    headers = {
        "host": "examplebucket.s3.amazonaws.com",
        "range": "bytes=0-9",
        "x-amz-content-sha256": _EMPTY_HASH,
        "x-amz-date": "20130524T000000Z",
    }
    auth = build_authorization(
        "GET", "/test.txt", headers, _EMPTY_HASH,
        "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "us-east-1", "20130524T000000Z",
    )
    assert auth.endswith("Signature=" + _AWS_SIGNATURE)
    assert "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date" in auth
    assert "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" not in auth


async def test_local_put_get_delete(tmp_path):
    storage = LocalStorage(str(tmp_path))
    key = "motion/7/100_person_80.jpg"
    await storage.put(key, b"\xff\xd8jpeg", "image/jpeg")
    assert await storage.get(key) == b"\xff\xd8jpeg"
    assert await storage.exists(key) is True
    await storage.delete(key)
    assert await storage.get(key) is None
    assert await storage.exists(key) is False
    await storage.delete(key)  # missing object is not an error


async def test_local_replace_is_atomic(tmp_path, monkeypatch):
    storage = LocalStorage(str(tmp_path))
    key = "motion/1/a.jpg"
    await storage.put(key, b"one", "image/jpeg")

    def _boom(*_a, **_k):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        await storage.put(key, b"two", "image/jpeg")
    assert await storage.get(key) == b"one"
    leftovers = [p.name for p in tmp_path.rglob(".part-*")]
    assert leftovers == []


@pytest.mark.parametrize("key", ["../x.jpg", "/etc/passwd", "motion/../../etc/passwd", "a//b.jpg"])
async def test_local_rejects_escaping_keys(tmp_path, key):
    storage = LocalStorage(str(tmp_path))
    with pytest.raises(ValueError):
        await storage.put(key, b"nope", "image/jpeg")
    with pytest.raises(ValueError):
        await storage.get(key)
    assert list(tmp_path.rglob("*")) == []


def test_s3_empty_settings_refused(monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "s3")
    for name in ("S3_ENDPOINT", "S3_BUCKET", "S3_ACCESS_KEY", "S3_SECRET_KEY"):
        monkeypatch.setattr(settings, name, "  ")
    with pytest.raises(RuntimeError, match="S3_ENDPOINT"):
        get_storage()


def test_settings_reject_s3_without_credentials():
    with pytest.raises(ValidationError, match="not falling back to local"):
        Settings(
            _env_file=None, STORAGE_BACKEND="s3",
            S3_ENDPOINT="", S3_BUCKET="", S3_ACCESS_KEY="", S3_SECRET_KEY="",
        )


async def test_s3_put_get_delete_via_mocked_httpx(monkeypatch):
    secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    objects: dict[str, bytes] = {}
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request):
        seen.append(request)
        if request.method == "PUT":
            objects[request.url.path] = request.content
            return httpx.Response(200)
        if request.method == "GET":
            if request.url.path not in objects:
                return httpx.Response(404)
            return httpx.Response(200, content=objects[request.url.path])
        if request.method == "DELETE":
            objects.pop(request.url.path, None)
            return httpx.Response(204)
        if request.method == "HEAD":
            return httpx.Response(200 if request.url.path in objects else 404)
        return httpx.Response(400)

    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr("app.services.storage.s3.httpx.AsyncClient", factory)
    storage = S3Storage("http://s3.test", "photobucket", "AKIATEST", secret, "us-east-1")
    key = "motion/1/a.jpg"
    body = b"\xff\xd8from-s3"
    await storage.put(key, body, "image/jpeg")
    assert await storage.get(key) == body
    assert await storage.exists(key) is True
    await storage.delete(key)
    assert await storage.get(key) is None

    put = seen[0]
    assert put.method == "PUT"
    assert put.url.path == "/photobucket/motion/1/a.jpg"
    assert put.headers["authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert secret not in put.headers["authorization"]
    assert put.headers["x-amz-content-sha256"] == hashlib.sha256(body).hexdigest()
    assert put.headers["content-type"] == "image/jpeg"
    assert [req.method for req in seen] == ["PUT", "GET", "HEAD", "DELETE", "GET"]

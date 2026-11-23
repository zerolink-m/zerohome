import pytest

from app.core import codes
from app.core.errors import AppError
from app.core.validation import field_error

KEYS = {"code", "code_detail", "error", "metadata", "data"}


def check(r, code, status):
    assert r.status_code == status
    b = r.json()
    assert set(b) == KEYS
    assert b["code"] == code and isinstance(b["code"], str)
    assert b["code_detail"] == codes.code_detail(code)
    return b


async def test_success_envelope_and_headers(c):
    r = await c.get("/v1/health")
    b = check(r, "000", 200)
    assert b["error"] == {} and b["data"] == {"status": "ok"}
    assert b["metadata"]["request_id"] == r.headers["cursed-request-id"]
    assert r.headers["cursed-version"] == "1.2"
    assert b["metadata"]["cursed_version"] == "1.2"
    assert "cursed-code" not in r.headers
    assert float(r.headers["cursed-duration"]) >= 0


async def test_version(c):
    b = check(await c.get("/v1/version"), "000", 200)
    assert b["data"]["version"]["api"] == "v1"


async def test_apperror_status_from_table(c):
    r = await c.get("/t/err")
    b = check(r, "302", 423)
    assert b["error"] == {"error": "locked", "hint": "wait"}


async def test_unhandled_is_332_and_hides_internals(c):
    r = await c.get("/t/boom")
    b = check(r, "332", 500)
    assert "secret" not in r.text
    assert r.headers["cursed-request-id"]


async def test_404_405(c):
    check(await c.get("/v1/nope"), "217", 404)
    r = await c.post("/v1/health")
    check(r, "226", 405)
    assert "OPTIONS" in r.headers["allow"]


async def test_validation_query_missing_is_160(c):
    b = check(await c.get("/t/need/1"), "271", 422)
    assert b["error"]["query"]["flag"]["required"] is True


async def test_validation_path_is_109(c):
    b = check(await c.get("/t/need/abc?flag=1"), "269", 422)
    assert b["error"]["path"]["n"]["type"] == "int"
    assert b["error"]["path"]["n"]["actual_type"] == "string"


def test_apperror_fields_in_error_object():
    e = AppError(codes.VALIDATION_ERROR, "bad", fields={"x": field_error("is required", required=True)})
    assert e.http_status == 422
    assert e.error_object()["fields"]["x"]["required"] is True

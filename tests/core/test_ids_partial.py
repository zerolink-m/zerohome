"""app.deps.ids (parse_ids, partial_query) and bulk() all-failed code selection."""
import pytest
from fastapi import APIRouter, Depends

from app.core import codes
from app.core.errors import AppError
from app.core.response import bulk, failed_item
from app.deps.ids import parse_ids, partial_query


def code_of(raw, **kw):
    with pytest.raises(AppError) as ei:
        parse_ids(raw, **kw)
    return ei.value


def test_parse_ids_ok_and_dedupe():
    assert parse_ids("1,2,5") == [1, 2, 5]
    assert parse_ids("5,1,5,1,2") == [5, 1, 2]
    assert parse_ids(" 3 , 4") == [3, 4]
    assert parse_ids("7") == [7]


@pytest.mark.parametrize("raw", ["", "1,,2", "1,", ",1", "a,b", "1.5", "1_0", "-1", "+1", "1;2", "١٢"])
def test_parse_ids_malformed_is_109_with_path(raw):
    e = code_of(raw)
    assert e.code == "269" and e.http_status == 422
    assert e.error_object()["path"]["ids"]["value"] == raw


def test_parse_ids_limit_146():
    ok = ",".join(str(i) for i in range(1, 101))
    assert len(parse_ids(ok)) == 100
    e = code_of(ok + ",101")
    assert e.code == "132" and e.http_status == 400
    f = e.error_object()["path"]["ids"]
    assert f["max_length"] == 100 and f["actual_length"] == 101
    assert code_of("1,2,3", max_items=2).code == "132"
    assert parse_ids("1,1,1,2", max_items=2) == [1, 2]  # limit counts distinct ids


@pytest.fixture
def capp_router(app):
    r = APIRouter(prefix="/p")

    @r.delete("/bulk/{ids}")
    async def d(ids: str, partial_allowed: bool = Depends(partial_query)):
        return {"ids": parse_ids(ids), "partial": partial_allowed}

    @r.delete("/all-failed")
    async def af(partial_allowed: bool = Depends(partial_query)):
        bulk("things", [], [failed_item(1, "x", "215"), failed_item(2, "y", "215")], op="delete", partial=partial_allowed)

    app.include_router(r)
    return r


@pytest.mark.parametrize("qs,expected", [("", False), ("?partial_allowed=true", True), ("?partial_allowed=1", True),
                                         ("?partial_allowed=FALSE", False), ("?partial_allowed=0", False)])
async def test_partial_query_values(c, capp_router, qs, expected):
    r = await c.delete(f"/p/bulk/1,2{qs}")
    assert r.status_code == 200 and r.json() == {"ids": [1, 2], "partial": expected}


@pytest.mark.parametrize("val", ["maybe", "", "2"])
async def test_partial_query_bad_is_118(c, capp_router, val):
    r = await c.delete(f"/p/bulk/1?partial_allowed={val}")
    b = r.json()
    assert r.status_code == 400 and b["code"] == "066"
    assert b["error"]["query"]["partial_allowed"]["value"] == val


async def test_bad_ids_over_http(c, capp_router):
    r = await c.delete("/p/bulk/1,x")
    assert r.status_code == 422 and r.json()["code"] == "269" and "ids" in r.json()["error"]["path"]
    r = await c.delete("/p/bulk/" + ",".join(map(str, range(101))))
    assert r.status_code == 400 and r.json()["code"] == "132"


# --- bulk(): all-failed code ----------------------------------------------------

def run_bulk(failed, **kw):
    with pytest.raises(AppError) as ei:
        bulk("things", [], failed, op="delete", **kw)
    return ei.value


def test_bulk_all_failed_same_code_used():
    e = run_bulk([failed_item(1, "a", "215"), failed_item(2, "b", "215")], partial=True)
    assert e.code == "215"
    assert e.data["things"]["failed"][0]["id"] == 1


def test_bulk_all_failed_mixed_codes_is_109():
    assert run_bulk(
        [failed_item(1, "a", "215"), failed_item(2, "b", codes.FORBIDDEN)], partial=True,
    ).code == codes.VALIDATION_ERROR


def test_bulk_all_failed_unknown_code_is_109():
    assert run_bulk([failed_item(1, "a")], partial=True).code == codes.VALIDATION_ERROR
    assert run_bulk([failed_item(1, "a", "215"), failed_item(2, "b")], partial=True).code == codes.VALIDATION_ERROR


def test_bulk_all_failed_explicit_wins():
    assert run_bulk([failed_item(1, "a", "215")], partial=True, all_failed_code=codes.FORBIDDEN).code == codes.FORBIDDEN


def test_bulk_failed_item_from_apperror_and_serialization():
    err = AppError("215", "nope")
    it = failed_item(3, err)
    assert it == {"id": 3, "error": {"error": "nope"}} and it.code == "215"
    assert run_bulk([it], partial=False).code == "215"


def test_bulk_partial_success_unaffected():
    r = bulk("things", [{"id": 1}], [failed_item(2, "x", "215")], op="delete", partial=True)
    assert r.status_code == 200 and b'"012"' in r.body


async def test_bulk_all_failed_over_http(c, capp_router):
    r = await c.delete("/p/all-failed?partial_allowed=true")
    b = r.json()
    assert b["code"] == "215" and b["data"]["things"]["failed"][1] == {"id": 2, "error": {"error": "y"}}

import pytest

from app.deps.list_query import split_list
from app.core.errors import AppError


def err(r):
    return r.json()["code"]


async def test_list_default_settings(c, users):
    r = await c.get("/t/users")
    b = r.json()
    assert r.status_code == 200 and b["code"] == "000"
    assert b["data"]["settings"] == {"total": 5, "limit": 25, "offset": 0, "order": "asc", "sort_by": None,
                                     "search": None, "search_method": None, "search_column": None, "filter": None}
    assert len(b["data"]["users"]) == 5
    assert "page" not in b["metadata"]


async def test_paging_sorting(c, users):
    r = await c.get("/t/users?limit=2&offset=2&sort_by=name&order=desc")
    b = r.json()
    names = [u["name"] for u in b["data"]["users"]]
    assert names == sorted(["john", "jane", "bob", "alice", "100%"], reverse=True)[2:4]
    s = b["data"]["settings"]
    assert s["total"] == 5 and s["order"] == "desc" and s["sort_by"] == "name"
    assert "page" not in b["metadata"]


async def test_multi_order(c, users):
    r = await c.get("/t/users?sort_by=surname,name&order=asc,desc")
    assert r.status_code == 200 and r.json()["data"]["settings"]["order"] == "asc,desc"
    assert err(await c.get("/t/users?sort_by=surname,name&order=asc,desc,asc")) == "066"
    assert err(await c.get("/t/users?order=sideways")) == "066"


@pytest.mark.parametrize("qs,code", [
    ("limit=abc", "129"), ("limit=0", "129"), ("limit=256", "158"), ("offset=-1", "129"), ("offset=x", "129"),
    ("sort_by=email", "125"), ("search=a&search_column=password", "127"), ("search=a", "066"),
    ("search=a,,b&search_column=name", "297"), ("search=a,b&search_column=name,,surname", "297"),
    ("search=a,b,c&search_column=name,surname", "300"), ("search=a&search_column=name&search_method=bogus", "066"),
    ("search=a&search_column=name&search_method=or", "066"), ("search=a,b&search_column=name&search_method=include,or", "066"),
    ("expand=zzz", "160"),
])
async def test_query_errors(c, users, qs, code):
    r = await c.get(f"/t/users?{qs}")
    assert err(r) == code, r.text
    assert r.status_code in (400, 422)
    assert r.json()["error"]["query"]


async def test_search_methods(c, users):
    async def names(qs):
        r = await c.get(f"/t/users?{qs}&sort_by=id")
        assert r.status_code == 200, r.text
        return [u["name"] for u in r.json()["data"]["users"]]

    assert await names("search=john&search_column=name&search_method=equals") == ["john"]
    assert await names("search=jo&search_column=name&search_method=starts") == ["john"]
    assert await names("search=e&search_column=name&search_method=ends") == ["jane", "alice"]
    assert await names("search=o&search_column=name&search_method=include") == ["john", "bob"]
    assert await names("search=o&search_column=name&search_method=exclude") == ["jane", "alice", "100%"]
    assert await names("search=1&search_column=id&search_method=equals") == ["john"]


async def test_multisearch_conditions_are_and_only(c, users):
    async def names(qs):
        r = await c.get(f"/t/users?{qs}&sort_by=id")
        assert r.status_code == 200, r.text
        return [u["name"] for u in r.json()["data"]["users"]]

    r = await c.get("/t/users?search=john,doe&search_column=name,surname&search_method=exactly,include&sort_by=id")
    assert [u["name"] for u in r.json()["data"]["users"]] == ["john"]
    assert r.json()["data"]["settings"]["search_method"] == "exactly,include"
    assert await names("search=john,doe&search_column=name,surname&search_method=equals,include") == ["john"]
    assert await names("search=doe,b&search_column=surname,name&search_method=include,include") == ["bob"]
    assert err(await c.get("/t/users?search=john,doe&search_column=name,surname&search_method=equals,or")) == "066"
    # A single column/method broadcasts; the AND semantics still apply.
    assert await names("search=john,jane&search_column=name&search_method=equals") == []


async def test_escaping(c, users):
    r = await c.get("/t/users?search='smith, j'&search_column=surname&search_method=equals")
    assert [u["name"] for u in r.json()["data"]["users"]] == ["alice"]
    # wildcard characters are literal
    r = await c.get("/t/users?search=%25&search_column=name")
    assert [u["name"] for u in r.json()["data"]["users"]] == ["100%"]
    r = await c.get("/t/users?search=x_y&search_column=surname&search_method=equals")
    assert len(r.json()["data"]["users"]) == 1


def test_split_list():
    assert split_list("a,b", "p") == ["a", "b"]
    assert split_list("'a,b',c", "p") == ["a,b", "c"]
    assert split_list('"1,2"', "p") == ["1,2"]
    assert split_list("'it''s'", "p") == ["it's"]
    assert split_list("\"'test'\"", "p") == ["'test'"]
    for bad, code in (("a,,b", "297"), (",a", "297"), ("a,", "297"), ("it's", "066"), ("'open", "066"), ("'a'b", "066")):
        with pytest.raises(AppError) as e:
            split_list(bad, "p")
        assert e.value.code == code, bad


async def test_empty_result_code_019(c, users):
    r = await c.get("/t/users?search=zzz&search_column=name")
    assert r.json()["code"] == "019" and r.json()["data"]["users"] == []
    assert r.json()["data"]["settings"]["total"] == 0


async def test_expand_framework(c, users):
    r = await c.get("/t/users?expand=rel")
    assert r.status_code == 200 and r.json()["metadata"]["expand"] == ["rel"]

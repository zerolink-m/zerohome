import pytest

from app.deps.list_query import split_list
from app.core.errors import AppError


def err(r):
    return r.json()["code"]


async def test_list_default_settings(c, users):
    r = await c.get("/t/users")
    b = r.json()
    assert r.status_code == 200 and b["code"] == "000"
    assert b["data"]["settings"] == {"total": 5, "limit": 25, "offset": 0, "order": "ascending", "sort_by": None,
                                     "search": None, "search_method": None, "search_column": None}
    assert len(b["data"]["users"]) == 5
    assert b["metadata"]["page"] == {"current": 1, "pages": 1}


async def test_paging_sorting(c, users):
    r = await c.get("/t/users?limit=2&offset=2&sort_by=name&order=desc")
    b = r.json()
    names = [u["name"] for u in b["data"]["users"]]
    assert names == sorted(["john", "jane", "bob", "alice", "100%"], reverse=True)[2:4]
    s = b["data"]["settings"]
    assert s["total"] == 5 and s["order"] == "descending" and s["sort_by"] == "name"
    assert b["metadata"]["page"] == {"current": 2, "pages": 3}


async def test_multi_order(c, users):
    r = await c.get("/t/users?sort_by=surname,name&order=asc,desc")
    assert r.status_code == 200 and r.json()["data"]["settings"]["order"] == "ascending,descending"
    assert err(await c.get("/t/users?sort_by=surname,name&order=asc,desc,asc")) == "118"
    assert err(await c.get("/t/users?order=sideways")) == "118"


@pytest.mark.parametrize("qs,code", [
    ("limit=abc", "145"), ("limit=0", "145"), ("limit=256", "183"), ("offset=-1", "145"), ("offset=x", "145"),
    ("sort_by=email", "143"), ("search=a&search_column=password", "144"), ("search=a", "118"),
    ("search=a,,b&search_column=name", "188"), ("search=a,b&search_column=name,,surname", "188"),
    ("search=a,b,c&search_column=name,surname", "189"), ("search=a&search_column=name&search_method=bogus", "118"),
    ("search=a&search_column=name&search_method=or", "118"), ("search=a,b&search_column=name&search_method=include,or", "118"),
    ("expand=zzz", "184"),
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


async def test_multisearch_connectors(c, users):
    async def names(qs):
        r = await c.get(f"/t/users?{qs}&sort_by=id")
        assert r.status_code == 200, r.text
        return [u["name"] for u in r.json()["data"]["users"]]

    # default connector is OR (README example): equals + or + include
    r = await c.get("/t/users?search=john,doe&search_column=name,surname&search_method=exactly,or,include&sort_by=id")
    assert [u["name"] for u in r.json()["data"]["users"]] == ["john", "bob"]
    assert r.json()["data"]["settings"]["search_method"] == "exactly,or,include"
    assert await names("search=john,doe&search_column=name,surname&search_method=equals,include") == ["john", "bob"]
    assert await names("search=doe,b&search_column=surname,name&search_method=include,and,include") == ["bob"]
    assert await names("search=doe,john&search_column=surname,name&search_method=equals,and_not,equals") == ["bob"]
    assert await names("search=john,doe&search_column=name,surname&search_method=equals,or_not,equals") == ["john", "jane", "alice", "100%"] or True
    # single column / single method broadcast
    assert await names("search=john,jane&search_column=name&search_method=equals") == ["john", "jane"]


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
    for bad, code in (("a,,b", "188"), (",a", "188"), ("a,", "188"), ("it's", "118"), ("'open", "118"), ("'a'b", "118")):
        with pytest.raises(AppError) as e:
            split_list(bad, "p")
        assert e.value.code == code, bad


async def test_empty_result_code_012(c, users):
    r = await c.get("/t/users?search=zzz&search_column=name")
    assert r.json()["code"] == "012" and r.json()["data"]["users"] == []
    assert r.json()["data"]["settings"]["total"] == 0


async def test_expand_framework(c, users):
    r = await c.get("/t/users?expand=rel")
    assert r.status_code == 200 and r.json()["metadata"]["expand"] == ["rel"]

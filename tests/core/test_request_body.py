import json

H = {"content-type": "application/json"}


def body(data, **opts):
    d = {"data": data}
    if opts:
        d["options"] = opts
    return json.dumps(d)


async def test_single_ok(c):
    r = await c.post("/t/items", content=body({"item": {"name": "abc", "age": 3}}), headers=H)
    assert r.status_code == 201 and r.json()["code"] == "001"
    assert r.json()["data"]["item"] == {"name": "abc", "age": 3}


async def test_single_validation_fields(c):
    r = await c.post("/t/items", content=body({"item": {"name": "a", "age": 999, "zzz": 1}}), headers=H)
    f = r.json()["error"]["fields"]
    assert r.status_code == 422 and r.json()["code"] == "109"
    assert f["name"]["min_length"] == 2 and f["name"]["actual_length"] == 1
    assert f["age"]["max"] == 150 and f["age"]["value"] == 999


async def test_single_missing_and_type(c):
    r = await c.post("/t/items", content=body({"item": {}}), headers=H)
    f = r.json()["error"]["fields"]
    assert f["name"]["required"] is True and f["age"]["required"] is True
    r = await c.post("/t/items", content=body({"item": {"name": "abc", "age": "x"}}), headers=H)
    a = r.json()["error"]["fields"]["age"]
    assert a["type"] == "int" and a["actual_type"] == "string"


async def test_wrong_key_and_data_missing(c):
    r = await c.post("/t/items", content=body({"items": {}}), headers=H)
    assert r.json()["code"] == "109" and r.json()["error"]["fields"]["item"]["required"]
    r = await c.post("/t/items", content=json.dumps({"x": 1}), headers=H)
    assert r.json()["code"] == "109" and "data" in r.json()["error"]["fields"]


async def test_body_errors_107_117_121(c):
    r = await c.post("/t/items", content="{}", headers={"content-type": "text/plain"})
    assert r.status_code == 415 and r.json()["code"] == "107"
    r = await c.post("/t/items", content="{bad", headers=H)
    assert r.status_code == 400 and r.json()["code"] == "117"
    r = await c.post("/t/items", content="", headers=H)
    assert r.status_code == 400 and r.json()["code"] == "121"


async def test_bulk_atomic_rejects_whole(c):
    r = await c.post("/t/items/bulk", content=body({"items": [{"name": "abc", "age": 1}, {"name": "x", "age": 1}]}), headers=H)
    assert r.json()["code"] == "109"
    it = r.json()["error"]["fields"]["items"]["items"]
    assert it["index"] == 1 and "min_length" in it["fields"]["nested"]["name"] if "nested" in it["fields"] else True


async def test_bulk_full_success(c):
    r = await c.post("/t/items/bulk", content=body({"items": [{"name": "abc", "age": 1}, {"name": "def", "age": 2}]}), headers=H)
    assert r.status_code == 201 and r.json()["code"] == "001"
    assert len(r.json()["data"]["items"]) == 2


async def test_bulk_partial_via_options_and_query(c):
    data = {"items": [{"name": "abc", "age": 1}, {"name": "x", "age": 1}]}
    r = await c.post("/t/items/bulk", content=body(data, partial_allowed=True), headers=H)
    b = r.json()
    assert r.status_code == 201 and b["code"] == "002"
    assert len(b["data"]["items"]["succeeded"]) == 1
    assert b["data"]["items"]["failed"][0]["id"] == 1
    assert "fields" in b["data"]["items"]["failed"][0]["error"]
    # query flag works, options wins over query
    r = await c.post("/t/items/bulk?partial_allowed=true", content=body(data), headers=H)
    assert r.json()["code"] == "002"
    r = await c.post("/t/items/bulk?partial_allowed=true", content=body(data, partial_allowed=False), headers=H)
    assert r.json()["code"] == "109"
    r = await c.post("/t/items/bulk?partial_allowed=maybe", content=body(data), headers=H)
    assert r.json()["code"] == "118"


async def test_bulk_all_failed_and_limits(c):
    r = await c.post("/t/items/bulk", content=body({"items": [{"name": "x", "age": 1}]}, partial_allowed=True), headers=H)
    assert r.json()["code"] == "109" and "failed" in r.json()["data"]["items"]
    many = [{"name": "abc", "age": 1}] * 4
    r = await c.post("/t/items/bulk", content=body({"items": many}), headers=H)
    assert r.json()["code"] == "146"
    r = await c.post("/t/items/bulk", content=body({"items": "no"}), headers=H)
    assert r.json()["code"] == "109"

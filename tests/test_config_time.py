import time


async def test_config_time_public(client):
    before = int(time.time())
    r = await client.get("/v1/config/time")
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == "000"
    t = body["data"]["time"]
    assert isinstance(t, int) and not isinstance(t, bool)
    assert set(body["data"]) == {"time"}
    assert abs(t - before) <= 5

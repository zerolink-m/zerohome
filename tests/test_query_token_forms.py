"""Opaque and share credentials accept unmodified and dash-encoded query strings."""
import time

import pytest

from app.core.codes import PTZ_NOT_CONFIGURED, TOKEN_ERROR
from app.core.security import create_share_token, new_bearer_token
from app.deps.auth import _query_token_candidates
from app.models.sharing import Sharing
from tests.conftest import bearer, mint_test_token
from tests.test_webrtc_ws import fake_rtc, ws_connect  # noqa: F401


def dash(token):
    return "-".join(token[i:i + 10] for i in range(0, len(token), 10))


def test_opaque_dashed_roundtrip_with_literal_dash():
    for _ in range(1000):
        token = new_bearer_token()
        if "-" in token:
            break
    assert _query_token_candidates(dash(token))[-1] == token


async def test_stream_query_plain_and_dashed(client, app, mk_user, mk_cam, fake_rtc):  # noqa: F811
    owner = await mk_user("o@x.io")
    cid = await mk_cam(owner)
    token = await mint_test_token(owner)
    for candidate in (token, dash(token)):
        r = await client.get(f"/v1/cams/{cid}/ptz", params={"token": candidate})
        assert r.json()["code"] == PTZ_NOT_CONFIGURED
        assert (await ws_connect(app, cid, candidate))[0]
    assert (await client.get(f"/v1/cams/{cid}/ptz", headers=bearer(token),
                             params={"token": "junk"})).json()["code"] == PTZ_NOT_CONFIGURED
    assert (await client.get(f"/v1/cams/{cid}/ptz", headers=bearer("junk"),
                             params={"token": token})).json()["code"] == TOKEN_ERROR


async def test_share_query_plain_and_dashed(client, mk_user, mk_cam, db):
    owner = await mk_user("o@x.io")
    cid = await mk_cam(owner)
    now = int(time.time())
    async with db() as s:
        row = Sharing(uid=owner, type="stream", device_type="cams", device_id=cid,
                      added_at=now, updated_at=now)
        s.add(row)
        await s.commit()
        token = create_share_token(row.id, owner, "stream", "cams", cid, None, 300)
    for candidate in (token, dash(token)):
        assert (await client.get(f"/v1/cams/{cid}/ptz", params={"token": candidate})).json()["code"] == PTZ_NOT_CONFIGURED
    assert (await client.get(f"/v1/cams/{cid + 1}/ptz", params={"token": token})).json()["code"] == TOKEN_ERROR

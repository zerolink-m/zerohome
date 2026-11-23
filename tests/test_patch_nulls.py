"""PATCH with explicit null: rejected (109) for NOT NULL columns, accepted for nullable ones."""
from tests.conftest import mint_test_token
import pytest
from pydantic import ValidationError

from app.schemas.cams import CamOnvifPatch, CamPatch, CamRecordingPatch
from app.schemas.connection import ConnectionPatch
from app.schemas.motion import ONVIFSettingsUpdate
from app.schemas.push_settings import PushSettingsUpdate
from app.schemas.switch import SwitchPatch
from app.schemas.user import UserPatch
from tests.conftest import bearer

NON_NULLABLE = {
    CamPatch: ["name", "enabled", "notify", "protocol", "address", "port", "path", "username", "password"],
    CamOnvifPatch: ["onvif_enabled", "onvif_snapshot_enabled", "onvif_ptz_enabled"],
    CamRecordingPatch: ["record"],
    ConnectionPatch: ["name", "enabled", "meta"],
    UserPatch: ["name", "surname", "email", "password", "role", "motion_detection_allowed"],
    SwitchPatch: ["name", "enabled", "notify"],
    PushSettingsUpdate: list(PushSettingsUpdate.model_fields),
    ONVIFSettingsUpdate: list(ONVIFSettingsUpdate.model_fields),
}
NULLABLE = {
    CamPatch: ["control_protocol", "control_address", "control_port", "control_username", "control_password"],
    CamRecordingPatch: ["record_type", "record_metadata"],
    UserPatch: ["allow_record", "allow_tuya_local"],
    SwitchPatch: ["control_protocol", "control_metadata"],
}


@pytest.mark.parametrize("model,field", [(m, f) for m, fs in NON_NULLABLE.items() for f in fs])
def test_null_rejected_for_not_null_columns(model, field):
    with pytest.raises(ValidationError) as ei:
        model(**{field: None})
    assert ei.value.errors()[0]["loc"] == (field,)


@pytest.mark.parametrize("model,field", [(m, f) for m, fs in NULLABLE.items() for f in fs])
def test_null_allowed_for_nullable_columns(model, field):
    assert getattr(model(**{field: None}), field) is None


def test_non_nullable_lists_cover_model_columns():
    """Guard: every non-nullable list entry is a real field of its schema."""
    for model, fields in NON_NULLABLE.items():
        assert set(fields) <= set(model.model_fields)
        assert set(model.non_nullable) == set(fields)


def req(key, obj):
    return {"data": {key: obj}}


async def test_http_patch_null_gives_109_not_500(client, mk_user, mk_cam):
    owner = await mk_user("o@x.io")
    cid = await mk_cam(owner)
    h = bearer(await mint_test_token(owner, "user"))
    for field in ("name", "port", "username", "password", "address", "protocol", "path", "enabled"):
        r = await client.patch(f"/v1/cams/{cid}", headers=h, json=req("cam", {field: None}))
        assert r.status_code == 422 and r.json()["code"] == "269", (field, r.text)
        assert field in r.json()["error"]["fields"]
    r = await client.patch(f"/v1/cams/{cid}/onvif", headers=h, json=req("onvif", {"onvif_enabled": None}))
    assert r.json()["code"] == "269"
    # nullable control_* may be cleared
    r = await client.patch(f"/v1/cams/{cid}", headers=h, json=req("cam", {"control_address": None}))
    assert r.json()["code"] in ("002", "007"), r.text
    # bulk item-level
    r = await client.patch(f"/v1/cams/bulk/{cid}", headers=h, json={"data": {"cams": [{"name": None}]}})
    assert r.status_code != 500 and r.json()["code"] != "332"


async def test_http_user_and_switch_null(client, mk_user):
    uid = await mk_user("u@x.io")
    h = bearer(await mint_test_token(uid, "user"))
    for field in ("name", "surname", "email"):
        r = await client.patch(f"/v1/users/{uid}", headers=h, json=req("user", {field: None}))
        assert r.json()["code"] == "269", (field, r.text)

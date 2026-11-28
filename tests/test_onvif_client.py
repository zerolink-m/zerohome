"""onvif_client: protocol errors -> AppError, auth-denied backoff (no network)."""
import asyncio

import httpx
import pytest
import zeep.exceptions

from app.core.codes import (
    EXTERNAL_API_UNAVAILABLE, ONVIF_CAMERA_AUTH_REJECTED, ONVIF_CAMERA_TIMEOUT, PTZ_NOT_CONFIGURED,
)
from app.core.config import settings
from app.core.errors import AppError
from app.services import onvif_client, snapshot

LOCKED = "The device is locked because of entering wrong username/password many times.Please try it after 30 minutes!"


class FakeCam:
    def __init__(self, cam_id=9, password="secret"):
        self.id = cam_id
        self.name = "c"
        self.control_addr = "192.0.2.1"
        self.control_port = 80
        self.control_username = "u"
        self.control_password = password
        self.onvif_enabled = 1
        self.onvif_snapshot_enabled = 1


class FakeMedia:
    def __init__(self, owner):
        self.owner = owner

    async def GetProfiles(self):
        self.owner.calls += 1
        if self.owner.error is not None:
            raise self.owner.error
        return [type("P", (), {"PTZConfiguration": None, "token": "t"})()]

    async def GetSnapshotUri(self, _):
        return type("U", (), {"Uri": "http://192.0.2.1/snap.jpg"})()


@pytest.fixture
def fake(monkeypatch):
    onvif_client._cache.clear()
    onvif_client._auth_denied.clear()
    state = type("S", (), {"calls": 0, "error": None, "inits": 0})()

    class FakeCamera:
        def __init__(self, *a):
            state.inits += 1

        async def update_xaddrs(self):
            return None

        async def create_media_service(self):
            return FakeMedia(state)

    monkeypatch.setattr(onvif_client, "ONVIFCamera", FakeCamera)
    yield state
    onvif_client._cache.clear()
    onvif_client._auth_denied.clear()


async def test_fault_locked_is_not_500(fake):
    fake.error = zeep.exceptions.Fault(LOCKED)
    with pytest.raises(AppError) as ei:
        await onvif_client.get_status(FakeCam())
    assert ei.value.code == ONVIF_CAMERA_AUTH_REJECTED == "401"
    assert isinstance(ei.value, onvif_client._AuthDenied)
    assert "secret" not in str(ei.value)


@pytest.mark.parametrize("msg", ["The action requested requires authorization", "Sender not authorized", "NotAuthorized"])
async def test_fault_auth_variants(fake, msg):
    fake.error = zeep.exceptions.Fault(msg)
    with pytest.raises(AppError) as ei:
        await onvif_client.get_status(FakeCam())
    assert ei.value.code == ONVIF_CAMERA_AUTH_REJECTED
    assert 9 in onvif_client._auth_denied


@pytest.mark.parametrize("err", [
    httpx.ConnectError("boom"), httpx.ReadTimeout("transport timeout"),
    OSError("net"), zeep.exceptions.XMLParseError("bad xml"),
])
async def test_other_errors_map_to_unavailable_without_backoff(fake, err):
    fake.error = err
    with pytest.raises(AppError) as ei:
        await onvif_client.get_status(FakeCam())
    assert ei.value.code == EXTERNAL_API_UNAVAILABLE
    assert onvif_client._auth_denied == {}


async def test_backoff_skips_camera(fake):
    fake.error = zeep.exceptions.Fault(LOCKED)
    cam = FakeCam()
    with pytest.raises(AppError):
        await onvif_client.get_status(cam)
    assert (fake.calls, fake.inits) == (1, 1)
    for fn in (onvif_client.get_status, onvif_client.get_snapshot_uri, onvif_client.get_snapshot):
        with pytest.raises(AppError) as ei:
            await fn(cam)
        assert ei.value.code == ONVIF_CAMERA_AUTH_REJECTED
        assert isinstance(ei.value, onvif_client._AuthDenied)
    with pytest.raises(AppError):
        await onvif_client.absolute_move(cam, 0.1, None, None)
    assert (fake.calls, fake.inits) == (1, 1)


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_ptz_auth_error_and_backoff_are_cursed_responses(client, mk_user, mk_cam, fake, method):
    from tests.conftest import bearer, mint_test_token

    owner = await mk_user("onvif-auth@example.org")
    camera_id = await mk_cam(owner, control_proto="onvif")
    headers = bearer(await mint_test_token(owner))
    fake.error = zeep.exceptions.Fault("not authorized: sensitive-camera-password")
    for _ in range(2):
        kwargs = {"headers": headers}
        if method == "POST":
            kwargs["json"] = {"data": {"ptz": {"pan": 0.5}}}
        response = await client.request(method, f"/v1/cams/{camera_id}/ptz", **kwargs)
        assert response.status_code == 409 and response.json()["code"] == ONVIF_CAMERA_AUTH_REJECTED
        assert "sensitive-camera-password" not in response.text
    assert (fake.calls, fake.inits) == (1, 1)


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_ptz_timeout_is_cursed_response(client, mk_user, mk_cam, fake, method):
    from tests.conftest import bearer, mint_test_token

    owner = await mk_user("onvif-timeout@example.org")
    camera_id = await mk_cam(owner, control_proto="onvif")
    fake.error = TimeoutError("sensitive-camera-password")
    kwargs = {"headers": bearer(await mint_test_token(owner))}
    if method == "POST":
        kwargs["json"] = {"data": {"ptz": {"pan": 0.5}}}
    response = await client.request(method, f"/v1/cams/{camera_id}/ptz", **kwargs)
    assert response.status_code == 409 and response.json()["code"] == ONVIF_CAMERA_TIMEOUT == "402"
    assert "sensitive-camera-password" not in response.text


async def test_onvif_deadline_emits_timeout_code(monkeypatch):
    monkeypatch.setattr(settings, "ONVIF_TIMEOUT_SECONDS", 0.001)

    async def slow_camera():
        await asyncio.sleep(1)

    with pytest.raises(onvif_client._CameraTimeout) as exc:
        await onvif_client._with_timeout(slow_camera(), 9)
    assert exc.value.code == ONVIF_CAMERA_TIMEOUT and exc.value.http_status == 409


@pytest.mark.parametrize("error,kind,code", [
    (TimeoutError("private transport details"), onvif_client._CameraTimeout, ONVIF_CAMERA_TIMEOUT),
    (zeep.exceptions.Fault("not authorized: private transport details"), onvif_client._AuthDenied,
     ONVIF_CAMERA_AUTH_REJECTED),
])
async def test_error_during_ptz_initialization_is_not_swallowed(fake, monkeypatch, error, kind, code):
    class Media:
        async def GetProfiles(self):
            return [type("Profile", (), {"PTZConfiguration": object(), "token": "t"})()]

    class Camera:
        def __init__(self, *args):
            pass

        async def update_xaddrs(self):
            pass

        async def create_media_service(self):
            return Media()

        async def create_ptz_service(self):
            raise error

    monkeypatch.setattr(onvif_client, "ONVIFCamera", Camera)
    with pytest.raises(kind) as exc:
        await onvif_client.get_status(FakeCam())
    assert exc.value.code == code
    assert "private transport details" not in str(exc.value)


async def test_invalidate_resets_backoff(fake):
    fake.error = zeep.exceptions.Fault(LOCKED)
    with pytest.raises(AppError):
        await onvif_client.get_status(FakeCam())
    onvif_client.invalidate(9)  # PATCH of control_password / onvif settings
    fake.error = None
    with pytest.raises(AppError) as ei:  # reached the camera again; no PTZ service -> 176 "not configured"
        await onvif_client.get_status(FakeCam(password="new"))
    assert fake.calls == 2 and "PTZ" in str(ei.value)


async def test_backoff_expires(fake, monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(onvif_client, "_now", lambda: t[0])
    monkeypatch.setattr(settings, "ONVIF_AUTH_BACKOFF_SECONDS", 1800)
    fake.error = zeep.exceptions.Fault(LOCKED)
    with pytest.raises(AppError):
        await onvif_client.get_status(FakeCam())
    t[0] += 1799
    with pytest.raises(AppError):
        await onvif_client.get_status(FakeCam())
    assert fake.calls == 1
    t[0] += 2
    fake.error = None
    with pytest.raises(AppError) as ei:
        await onvif_client.get_status(FakeCam())
    assert fake.calls == 2 and "PTZ" in str(ei.value)
    assert onvif_client._auth_denied == {}


async def test_success_path_untouched(fake):
    uri = await onvif_client.get_snapshot_uri(FakeCam())
    assert uri == "http://192.0.2.1/snap.jpg"
    assert onvif_client._auth_denied == {}
    with pytest.raises(AppError) as ei:
        await onvif_client.get_status(FakeCam())
    assert ei.value.code == PTZ_NOT_CONFIGURED and "PTZ" in str(ei.value)


def _patch_http(monkeypatch, statuses):
    seen = []

    class Resp:
        def __init__(self, code):
            self.status_code = code
            self.content = b"\xff\xd8jpg"

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("x", request=None, response=self)

    class Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            seen.append(url)
            return Resp(statuses[min(len(seen), len(statuses)) - 1])

    monkeypatch.setattr(onvif_client.httpx, "AsyncClient", Client)
    return seen


async def test_download_401_final_is_denied_and_remembered(fake, monkeypatch):
    seen = _patch_http(monkeypatch, [401])
    with pytest.raises(AppError) as ei:
        await onvif_client.get_snapshot(FakeCam())
    assert ei.value.code == ONVIF_CAMERA_AUTH_REJECTED and 9 in onvif_client._auth_denied
    with pytest.raises(AppError):
        await onvif_client.get_snapshot(FakeCam())
    assert len(seen) == 1


async def test_download_ok_not_denied(fake, monkeypatch):
    _patch_http(monkeypatch, [200])
    assert await onvif_client.get_snapshot(FakeCam()) == b"\xff\xd8jpg"
    assert onvif_client._auth_denied == {}


async def test_download_network_error_is_app_error(fake, monkeypatch):
    class Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            raise httpx.ConnectError("down")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(onvif_client.httpx, "AsyncClient", Client)
    with pytest.raises(AppError) as ei:
        await onvif_client.get_snapshot(FakeCam())
    assert ei.value.code == EXTERNAL_API_UNAVAILABLE


async def test_snapshot_falls_back_when_onvif_denied(fake, monkeypatch):
    fake.error = zeep.exceptions.Fault(LOCKED)
    monkeypatch.setattr(settings, "INGEST_ENABLED", False)
    rtsp = []

    async def grab(cam):
        rtsp.append(cam.id)
        return b"RTSP"

    monkeypatch.setattr(snapshot, "_grab_from_rtsp", grab)
    cam = FakeCam()
    assert await snapshot.get_live_snapshot(cam) == b"RTSP"
    assert await snapshot.get_live_snapshot(cam) == b"RTSP"
    assert rtsp == [9, 9]
    assert fake.calls == 1  # second snapshot did not touch the camera over ONVIF

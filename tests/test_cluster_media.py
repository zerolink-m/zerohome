"""Cluster fMP4 over loopback TCP. No ffmpeg, no non-loopback traffic."""
import asyncio
import socket
import time

import pytest
from pydantic import ValidationError
from sqlalchemy import update

from app.core.config import Settings, settings
from app.models.node import Assignment, Node
from app.services.cluster import media_server as media_server_mod
from app.services.cluster import remote_hub as remote_hub_mod
from app.services.cluster.media_client import MediaClient, MediaRefused
from app.services.cluster.media_protocol import (
    ERR_NOT_OWNER,
    ERR_UNAUTHORIZED,
    TYPE_ERROR,
    TYPE_FRAGMENT,
    TYPE_HELLO,
    TYPE_INIT,
    TYPE_PING,
    TYPE_PONG,
    TYPE_RESET,
    TYPE_STATE,
    TYPE_SUBSCRIBE,
    TYPE_UNSUBSCRIBE,
    decode_hello,
    encode_frame,
    encode_hello,
    encode_subscribe,
    read_frame,
    sign_nonce,
)
from app.services.cluster.media_server import MediaServer
from app.services.cluster.remote_hub import RemoteHub, set_bridge
from app.services.ingest.hub import IngestHub
from app.services.ingest.supervisor import IngestSupervisor
from tests.fmp4_builder import fragment, init_segment, seq_of
from tests.test_cluster_runner import Clock, Fake, make


@pytest.fixture(autouse=True)
async def _stop_global_media():
    yield
    await media_server_mod.stop()
    await remote_hub_mod.stop()


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


async def grant(sm, node_id, cams, address, generation=1, expires=4_000_000_000):
    async with sm() as db:
        if await db.get(Node, node_id) is None:
            db.add(Node(
                node_id=node_id, started_at=1, heartbeat_at=1,
                capabilities={"ingest": True}, address=address, version="test",
            ))
        else:
            node = await db.get(Node, node_id)
            node.address = address
        for cam_id in cams:
            db.add(Assignment(
                function="ingest", resource_id=cam_id, owner_node_id=node_id,
                generation=generation, expires_at=expires, updated_at=1,
            ))
        await db.commit()


def make_server(sm, hub, port, owned, *, secret="", buffer_bytes=8 * 1024 * 1024, node_id="owner",
                idle_timeout=None):
    gens = {cam_id: 1 for cam_id in owned}

    def generation_of(cam_id, _gens=gens):
        return _gens.get(cam_id)

    server = MediaServer(
        sessionmaker=sm, hub=hub, node_id=node_id, generation_of=generation_of,
        clock=lambda: 1_000_000, buffer_bytes=buffer_bytes, host="127.0.0.1",
        port=port, secret=secret, idle_timeout=idle_timeout,
    )
    return server, gens


async def open_raw(port, secret=""):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    msg_type, _channel, payload = await asyncio.wait_for(read_frame(reader), 2)
    assert msg_type == TYPE_HELLO
    _version, _node, nonce = decode_hello(payload)
    auth = sign_nonce(secret, nonce) if secret else b""
    assert (not secret and nonce == b"") or (secret and len(nonce) == 32)
    writer.write(encode_frame(TYPE_HELLO, 0, encode_hello("viewer", auth)))
    await writer.drain()
    return reader, writer


async def subscribe(writer, channel, cam_id):
    writer.write(encode_frame(TYPE_SUBSCRIBE, channel, encode_subscribe(cam_id)))
    await writer.drain()


async def media_frames(reader, n, timeout=2.0):
    """n frames that carry media or an error, skipping STATE."""
    out = []
    deadline = time.monotonic() + timeout
    while len(out) < n:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError(f"got {len(out)} of {n} media frames")
        msg_type, channel, payload = await asyncio.wait_for(read_frame(reader), left)
        if msg_type in (TYPE_STATE, TYPE_PING, TYPE_PONG):
            continue
        out.append((msg_type, channel, payload))
    return out


async def test_subscribe_gets_init_then_fragments_in_order(sessionmaker_):
    port = free_port()
    hub = IngestHub()
    server, _gens = make_server(sessionmaker_, hub, port, [1])
    await server.start()
    try:
        await grant(sessionmaker_, "owner", [1], f"127.0.0.1:{port}")
        reader, writer = await open_raw(port)
        await subscribe(writer, 1, 1)
        for _ in range(100):
            if hub.subscriber_count(1) == 1:
                break
            await asyncio.sleep(0.02)
        assert hub.subscriber_count(1) == 1
        assert hub.get(1).has_viewers.is_set()
        hub.get(1).feed(init_segment())
        for i in range(4):
            hub.get(1).feed(fragment(i, key=(i % 2 == 0)))
            await asyncio.sleep(0)
        got = await media_frames(reader, 5)
        assert [(t, c) for t, c, _p in got] == [(TYPE_INIT, 1)] + [(TYPE_FRAGMENT, 1)] * 4
        assert got[0][2] == init_segment()
        assert [seq_of(p) for _t, _c, p in got[1:]] == [0, 1, 2, 3]
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


async def test_several_cameras_share_one_connection(sessionmaker_):
    port = free_port()
    hub = IngestHub()
    server, _gens = make_server(sessionmaker_, hub, port, [1, 2])
    await server.start()
    try:
        await grant(sessionmaker_, "owner", [1, 2], f"127.0.0.1:{port}")
        reader, writer = await open_raw(port)
        await subscribe(writer, 1, 1)
        await subscribe(writer, 2, 2)
        for _ in range(100):
            if hub.subscriber_count(1) == 1 and hub.subscriber_count(2) == 1:
                break
            await asyncio.sleep(0.02)
        assert server.connection_count == 1
        assert server.channel_count == 2
        hub.get(1).feed(init_segment())
        hub.get(2).feed(init_segment(extra=8))
        hub.get(1).feed(fragment(10))
        hub.get(2).feed(fragment(20))
        got = {}
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and (got.get(1) != [None, 10] or got.get(2) != [None, 20]):
            msg_type, channel, payload = await asyncio.wait_for(read_frame(reader), 1)
            if msg_type in (TYPE_STATE, TYPE_PING, TYPE_PONG):
                continue
            got.setdefault(channel, [])
            if msg_type == TYPE_INIT:
                got[channel].append(None)
            elif msg_type == TYPE_FRAGMENT:
                got[channel].append(seq_of(payload))
        assert got[1] == [None, 10]
        assert got[2] == [None, 20]
        assert server.connection_count == 1
        writer.close()
    finally:
        await server.stop()


async def test_slow_client_is_reset_and_does_not_block_feed(sessionmaker_):
    port = free_port()
    limit = 200_000
    hub = IngestHub(max_buffer_bytes=8_000_000, subscriber_limit_bytes=8_000_000)
    server, _gens = make_server(sessionmaker_, hub, port, [1], buffer_bytes=limit)
    await server.start()
    try:
        await grant(sessionmaker_, "owner", [1], f"127.0.0.1:{port}")
        reader, writer = await open_raw(port)
        await subscribe(writer, 1, 1)
        for _ in range(100):
            if hub.subscriber_count(1) == 1:
                break
            await asyncio.sleep(0.02)
        hub.get(1).feed(init_segment())
        await asyncio.sleep(0.05)
        server.output_open.clear()  # socket stops draining; the app buffer is on its own
        await asyncio.sleep(0.05)
        started = time.monotonic()
        for i in range(12):
            hub.get(1).feed(fragment(i, size=90_000))
            await asyncio.sleep(0)
        elapsed = time.monotonic() - started
        await asyncio.sleep(0.05)
        assert elapsed < 1.5
        # The queue filled up to the cap and then stopped growing (12 fragments would be ~1 MB).
        assert limit // 2 <= server.peak_buffered <= limit
        assert server.buffered_bytes() <= limit
        server.output_open.set()
        deadline = time.monotonic() + 3
        kinds = []
        while time.monotonic() < deadline and TYPE_RESET not in kinds:
            msg_type, _channel, _payload = await asyncio.wait_for(read_frame(reader), 1)
            kinds.append(msg_type)
        assert TYPE_RESET in kinds
        writer.close()
    finally:
        server.output_open.set()
        await server.stop()


async def test_unsubscribe_stops_that_camera(sessionmaker_):
    port = free_port()
    hub = IngestHub()
    server, _gens = make_server(sessionmaker_, hub, port, [1])
    await server.start()
    try:
        await grant(sessionmaker_, "owner", [1], f"127.0.0.1:{port}")
        reader, writer = await open_raw(port)
        await subscribe(writer, 1, 1)
        hub.get(1).feed(init_segment())
        hub.get(1).feed(fragment(1))
        first = await media_frames(reader, 2)
        assert first[0][0] == TYPE_INIT and seq_of(first[1][2]) == 1
        writer.write(encode_frame(TYPE_UNSUBSCRIBE, 1, b""))
        await writer.drain()
        for _ in range(100):
            if hub.subscriber_count(1) == 0:
                break
            await asyncio.sleep(0.02)
        assert hub.subscriber_count(1) == 0
        assert not hub.get(1).has_viewers.is_set()
        hub.get(1).feed(fragment(99))
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            try:
                msg_type, _channel, payload = await asyncio.wait_for(read_frame(reader), 0.1)
            except TimeoutError:
                break
            if msg_type == TYPE_FRAGMENT:
                assert seq_of(payload) != 99
        writer.close()
    finally:
        await server.stop()


async def test_not_owner_sends_error_and_no_media(sessionmaker_):
    port = free_port()
    hub = IngestHub()
    # generation_of is 1 only for cam 1. Cam 1's row is a different node.
    # Cam 2 has no row. Cam 3 is ours but a newer generation than we run.
    server, _gens = make_server(sessionmaker_, hub, port, [1, 3])
    await server.start()
    try:
        await grant(sessionmaker_, "other", [1], f"127.0.0.1:{port}")
        await grant(sessionmaker_, "owner", [3], f"127.0.0.1:{port}", generation=9)
        for cam_id in (1, 2, 3):
            hub.get(cam_id).feed(init_segment())
            hub.get(cam_id).feed(fragment(cam_id))
        reader, writer = await open_raw(port)
        for channel, cam_id in ((1, 1), (2, 2), (3, 3)):
            await subscribe(writer, channel, cam_id)
            msg_type, channel_id, payload = (await media_frames(reader, 1))[0]
            assert (msg_type, channel_id, payload) == (TYPE_ERROR, channel, ERR_NOT_OWNER)
        await asyncio.sleep(0.2)
        assert hub.subscriber_count(1) == 0
        assert hub.subscriber_count(2) == 0
        assert hub.subscriber_count(3) == 0
        writer.close()
    finally:
        await server.stop()


async def test_empty_secret_loopback_only(sessionmaker_, monkeypatch):
    port = free_port()
    hub = IngestHub()
    server, _gens = make_server(sessionmaker_, hub, port, [1], secret="")
    await server.start()
    try:
        await grant(sessionmaker_, "owner", [1], f"127.0.0.1:{port}")
        reader, writer = await open_raw(port)
        await subscribe(writer, 1, 1)
        hub.get(1).feed(init_segment())
        hub.get(1).feed(fragment(0))  # moof closes the init segment
        assert (await media_frames(reader, 1))[0][0] == TYPE_INIT
        writer.close()
        await writer.wait_closed()
        for _ in range(50):
            if server.connection_count == 0:
                break
            await asyncio.sleep(0.02)

        client = MediaClient(
            node_id="puller", secret="", default_port=port,
            on_init=lambda *_a: None, on_fragment=lambda *_a: None, on_reset=lambda *_a: None,
            on_state=lambda *_a: None, on_error=lambda *_a: None,
        )
        with pytest.raises(MediaRefused, match="CLUSTER_SECRET"):
            await client.subscribe(1, "owner", "203.0.113.9")
        await client.close()

        real = asyncio.StreamWriter.get_extra_info

        def fake(self, name, default=None):
            if name == "peername":
                return ("192.0.2.9", 40000)
            return real(self, name, default)

        monkeypatch.setattr(asyncio.StreamWriter, "get_extra_info", fake)
        _reader, outsider = await asyncio.open_connection("127.0.0.1", port)
        with pytest.raises((ConnectionError, OSError, asyncio.IncompleteReadError)):
            await asyncio.wait_for(read_frame(_reader), 1)
        assert server.connection_count == 0
        outsider.close()
    finally:
        await server.stop()


async def test_secret_rejects_bad_hmac_and_accepts_good(sessionmaker_):
    port = free_port()
    hub = IngestHub()
    server, _gens = make_server(sessionmaker_, hub, port, [1], secret="test-secret")
    await server.start()
    try:
        await grant(sessionmaker_, "owner", [1], f"127.0.0.1:{port}")
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        msg_type, _channel, payload = await asyncio.wait_for(read_frame(reader), 2)
        assert msg_type == TYPE_HELLO
        _version, _node, nonce = decode_hello(payload)
        writer.write(encode_frame(TYPE_HELLO, 0, encode_hello("viewer", b"\x00" * 32)))
        await writer.drain()
        try:
            msg_type, _channel, body = await asyncio.wait_for(read_frame(reader), 1)
            assert (msg_type, body) == (TYPE_ERROR, ERR_UNAUTHORIZED)
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            pass
        writer.close()
        assert nonce and not auth_matches_zero(nonce)

        hub.get(1).feed(init_segment())
        hub.get(1).feed(fragment(0))
        reader, writer = await open_raw(port, secret="test-secret")
        await subscribe(writer, 1, 1)
        assert (await media_frames(reader, 1))[0][0] == TYPE_INIT
        writer.close()
    finally:
        await server.stop()


def auth_matches_zero(nonce: bytes) -> bool:
    return sign_nonce("test-secret", nonce) == b"\x00" * 32


async def test_client_reconnects_and_receives_init_again(sessionmaker_):
    port = free_port()
    hub = IngestHub()
    server, _gens = make_server(sessionmaker_, hub, port, [1])
    await server.start()
    inits = []
    client = MediaClient(
        node_id="puller", secret="", default_port=port, reconnect_min=0.05, reconnect_max=0.2,
        on_init=lambda _cam, data: inits.append(data),
        on_fragment=lambda *_a: None, on_reset=lambda *_a: None,
        on_state=lambda *_a: None, on_error=lambda *_a: None,
    )
    try:
        await grant(sessionmaker_, "owner", [1], f"127.0.0.1:{port}")
        hub.get(1).feed(init_segment())
        hub.get(1).feed(fragment(0))
        await client.subscribe(1, "owner", f"127.0.0.1:{port}")
        for _ in range(100):
            if inits:
                break
            await asyncio.sleep(0.02)
        assert inits == [init_segment()]
        await server.disconnect_all()
        for _ in range(150):
            if len(inits) >= 2:
                break
            await asyncio.sleep(0.02)
        assert len(inits) >= 2
        assert all(blob == init_segment() for blob in inits)
        assert client.connection_count == 1
    finally:
        await client.close()
        await server.stop()


async def test_oversize_frame_closes_the_connection(sessionmaker_):
    port = free_port()
    hub = IngestHub()
    server, _gens = make_server(sessionmaker_, hub, port, [1])
    await server.start()
    try:
        reader, writer = await open_raw(port)
        writer.write((9 * 1024 * 1024).to_bytes(4, "big"))
        await writer.drain()
        with pytest.raises((ConnectionError, OSError, asyncio.IncompleteReadError)):
            await asyncio.wait_for(read_frame(reader), 1)
        writer.close()
        for _ in range(50):
            if server.connection_count == 0:
                break
            await asyncio.sleep(0.02)
        _reader2, writer2 = await open_raw(port)
        writer2.close()
        await writer2.wait_closed()
    finally:
        await server.stop()


async def test_disabled_cluster_does_not_listen(monkeypatch):
    port = free_port()
    monkeypatch.setattr(settings, "CLUSTER_ENABLED", False)
    monkeypatch.setattr(settings, "CLUSTER_MEDIA_PORT", port)
    await media_server_mod.start()
    assert media_server_mod.get_server() is None
    with pytest.raises(OSError):
        await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 0.5)


def test_settings_reject_plaintext_off_loopback():
    with pytest.raises(ValidationError, match="CLUSTER_SECRET"):
        Settings(_env_file=None, CLUSTER_ENABLED=True, NODE_ID="n1", CLUSTER_MEDIA_HOST="0.0.0.0")
    ok = Settings(
        _env_file=None, CLUSTER_ENABLED=True, NODE_ID="n1",
        CLUSTER_MEDIA_HOST="0.0.0.0", CLUSTER_SECRET="s",
    )
    assert ok.CLUSTER_MEDIA_PORT == 4700
    assert Settings(_env_file=None, CLUSTER_ENABLED=True, NODE_ID="n1").CLUSTER_MEDIA_HOST == "127.0.0.1"


async def test_feed_remote_fans_out_like_a_local_feed():
    hub = IngestHub()
    sub = hub.subscribe(1)
    hub.get(1).feed_remote(init=init_segment())
    hub.get(1).feed_remote(fragment=fragment(3, key=True))
    hub.get(1).feed_remote(fragment=fragment(4, key=False))
    got = [await asyncio.wait_for(sub.__anext__(), 1) for _ in range(3)]
    assert got[0] == init_segment()
    assert [seq_of(part) for part in got[1:]] == [3, 4]
    assert hub.get(1).state == "online"
    late = hub.subscribe(1)
    replay = [await asyncio.wait_for(late.__anext__(), 1) for _ in range(3)]
    assert replay[0] == init_segment() and seq_of(replay[-1]) == 4


async def test_remote_pull_follows_viewers_and_grace(sessionmaker_):
    port = free_port()
    owner = IngestHub()
    local = IngestHub()
    server, gens = make_server(sessionmaker_, owner, port, [1, 2])
    await server.start()
    bridge = RemoteHub(
        node_id="puller", sessionmaker=sessionmaker_, hub=local, holds=lambda _cam: False,
        grace=0.3, watch_interval=0.1, reconnect_min=0.05, reconnect_max=0.2,
    )
    set_bridge(bridge)
    try:
        await grant(sessionmaker_, "owner", [1, 2], f"127.0.0.1:{port}")
        await bridge.start()
        owner.get(1).feed(init_segment())
        owner.get(1).feed(fragment(1))
        owner.get(2).feed(init_segment(extra=4))
        owner.get(2).feed(fragment(2))
        sub1 = local.subscribe(1)
        sub2 = local.subscribe(2)
        async def take(sub, n):
            return [await asyncio.wait_for(sub.__anext__(), 2) for _ in range(n)]
        got1, got2 = await asyncio.gather(take(sub1, 2), take(sub2, 2))
        assert got1[0] == init_segment() and seq_of(got1[1]) == 1
        assert seq_of(got2[1]) == 2
        assert server.connection_count == 1
        assert bridge._client.connection_count == 1
        assert owner.subscriber_count(1) == 1 and owner.subscriber_count(2) == 1
        sub1.close()
        sub2.close()
        for _ in range(80):
            if owner.subscriber_count(1) == 0 and owner.subscriber_count(2) == 0:
                break
            await asyncio.sleep(0.05)
        assert owner.subscriber_count(1) == 0 and owner.subscriber_count(2) == 0
        owner.get(1).feed(fragment(99))
        await asyncio.sleep(0.2)
        assert all(seq_of(part) != 99 for part in local.get(1).gop)
        # generation change resets the local hub and subscribes again
        sub1 = local.subscribe(1)
        for _ in range(50):
            if owner.subscriber_count(1) == 1:
                break
            await asyncio.sleep(0.02)
        gens[1] = 2
        async with sessionmaker_() as db:
            await db.execute(update(Assignment).where(
                Assignment.function == "ingest", Assignment.resource_id == 1,
            ).values(generation=2))
            await db.commit()
        for _ in range(50):
            if sub1.closed:
                break
            await asyncio.sleep(0.05)
        assert sub1.closed and sub1.close_reason == "generation"
        again = local.subscribe(1)
        fresh = await asyncio.wait_for(again.__anext__(), 2)
        assert fresh == init_segment()
        again.close()
    finally:
        await bridge.stop()
        set_bridge(None)
        await server.stop()


async def test_is_running_demands_remote_only_while_the_bridge_runs(sessionmaker_):
    supervisor = IngestSupervisor()
    assert supervisor.is_running(424242) is False
    bridge = RemoteHub(
        node_id="puller", sessionmaker=sessionmaker_, hub=IngestHub(),
        holds=lambda _cam: False, grace=2, watch_interval=5,
    )
    set_bridge(bridge)
    await bridge.start()
    try:
        assert supervisor.is_running(424242) is True
        bridge._holds = lambda _cam: True
        assert supervisor.is_running(424242) is False
    finally:
        await bridge.stop()
        set_bridge(None)
    assert supervisor.is_running(424242) is False


async def test_runner_stops_remote_on_acquire_and_starts_it_on_loss(sessionmaker_):
    class Media:
        def __init__(self):
            self.events = []
            self.viewers = {1}

        def has_local_viewers(self, cam_id):
            return cam_id in self.viewers

        async def on_acquired(self, cam_id):
            self.events.append(("acq", cam_id))

        async def on_lost(self, cam_id):
            self.events.append(("lost", cam_id))

    media = Media()
    clock = Clock()
    fake = Fake("ingest", [1])
    runner = make("a", sessionmaker_, clock, [fake])
    runner.attach_media(media)
    await runner._heartbeat(register=True)
    await runner.reconcile_once()
    await runner.drain()
    assert ("acq", 1) in media.events
    async with sessionmaker_() as db:
        await db.execute(update(Assignment).where(Assignment.resource_id == 1).values(
            owner_node_id="x", generation=Assignment.generation + 1, expires_at=clock.t + 100,
        ))
        await db.commit()
    clock.t += 10
    await runner.reconcile_once()
    await runner.drain()
    assert ("lost", 1) in media.events
    assert fake.running == set()


def _count_subscribes(monkeypatch):
    from app.services.cluster.media_server import _Conn

    calls = {"n": 0}
    original = _Conn._on_subscribe

    async def wrapped(self, channel, payload):
        calls["n"] += 1
        return await original(self, channel, payload)

    monkeypatch.setattr(_Conn, "_on_subscribe", wrapped)
    return calls


async def test_overflow_catchup_does_not_open_a_second_subscription(sessionmaker_, monkeypatch):
    from app.services.ingest.hub import CamHub

    port = free_port()
    limit = 200_000
    hub = IngestHub(max_buffer_bytes=8_000_000, subscriber_limit_bytes=8_000_000)
    opened = []
    real_subscribe = CamHub.subscribe

    def counting(self, replay=True):
        sub = real_subscribe(self, replay=replay)
        opened.append(sub)
        return sub

    monkeypatch.setattr(CamHub, "subscribe", counting)
    subscribes = _count_subscribes(monkeypatch)
    server, _gens = make_server(sessionmaker_, hub, port, [1], buffer_bytes=limit)
    await server.start()
    resets = []
    inits = []
    client = MediaClient(
        node_id="puller", secret="", default_port=port, reconnect_min=0.05, reconnect_max=0.2,
        on_init=lambda _cam, data: inits.append(data),
        on_fragment=lambda *_a: None,
        on_reset=lambda _cam, reason: resets.append(reason),
        on_state=lambda *_a: None, on_error=lambda *_a: None,
    )
    try:
        await grant(sessionmaker_, "owner", [1], f"127.0.0.1:{port}")
        hub.get(1).feed(init_segment())
        hub.get(1).feed(fragment(0, key=True))
        await client.subscribe(1, "owner", f"127.0.0.1:{port}")
        for _ in range(100):
            if inits and subscribes["n"] == 1 and len(opened) == 1:
                break
            await asyncio.sleep(0.02)
        assert subscribes["n"] == 1 and len(opened) == 1
        server.output_open.clear()
        await asyncio.sleep(0.05)
        for i in range(12):
            hub.get(1).feed(fragment(i + 1, key=True, size=90_000))
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        server.output_open.set()
        for _ in range(100):
            if "overflow" in resets and len(inits) >= 2:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.3)  # a mistaken SUBSCRIBE would have been processed by now
        assert "overflow" in resets
        assert subscribes["n"] == 1  # client did not subscribe again
        # Each overflow catch-up replaces the previous subscription; it must not
        # leave a second one open beside it.
        assert len(opened) >= 2
        assert sum(not sub.closed for sub in opened) == 1
    finally:
        server.output_open.set()
        await client.close()
        await server.stop()


async def test_restart_reset_still_subscribes_again(sessionmaker_, monkeypatch):
    port = free_port()
    hub = IngestHub()
    subscribes = _count_subscribes(monkeypatch)
    server, _gens = make_server(sessionmaker_, hub, port, [1])
    await server.start()
    resets = []
    inits = []
    client = MediaClient(
        node_id="puller", secret="", default_port=port, reconnect_min=0.05, reconnect_max=0.2,
        on_init=lambda _cam, data: inits.append(data),
        on_fragment=lambda *_a: None,
        on_reset=lambda _cam, reason: resets.append(reason),
        on_state=lambda *_a: None, on_error=lambda *_a: None,
    )
    try:
        await grant(sessionmaker_, "owner", [1], f"127.0.0.1:{port}")
        hub.get(1).feed(init_segment())
        hub.get(1).feed(fragment(0, key=True))
        await client.subscribe(1, "owner", f"127.0.0.1:{port}")
        for _ in range(100):
            if inits and subscribes["n"] == 1:
                break
            await asyncio.sleep(0.02)
        assert subscribes["n"] == 1
        hub.get(1).reset_stream("restart")
        hub.get(1).feed(init_segment())
        hub.get(1).feed(fragment(1, key=True))
        for _ in range(100):
            if "restart" in resets and subscribes["n"] >= 2 and len(inits) >= 2:
                break
            await asyncio.sleep(0.02)
        assert "restart" in resets
        assert subscribes["n"] >= 2
        assert len(inits) >= 2
        assert hub.subscriber_count(1) == 1
    finally:
        await client.close()
        await server.stop()


async def test_silent_connection_is_closed_and_drops_the_hub_subscription(sessionmaker_):
    port = free_port()
    hub = IngestHub()
    server, _gens = make_server(sessionmaker_, hub, port, [1], idle_timeout=0.4)
    await server.start()
    try:
        await grant(sessionmaker_, "owner", [1], f"127.0.0.1:{port}")
        reader, writer = await open_raw(port)
        await subscribe(writer, 1, 1)
        for _ in range(50):
            if hub.subscriber_count(1) == 1:
                break
            await asyncio.sleep(0.02)
        assert hub.subscriber_count(1) == 1 and hub.get(1).has_viewers.is_set()
        # No further frames from this peer. Pings the server sends do not reset its own timer.
        await asyncio.sleep(0.9)
        assert hub.subscriber_count(1) == 0
        assert not hub.get(1).has_viewers.is_set()
        assert server.connection_count == 0
        writer.close()
    finally:
        await server.stop()


async def test_pings_keep_a_live_connection_and_a_silent_server_is_redialed(sessionmaker_):
    port = free_port()
    hub = IngestHub()
    server, _gens = make_server(sessionmaker_, hub, port, [1], idle_timeout=0.4)
    await server.start()
    client = MediaClient(
        node_id="puller", secret="", default_port=port, idle_timeout=0.4,
        reconnect_min=0.05, reconnect_max=0.2,
        on_init=lambda *_a: None, on_fragment=lambda *_a: None, on_reset=lambda *_a: None,
        on_state=lambda *_a: None, on_error=lambda *_a: None,
    )
    try:
        await grant(sessionmaker_, "owner", [1], f"127.0.0.1:{port}")
        hub.get(1).feed(init_segment())
        hub.get(1).feed(fragment(0, key=True))
        await client.subscribe(1, "owner", f"127.0.0.1:{port}")
        for _ in range(50):
            if hub.subscriber_count(1) == 1:
                break
            await asyncio.sleep(0.02)
        assert hub.subscriber_count(1) == 1
        await asyncio.sleep(1.2)  # several idle periods; PINGs must keep both sides up
        assert hub.subscriber_count(1) == 1
        assert server.connection_count == 1
        assert client.connection_count == 1
    finally:
        await client.close()
        await server.stop()

    silent_port = free_port()
    accepted = []

    async def silent(reader, writer):
        accepted.append(1)
        writer.write(encode_frame(TYPE_HELLO, 0, encode_hello("owner", b"")))
        await writer.drain()
        try:
            await asyncio.wait_for(read_frame(reader), 1)
        except Exception:
            pass
        try:
            await asyncio.sleep(3)
        finally:
            writer.close()

    fake = await asyncio.start_server(silent, "127.0.0.1", silent_port)
    quiet = MediaClient(
        node_id="puller", secret="", default_port=silent_port, idle_timeout=0.3,
        reconnect_min=0.05, reconnect_max=0.1,
        on_init=lambda *_a: None, on_fragment=lambda *_a: None, on_reset=lambda *_a: None,
        on_state=lambda *_a: None, on_error=lambda *_a: None,
    )
    try:
        await quiet.subscribe(1, "silent", f"127.0.0.1:{silent_port}")
        for _ in range(40):
            if len(accepted) >= 2:
                break
            await asyncio.sleep(0.05)
        assert len(accepted) >= 2  # idle timeout closed the first connection; the client redialed
    finally:
        await quiet.close()
        fake.close()
        await fake.wait_closed()

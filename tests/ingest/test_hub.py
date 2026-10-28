import asyncio
import time

from app.services.fmp4 import Fmp4Splitter, fragment_starts_with_non_sync
from app.services.ingest.hub import STATE_ONLINE, IngestHub
from tests.fmp4_builder import fragment, init_segment, seq_of


async def take(sub, n, timeout=1.0):
    out = []
    for _ in range(n):
        out.append(await asyncio.wait_for(sub.__anext__(), timeout))
    return out


def test_fragment_keyframe_detection():
    assert not fragment_starts_with_non_sync(fragment(0, key=True))
    assert fragment_starts_with_non_sync(fragment(1, key=False))
    assert not fragment_starts_with_non_sync(b"garbage")


def test_splitter_reassembles_arbitrary_chunks():
    data = init_segment() + b"".join(fragment(i, size=300) for i in range(4))
    sp = Fmp4Splitter()
    frags = []
    for i in range(0, len(data), 7):
        frags += sp.feed(data[i:i + 7])
    assert sp.init_segment == init_segment()
    assert [seq_of(f) for f in frags] == [0, 1, 2, 3]


async def test_new_subscriber_gets_init_and_last_gop_immediately():
    hub = IngestHub(max_buffer_bytes=100_000)
    cam = hub.get(1)
    cam.feed(init_segment())
    for i, key in enumerate([True, False, False, True, False]):
        cam.feed(fragment(i, key=key))
    assert cam.state == STATE_ONLINE
    sub = hub.subscribe(1)
    got = await take(sub, 3)  # no waiting: already queued
    assert got[0] == init_segment()
    assert [seq_of(f) for f in got[1:]] == [3, 4]
    cam.feed(fragment(5, key=False))
    assert seq_of((await take(sub, 1))[0]) == 5


async def test_no_subscribers_only_updates_buffer():
    hub = IngestHub(max_buffer_bytes=100_000)
    cam = hub.get(1)
    cam.feed(init_segment())
    for i in range(10):
        cam.feed(fragment(i, key=(i % 5 == 0)))
    assert cam.subscriber_count == 0
    assert [seq_of(f) for f in cam.gop] == [5, 6, 7, 8, 9]
    assert cam.bytes_in > 0 and cam.last_data_at is not None


async def test_subscribe_before_init_waits_for_it():
    hub = IngestHub()
    sub = hub.subscribe(1)
    task = asyncio.create_task(take(sub, 2))
    await asyncio.sleep(0.05)
    assert not task.done()
    hub.get(1).feed(init_segment() + fragment(0))
    got = await asyncio.wait_for(task, 1)
    assert got[0] == init_segment() and seq_of(got[1]) == 0


async def test_slow_subscriber_does_not_block_and_is_cut_off():
    hub = IngestHub(max_buffer_bytes=10_000, subscriber_limit_bytes=5_000)
    cam = hub.get(1)
    cam.feed(init_segment())
    slow = hub.subscribe(1)  # never reads
    fast = hub.subscribe(1)
    received = []

    async def reader():
        async for chunk in fast:
            received.append(chunk)
            if len(received) >= 41:
                return

    task = asyncio.create_task(reader())
    t0 = time.monotonic()
    for i in range(40):
        cam.feed(fragment(i, size=500))
        await asyncio.sleep(0)
    assert time.monotonic() - t0 < 1.0
    await asyncio.wait_for(task, 2)
    assert slow.overflowed and slow.closed and slow.close_reason == "overflow"
    assert cam.subscriber_count == 1  # only the fast one is left
    assert len(received) == 41  # init + 40, none lost for the healthy subscriber
    fast.close()
    # the slow subscriber's iterator simply ends
    assert [c async for c in slow] == []


async def test_buffer_limit_in_bytes():
    hub = IngestHub(max_buffer_bytes=3_000)
    cam = hub.get(1)
    cam.feed(init_segment())
    cam.feed(fragment(0, key=True, size=1000))
    for i in range(1, 10):  # one long GOP of non-key fragments
        cam.feed(fragment(i, key=False, size=1000))
    assert cam.gop_bytes <= 3_000 + 200
    assert seq_of(cam.gop[-1]) == 9  # newest always kept
    # one fragment bigger than the cap is still kept alone
    cam.feed(fragment(10, key=True, size=9_000))
    assert len(cam.gop) == 1


async def test_viewer_events_counter_and_listener():
    hub = IngestHub()
    seen = []
    hub.add_viewers_listener(lambda cam_id, n: seen.append((cam_id, n)))
    cam = hub.get(7)
    assert cam.no_viewers.is_set() and not cam.has_viewers.is_set()
    a, b = hub.subscribe(7), hub.subscribe(7)
    assert hub.subscriber_count(7) == 2
    assert cam.has_viewers.is_set() and not cam.no_viewers.is_set()
    a.close()
    assert hub.subscriber_count(7) == 1
    async with b:
        pass
    assert cam.no_viewers.is_set() and not cam.has_viewers.is_set()
    assert seen == [(7, 1), (7, 2), (7, 1), (7, 0)]


async def test_reset_stream_ends_subscribers_and_clears_buffer():
    hub = IngestHub()
    cam = hub.get(1)
    cam.feed(init_segment() + fragment(0))
    sub = hub.subscribe(1)
    cam.reset_stream("restart")
    assert [c async for c in sub][0] == init_segment()  # what was queued, then the end
    assert sub.close_reason == "restart"
    assert cam.init_segment is None and cam.gop == []
    assert cam.state == "offline"
    assert hub.subscriber_count(1) == 0


def test_status_fields():
    hub = IngestHub()
    st = hub.get(1).status()
    for key in ("state", "subscribers", "last_data_age", "restarts", "last_error", "bytes_in"):
        assert key in st

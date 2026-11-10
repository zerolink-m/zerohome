"""WebRTC audio: has_audio probe, offer SDP, 20 ms framing, shared clock with the video,
failure isolation and process cleanup. Fake ffmpeg (tests/fake_ffmpeg_webrtc.py); one test
with the real ffmpeg at the end (skipped without it)."""
import asyncio
import logging
import time
from types import SimpleNamespace

import pytest
from aiortc.mediastreams import MediaStreamError

from app.core.config import settings
from app.services.ingest.hub import IngestHub
from app.services.webrtc_manager import audio as audio_mod
from app.services.webrtc_manager import probe as probe_mod
from app.services.webrtc_manager import session as session_mod
from app.services.webrtc_manager.audio import FRAME_BYTES, AudioPipe, RTSPAudioStreamTrack
from app.services.webrtc_manager.track import RTSPVideoStreamTrack
from tests.fmp4_builder import fragment, init_segment
from tests.ingest.conftest import alive, wait_until
from tests.ingest.test_webrtc_decoder import env, make_track, pids, runs  # noqa: F401  (fixture)

AUDIO = (b"vide", b"soun")


def feed(env, audio=True, cam_id=1):
    init = init_segment(tracks=AUDIO if audio else (b"vide",))
    env.hub.get(cam_id).feed(init + fragment(0) + fragment(1, key=False))


def audio_runs(env):
    return [a for a in runs(env.args_log) if "s16le" in a]


def pcm_value(frame) -> int:
    return int.from_bytes(bytes(frame.planes[0])[:2], "little", signed=True)


# ------------------------------------------------------------------ probe

def test_has_audio_reads_the_init_segment():
    assert probe_mod.has_audio(init_segment(tracks=AUDIO)) is True
    assert probe_mod.has_audio(init_segment(tracks=(b"soun",))) is True
    assert probe_mod.has_audio(init_segment(tracks=(b"vide",))) is False
    assert probe_mod.has_audio(init_segment()) is False
    assert probe_mod.has_audio(b"") is False
    assert probe_mod.has_audio(b"\x00\x00\x00\x10moov\xff\xff") is False  # broken boxes: no crash


# ------------------------------------------------------------ SDP / session

async def offer_for(env, monkeypatch):
    monkeypatch.setattr(session_mod, "_build_rtsp_url", lambda cam: "rtsp://u:p@127.0.0.1/x")
    await session_mod.create_session(SimpleNamespace(id=1), "s1")
    sess = session_mod._sessions["s1"]
    sdp = await session_mod.create_offer("s1")
    return sess, sdp


@pytest.mark.parametrize("has_audio,enabled,expect", [
    (True, True, True), (False, True, False), (True, False, False)])
async def test_audio_transceiver_only_when_stream_has_audio(env, monkeypatch, has_audio, enabled, expect):
    monkeypatch.setattr(settings, "WEBRTC_AUDIO_ENABLED", enabled)
    feed(env, audio=has_audio)
    try:
        sess, sdp = await offer_for(env, monkeypatch)
        assert ("m=audio" in sdp) is expect and "m=video" in sdp
        assert (sess.audio_track is not None) is expect
        assert sess.video_track.want_audio is expect
        if expect:
            assert "opus" in sdp.lower()
    finally:
        await session_mod.close_session("s1")


async def test_no_hub_no_audio(env, monkeypatch):
    # hub never comes online: the direct path is video only, and the offer says so
    env.running[1] = False
    try:
        sess, sdp = await offer_for(env, monkeypatch)
        assert "m=audio" not in sdp and sess.audio_track is None
    finally:
        await session_mod.close_session("s1")


# ------------------------------------------------------------ framing / clock

async def test_pcm_is_cut_into_20ms_frames_with_monotonic_pts(env):
    feed(env)
    video, ended = make_track(audio=True)
    atrack = RTSPAudioStreamTrack(video)
    try:
        await asyncio.wait_for(video.recv(), 5)
        frames = [await asyncio.wait_for(atrack.recv(), 5) for _ in range(30)]
        assert video.audio is not None and atrack.kind == "audio"
        for f in frames:
            assert (f.samples, f.sample_rate, f.layout.name, f.format.name) == (960, 48000, "mono", "s16")
            assert f.time_base.numerator == 1 and f.time_base.denominator == 48000
        pts = [f.pts for f in frames]
        assert all(b - a >= 960 for a, b in zip(pts, pts[1:])), pts
        assert pts[1] - pts[0] == 960 and (pts[-1] - pts[0]) % 960 == 0
        vals = [pcm_value(f) for f in frames]
        assert vals == sorted(vals) and vals[0] == 1 and len(set(vals)) == len(vals)  # in order, from the head
        assert ended == []
    finally:
        await video.aclose()


async def test_audio_and_video_share_one_timeline(env):
    feed(env)
    video, _ = make_track(audio=True)
    atrack = RTSPAudioStreamTrack(video)
    try:
        v, a = await asyncio.gather(asyncio.wait_for(video.recv(), 5), asyncio.wait_for(atrack.recv(), 5))
        assert video.clock.t0 is not None
        v_sec, a_sec = v.pts / 90000, a.pts / 48000
        assert abs(v_sec - a_sec) < 0.3, (v_sec, a_sec)
        t0 = video.clock.t0
        for _ in range(5):
            v, a = await asyncio.gather(video.recv(), atrack.recv())
        assert video.clock.t0 == t0  # the origin is never moved
        assert abs(v.pts / 90000 - a.pts / 48000) < 0.5
        # paced by the wall clock: both lines advance with real time
        elapsed = time.monotonic() - t0
        assert abs(a.pts / 48000 - elapsed) < 0.5
    finally:
        await video.aclose()


def test_stale_audio_is_dropped_whole_frames_oldest_first():
    pipe = AudioPipe(1)
    pipe.buf = bytearray(b"".join(bytes([i]) * FRAME_BYTES for i in range(100)))  # 2 s
    keep = int(0.5 * audio_mod.BYTES_PER_SECOND)  # 25 frames
    first = pipe.take(keep)
    assert first is not None and first[0] == 100 - 25  # 75 oldest dropped, then one taken
    assert len(pipe.buf) == 24 * FRAME_BYTES and pipe.dropped_bytes == 75 * FRAME_BYTES
    assert pipe.take(keep)[0] == 76  # no further drops, order kept
    assert AudioPipe(1).take(keep) is None


async def test_backlog_allowance_is_latency_plus_largest_burst(env, monkeypatch):
    monkeypatch.setattr(settings, "WEBRTC_MAX_LATENCY_MS", 100)
    monkeypatch.setenv("FAKE_WEBRTC_ABURST_MS", "1000")
    feed(env)
    video, _ = make_track(audio=True)
    atrack = RTSPAudioStreamTrack(video)
    try:
        await asyncio.wait_for(video.recv(), 5)
        assert await wait_until(lambda: video.audio.max_burst >= 40 * FRAME_BYTES, 3)
        first = await asyncio.wait_for(atrack.recv(), 5)
        # a replayed 1 s GOP is one burst, so it is played from its head (like the video)
        assert pcm_value(first) <= 3
    finally:
        await video.aclose()


# ---------------------------------------------------- failure and lifecycle

async def test_audio_decoder_crash_keeps_video_alive_and_audio_goes_silent(env, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("FAKE_WEBRTC_AUDIO_MODE", "crash")
    monkeypatch.setenv("FAKE_WEBRTC_ABURST_MS", "0")
    feed(env)
    video, ended = make_track(audio=True)
    atrack = RTSPAudioStreamTrack(video)
    try:
        await asyncio.wait_for(video.recv(), 5)
        pipe = video.audio
        got = [await asyncio.wait_for(atrack.recv(), 5) for _ in range(12)]
        assert pipe.dead and "ended" in pipe.reason
        assert [pcm_value(f) for f in got[:3]] == [1, 2, 3]
        assert all(pcm_value(f) == 0 for f in got[-3:])  # silence afterwards
        pts = [f.pts for f in got]
        assert all(b - a >= 960 for a, b in zip(pts, pts[1:]))
        for _ in range(5):  # the video keeps going
            await asyncio.wait_for(video.recv(), 5)
        assert ended == [] and not video._ended
        assert await wait_until(lambda: not alive(pipe.owned.pid), 5)
        assert "continuing without sound" in caplog.text
    finally:
        await video.aclose()


async def test_audio_decoder_that_cannot_start_means_no_sound_not_no_video(env, monkeypatch):
    monkeypatch.setenv("FAKE_WEBRTC_AUDIO_MODE", "reject")
    feed(env)
    video, ended = make_track(audio=True)
    atrack = RTSPAudioStreamTrack(video)
    try:
        await asyncio.wait_for(video.recv(), 5)
        f = await asyncio.wait_for(atrack.recv(), 5)
        assert pcm_value(f) == 0
        assert await wait_until(lambda: video.audio is None or video.audio.dead, 5)
        await asyncio.wait_for(video.recv(), 5)
        assert ended == []
    finally:
        await video.aclose()


async def test_stalled_audio_decoder_is_dropped_and_killed(env, monkeypatch):
    monkeypatch.setenv("FAKE_WEBRTC_AUDIO_MODE", "silent")
    monkeypatch.setattr(settings, "WEBRTC_FIRST_FRAME_TIMEOUT_SECONDS", 1.0)
    feed(env)
    video, ended = make_track(audio=True)
    atrack = RTSPAudioStreamTrack(video)
    try:
        await asyncio.wait_for(video.recv(), 5)
        f = await asyncio.wait_for(atrack.recv(), 5)
        assert pcm_value(f) == 0 and video.audio.dead
        assert await wait_until(lambda: not alive(video.audio.owned.pid), 5)
        assert ended == []
    finally:
        await video.aclose()


async def test_session_close_leaves_no_processes(env, monkeypatch):
    feed(env)
    try:
        sess, _ = await offer_for(env, monkeypatch)
        video, atrack = sess.video_track, sess.audio_track
        await asyncio.wait_for(video.recv(), 5)
        await asyncio.wait_for(atrack.recv(), 5)
        assert await wait_until(lambda: len(pids(env.pid_log)) == 3, 5)  # probe, video, audio
        spawned = pids(env.pid_log)
        assert len(audio_runs(env)) == 1
        assert alive(video.owned.pid) and alive(video.audio.owned.pid)
        assert env.hub.subscriber_count(1) == 1  # one subscription feeds both decoders
    finally:
        await session_mod.close_session("s1")
    assert await wait_until(lambda: not any(alive(p) for p in spawned), 5)
    assert not procs_live()
    assert env.hub.subscriber_count(1) == 0
    with pytest.raises(MediaStreamError):
        await atrack.recv()


def procs_live():
    from app.services.webrtc_manager import process as procs
    return list(procs._live)


async def test_audio_decoder_args(env):
    feed(env)
    video, _ = make_track(audio=True)
    try:
        await asyncio.wait_for(video.recv(), 5)
        assert await wait_until(lambda: len(audio_runs(env)) == 1, 5)
        (argv,) = audio_runs(env)
        assert "-vn" in argv and argv[argv.index("-map") + 1] == "0:a:0"
        assert argv[argv.index("-f") + 1] == "s16le" and argv[argv.index("-ar") + 1] == "48000"
        assert argv[argv.index("-ac") + 1] == "1" and "pipe:1" in argv and "-re" not in argv
    finally:
        await video.aclose()


async def test_no_audio_in_stream_spawns_no_audio_decoder(env):
    feed(env, audio=False)
    video, _ = make_track(audio=True)
    atrack = RTSPAudioStreamTrack(video)
    try:
        await asyncio.wait_for(video.recv(), 5)
        assert video.audio is None and not audio_runs(env)
        assert pcm_value(await asyncio.wait_for(atrack.recv(), 5)) == 0  # silence, not a hang
    finally:
        await video.aclose()


async def test_video_only_track_never_touches_audio(env):
    feed(env)
    video, _ = make_track()  # audio=False: the old behaviour
    try:
        await asyncio.wait_for(video.recv(), 5)
        assert video.audio is None and not audio_runs(env)
    finally:
        await video.aclose()


# ------------------------------------------------------------- real ffmpeg

def test_real_probe_detects_audio_in_generated_fmp4():
    from tests.ingest import test_webrtc_real as real
    if not (real.FFMPEG and real.FFPROBE):
        pytest.skip("ffmpeg/ffprobe not available")
    from app.services.fmp4 import Fmp4Splitter
    from tests.ingest.test_real_transcoder import _encoders, _generate
    if "libx264" not in _encoders():
        pytest.skip("libx264 not available")
    sp = Fmp4Splitter()
    sp.feed(_generate("libx264", 2))
    assert sp.init_segment and probe_mod.has_audio(sp.init_segment) is True


from tests.ingest.test_webrtc_real import FFMPEG, FFPROBE, H, W, real_env, sample  # noqa: E402,F401


@pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not available")
async def test_real_ffmpeg_audio_track_delivers_the_sine(sample, real_env):
    """Real hub fMP4 (video + AAC sine) -> real audio ffmpeg -> 20 ms frames with the tone in them."""
    import numpy as np

    codec, _, init, fragments = sample
    assert probe_mod.has_audio(init)
    cam_hub = real_env.get(1)
    cam_hub.feed(init)
    pre = 0
    for frag in fragments:
        cam_hub.feed(frag)
        pre += 1
        if cam_hub.gop_bytes > 20_000 and pre >= 3:
            break
    rest = fragments[pre:]

    async def live():
        for frag in rest:
            cam_hub.feed(frag)
            await asyncio.sleep(0.5)

    feeder = asyncio.create_task(live())
    ended = []
    video = RTSPVideoStreamTrack("rtsp://u:p@127.0.0.1/x", cam_id=1, on_end=ended.append, audio=True)
    atrack = RTSPAudioStreamTrack(video)
    try:
        first_video = await asyncio.wait_for(video.recv(), 30)
        assert (first_video.width, first_video.height) == (W, H) and video.audio is not None
        frames = [await asyncio.wait_for(atrack.recv(), 15) for _ in range(100)]  # ~2 s
        pcm = np.concatenate([np.frombuffer(bytes(f.planes[0]), dtype="<i2") for f in frames]).astype(float)
        pts = [f.pts for f in frames]
        assert all(f.samples == 960 and f.sample_rate == 48000 for f in frames)
        assert all(b - a >= 960 for a, b in zip(pts, pts[1:]))
        assert np.sqrt((pcm ** 2).mean()) > 500, "audio is silent"
        spectrum = np.abs(np.fft.rfft(pcm * np.hanning(len(pcm))))
        peak_hz = np.argmax(spectrum) * 48000 / len(pcm)
        assert abs(peak_hz - 440) < 20, peak_hz
        assert not video.audio.dead and ended == []
        pid = video.audio.owned.pid
    finally:
        feeder.cancel()
        await video.aclose()
    assert not alive(pid) and real_env.subscriber_count(1) == 0

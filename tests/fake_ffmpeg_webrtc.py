#!/usr/bin/env python3
"""Fake ffmpeg + ffprobe for the WebRTC decoder tests (no network, no real codecs).

Called as ffprobe (argv has -show_entries): reads stdin to EOF (if `-i pipe:0`), prints
the probe result for the size in $FAKE_WEBRTC_SIZE ("WxH@FPS", default 64x48@10) as JSON
(or csv `w,h,num/den` for the direct RTSP form). $FAKE_WEBRTC_PROBE=fail -> exit 1.

Called as ffmpeg: writes raw yuv420p frames of $FAKE_WEBRTC_SIZE to stdout. The first byte
of every frame is its sequence number (mod 256). From `-i pipe:0` the first stdin bytes
start the output: $FAKE_WEBRTC_BURST frames at once (the replayed GOP, default 0), then
paced at fps. From an rtsp URL it starts at once, paced. $FAKE_WEBRTC_MODE:
    ok      as described; exits 0 on stdin EOF
    silent  never writes a frame
    stall   writes 3 frames, then goes silent
    crash   writes 3 frames, then exits 1 (stderr mentions the URL with its password)
    hang    like stall but ignores SIGTERM
    reject  exits 1 at once (like ffmpeg refusing its options)
$FAKE_FFMPEG_ARGS_LOG: JSON argv per start; $FAKE_FFMPEG_LOG: "<pid> <time>" per start.
The process exits by itself after 60 s whatever happens.
"""
import json
import os
import signal
import sys
import threading
import time


def size():
    spec = os.environ.get("FAKE_WEBRTC_SIZE", "64x48@10")
    wh, _, fps = spec.partition("@")
    w, _, h = wh.partition("x")
    return int(w), int(h), float(fps or 10)


def log_start(argv):
    path = os.environ.get("FAKE_FFMPEG_ARGS_LOG")
    if path:
        with open(path, "a") as fh:
            fh.write(json.dumps(argv) + "\n")
    path = os.environ.get("FAKE_FFMPEG_LOG")
    if path:
        with open(path, "a") as fh:
            fh.write(f"{os.getpid()} {time.time():.3f}\n")


def probe(argv):
    w, h, fps = size()
    log_start(argv)
    if "pipe:0" in argv:
        while sys.stdin.buffer.read(65536):
            pass
    if os.environ.get("FAKE_WEBRTC_PROBE") == "fail":
        sys.stderr.write("fake ffprobe: Invalid data found\n")
        return 1
    if "json" in argv:
        print(json.dumps({"streams": [{"width": w, "height": h, "r_frame_rate": f"{int(fps)}/1",
                                       "avg_frame_rate": f"{int(fps)}/1"}]}))
    else:
        print(f"{w},{h},{int(fps)}/1")
    return 0


def main():
    argv = sys.argv[1:]
    if "-show_entries" in argv:
        return probe(argv)
    mode = os.environ.get("FAKE_WEBRTC_MODE", "ok")
    log_start(argv)
    if mode == "reject":
        sys.stderr.write("fake: Unrecognized option 'x' rtsp://user:secret@cam/x\n")
        return 1
    if mode == "hang":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    deadline = time.time() + 60
    w, h, fps = size()
    frame_len = w * h + 2 * ((w + 1) // 2) * ((h + 1) // 2)
    out = sys.stdout.buffer
    from_pipe = "pipe:0" in argv
    started = threading.Event()
    eof = threading.Event()

    def reader():
        while os.read(0, 65536):
            started.set()
        eof.set()
        started.set()

    if from_pipe:
        threading.Thread(target=reader, daemon=True).start()
        started.wait(deadline - time.time())
    seq = 0

    def emit():
        nonlocal seq
        out.write(bytes([seq % 256]) * frame_len)
        out.flush()
        seq += 1

    try:
        if mode == "silent":
            while time.time() < deadline:
                time.sleep(0.1)
            return 0
        for _ in range(int(os.environ.get("FAKE_WEBRTC_BURST", "0")) if from_pipe else 0):
            emit()
        while time.time() < deadline:
            if eof.is_set():
                return 0
            if mode in ("stall", "hang") and seq >= 3:
                time.sleep(0.1)
                continue
            if mode == "crash" and seq >= 3:
                sys.stderr.write("fake: boom rtsp://user:secret@cam/x\n")
                return 1
            emit()
            time.sleep(1.0 / fps)
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

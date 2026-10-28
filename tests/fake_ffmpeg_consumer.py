#!/usr/bin/env python3
"""Fake ffmpeg + ffprobe for the stage 5 tests (snapshots and motion detection on the hub).
No network, no real codecs.

ffprobe (argv has -show_entries): prints JSON for $FAKE_HC_SIZE ("WxH", default 64x48);
reads stdin to EOF first for `-i pipe:0`. $FAKE_HC_PROBE=fail -> exit 1.

ffmpeg, by output kind:
  * JPEG snapshot (`mjpeg` in argv, input pipe:0): reads stdin to EOF, then writes
    $FAKE_HC_JPEGS (default 3) fake JPEGs `FFD8FF 00 <seq> FFD9` -- the last one has seq=N-1.
    $FAKE_HC_JPEG_STDIN=<path>: the bytes read from stdin are saved there (to check what
    the decoder was fed).
  * raw bgr24 frames from `-i pipe:0` (hub decoder): after the init segment
    ($FAKE_HC_INIT_BYTES bytes) every new stdin data makes it write $FAKE_HC_BURST (default
    3) frames of $FAKE_HC_SIZE at once. The first 8 bytes of a frame = total stdin bytes
    read so far (big endian), so a test can tell which fragments the decoder saw. Exits 0
    on stdin EOF.
  * raw bgr24 frames from an rtsp URL (direct path): $FAKE_HC_DIRECT (default 3) frames at
    once, then idles. Frame marker = 0.
$FAKE_HC_MODE:
    ok       as described
    silent   never writes a frame (hub decoder / direct)
    crash    exits 1 at once; stderr mentions the URL with its password
    trunc    writes half a frame, exits 0; only for the first $FAKE_HC_TRUNC_RUNS starts (default 1)
             (counted in $FAKE_HC_COUNTER), later starts behave as ok
    hang     like ok but ignores SIGTERM; a snapshot never finishes after reading stdin
    stall    a snapshot never finishes after reading stdin (still obeys SIGTERM)
    hubcrash exits 1 at once when the input is pipe:0 (hub decoder), direct RTSP works
$FAKE_FFMPEG_ARGS_LOG: JSON argv per start; $FAKE_FFMPEG_LOG: "<pid> <time>" per start.
The process exits by itself after 60 s whatever happens.
"""
import json
import os
import signal
import struct
import sys
import time


def size():
    w, _, h = os.environ.get("FAKE_HC_SIZE", "64x48").partition("x")
    return int(w), int(h)


def log_start(argv):
    path = os.environ.get("FAKE_FFMPEG_ARGS_LOG")
    if path:
        with open(path, "a") as fh:
            fh.write(json.dumps(argv) + "\n")
    path = os.environ.get("FAKE_FFMPEG_LOG")
    if path:
        with open(path, "a") as fh:
            fh.write(f"{os.getpid()} {time.time():.3f}\n")


def read_all_stdin():
    data = bytearray()
    while True:
        chunk = os.read(0, 65536)
        if not chunk:
            return bytes(data)
        data += chunk


def probe(argv):
    w, h = size()
    log_start(argv)
    if "pipe:0" in argv:
        read_all_stdin()
    if os.environ.get("FAKE_HC_PROBE") == "fail":
        sys.stderr.write("fake ffprobe: Invalid data found\n")
        return 1
    if "json" in argv:
        print(json.dumps({"streams": [{"width": w, "height": h, "r_frame_rate": "13/1", "avg_frame_rate": "13/1"}]}))
    else:
        print(f"{w},{h}")  # csv=p=0 of the direct RTSP probe
    return 0


def trunc_run():
    """True for the first FAKE_HC_TRUNC_RUNS starts of mode=trunc."""
    path = os.environ.get("FAKE_HC_COUNTER")
    n = 0
    if path:
        try:
            n = int(open(path).read() or 0)
        except OSError:
            n = 0
        with open(path, "w") as fh:
            fh.write(str(n + 1))
    return n < int(os.environ.get("FAKE_HC_TRUNC_RUNS", "1"))


def main():
    argv = sys.argv[1:]
    if "-show_entries" in argv:
        return probe(argv)
    log_start(argv)
    mode = os.environ.get("FAKE_HC_MODE", "ok")
    w, h = size()
    out = sys.stdout.buffer
    deadline = time.time() + 60
    if mode == "crash" or (mode == "hubcrash" and "pipe:0" in argv):
        sys.stderr.write("fake: boom rtsp://user:secret@cam/x\n")
        return 1
    if mode == "hang":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    frame_len = w * h * 3
    try:
        if "mjpeg" in argv:  # snapshot
            data = read_all_stdin()
            path = os.environ.get("FAKE_HC_JPEG_STDIN")
            if path:
                with open(path, "wb") as fh:
                    fh.write(data)
            n = int(os.environ.get("FAKE_HC_JPEGS", "3"))
            if mode in ("hang", "stall"):
                while time.time() < deadline:
                    time.sleep(0.1)
                return 0
            if mode == "silent":
                return 0
            for seq in range(n):
                out.write(b"\xff\xd8\xff\x00" + bytes([seq]) + b"\xff\xd9")
            out.flush()
            return 0
        if mode == "trunc" and trunc_run():
            out.write(b"\x01" * (frame_len // 2))
            out.flush()
            return 0
        if mode == "silent":
            while time.time() < deadline:
                time.sleep(0.1)
            return 0
        if "pipe:0" in argv:  # hub decoder
            init_bytes = int(os.environ.get("FAKE_HC_INIT_BYTES", "0"))
            burst = int(os.environ.get("FAKE_HC_BURST", "3"))
            total = 0
            while time.time() < deadline:
                chunk = os.read(0, 65536)
                if not chunk:
                    return 0
                before = total
                total += len(chunk)
                if total > init_bytes and total > before:
                    for _ in range(burst):
                        out.write(struct.pack(">Q", total) + b"\0" * (frame_len - 8))
                    out.flush()
            return 0
        # direct RTSP
        for _ in range(int(os.environ.get("FAKE_HC_DIRECT", "3"))):
            out.write(struct.pack(">Q", 0) + b"\0" * (frame_len - 8))
        out.flush()
        while time.time() < deadline:
            time.sleep(0.1)
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

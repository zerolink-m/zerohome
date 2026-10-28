#!/usr/bin/env python3
"""Fake ffmpeg for the HLS manager tests: no network, no real encoding. Writes segment
files and a playlist like `-f hls` does (segments first, then the playlist).

Invoked without `-f hls` (as the ingest ffmpeg) it delegates to fake_ffmpeg.py, so one
FFMPEG_BINARY serves ingest and HLS in a test.
Input: `-i pipe:0` (hub source; stdin is drained, EOF ends the run) or `-i <rtsp url>`
(direct source). Mode from the environment, per source:
    FAKE_HLS_HUB_MODE / FAKE_HLS_DIRECT_MODE:
    ok (default)  a new segment every $FAKE_HLS_INTERVAL seconds (default 0.15)
    silent        writes nothing, sleeps (still drains stdin)
    stall         two segments, then nothing
    crash         two segments, then exit 1 (stderr mentions the camera URL with password)
    hang          like ok, but ignores SIGTERM and leaves a SIGTERM-immune child
    reject        exit 1 at once (like an ffmpeg refusing its options)
Env FAKE_FFMPEG_LOG (one "<pid> <time>" line per start), FAKE_FFMPEG_ARGS_LOG (one JSON
argv line per start), FAKE_FFMPEG_CHILD_PIDFILE (hang). Always exits by itself after 120 s.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time


def opt(argv, name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default


def main() -> int:
    argv = sys.argv[1:]
    if "hls" not in argv:  # the ingest ffmpeg (fMP4 to stdout): behave like fake_ffmpeg.py
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import fake_ffmpeg
        return fake_ffmpeg.main()
    source = opt(argv, "-i", "")
    hub = source == "pipe:0"
    mode = os.environ.get("FAKE_HLS_HUB_MODE" if hub else "FAKE_HLS_DIRECT_MODE", "ok")
    args_log = os.environ.get("FAKE_FFMPEG_ARGS_LOG")
    if args_log:
        with open(args_log, "a") as fh:
            fh.write(json.dumps(argv) + "\n")
    log = os.environ.get("FAKE_FFMPEG_LOG")
    if log:
        with open(log, "a") as fh:
            fh.write(f"{os.getpid()} {time.time():.3f}\n")
    if mode == "reject":
        sys.stderr.write(f"fake: Unrecognized option 'x' {source or 'rtsp://user:secret@cam/x'}\n")
        return 1
    if mode == "hang":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        child = subprocess.Popen([sys.executable, "-c",
                                  "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(120)"])
        pidfile = os.environ.get("FAKE_FFMPEG_CHILD_PIDFILE")
        if pidfile:
            open(pidfile, "w").write(str(child.pid))
        mode = "ok"

    if hub:
        def drain():
            while os.read(sys.stdin.fileno(), 65536):
                pass
            os._exit(0)  # EOF on stdin: like ffmpeg finishing its input
        threading.Thread(target=drain, daemon=True).start()

    playlist = argv[-1]
    pattern = opt(argv, "-hls_segment_filename")
    first = int(opt(argv, "-start_number", 0))
    size = int(opt(argv, "-hls_list_size", 10))
    interval = float(os.environ.get("FAKE_HLS_INTERVAL", "0.15"))
    deadline = time.time() + 120
    window: list[tuple[int, str]] = []
    n = first

    def write_segment():
        nonlocal n
        path = pattern % n
        with open(path, "wb") as fh:
            fh.write(b"\x47" + b"TS%d" % n)
        window.append((n, os.path.basename(path)))
        del window[:-size]
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:2",
                 f"#EXT-X-MEDIA-SEQUENCE:{window[0][0]}"]
        for _, name in window:
            lines += ["#EXTINF:2.000000,", name]
        tmp = playlist + ".tmp"
        with open(tmp, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        os.replace(tmp, playlist)
        n += 1

    if mode == "silent":
        while time.time() < deadline:
            time.sleep(0.1)
        return 0
    produced = 0
    while time.time() < deadline:
        if mode in ("stall", "crash") and produced >= 2:
            if mode == "crash":
                sys.stderr.write("fake: boom reading rtsp://user:secret@cam/x\n")
                return 1
            time.sleep(0.1)
            continue
        write_segment()
        produced += 1
        time.sleep(interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())

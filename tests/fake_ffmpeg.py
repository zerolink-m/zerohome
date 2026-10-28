#!/usr/bin/env python3
"""Fake ffmpeg for ingest tests: speaks no RTSP, ignores the network entirely.

Behaviour comes from the URL given after `-i` (the camera path):
    rtsp://u:p@host:554/<mode>?key=value&...
modes:
    ok      emit init + fragments at `rate` per second (default 20), a keyframe every
            `gop` fragments (default 5); fragment payload `size` bytes (default 1000)
    crash   emit init + `frags` fragments (default 3), then exit with code 1
    silent  print nothing at all and sleep
    stall   emit init + `frags` fragments (default 3), then go silent (sleep)
    hang    like ok, but ignore SIGTERM and leave a SIGTERM-immune child in the group
    flaky   exit 1 immediately for the first `fail` runs (counted in
            $FAKE_FFMPEG_COUNTER), then behave as ok
Env: FAKE_FFMPEG_LOG   file; one line "<pid> <time>" appended per start
     FAKE_FFMPEG_CHILD_PIDFILE   (hang) file receiving the child's pid
Transcoder mode: when the input is `-i pipe:0` the fake reads fMP4 from stdin and writes
an fMP4 to stdout (init once the first input bytes arrived, then one fragment per input
read). Behaviour from $FAKE_TRANSCODER_MODE:
    ok (default)   as described, exits 0 on stdin EOF
    silent         read stdin, never write
    crash          write init + 2 fragments, then exit 1
    hang           like ok but ignore SIGTERM
    reject         exit 1 at once (like an ffmpeg that refuses its options)
Env FAKE_FFMPEG_ARGS_LOG: file; one JSON line with argv appended per start.
The process always exits by itself after `life` seconds (default 120) so a failing
test can never leave it behind for long.
"""
import os
import signal
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fmp4_builder as fb  # noqa: E402


def transcoder(argv: list[str]) -> int:
    import json

    mode = os.environ.get("FAKE_TRANSCODER_MODE", "ok")
    args_log = os.environ.get("FAKE_FFMPEG_ARGS_LOG")
    if args_log:
        with open(args_log, "a") as fh:
            fh.write(json.dumps(argv) + "\n")
    log = os.environ.get("FAKE_FFMPEG_LOG")
    if log:
        with open(log, "a") as fh:
            fh.write(f"{os.getpid()} {time.time():.3f}\n")
    if mode == "reject":
        sys.stderr.write("fake: Unrecognized option 'x' rtsp://user:secret@cam/x\n")
        return 1
    if mode == "hang":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    deadline = time.time() + 120
    out = sys.stdout.buffer
    stdin = sys.stdin.buffer
    seq = 0
    started = False
    while time.time() < deadline:
        data = os.read(stdin.fileno(), 65536)
        if not data:
            return 0
        if mode == "silent":
            continue
        try:
            if not started:
                out.write(fb.init_segment())
                started = True
            out.write(fb.fragment(seq, key=(seq % 5 == 0), size=500))
            out.flush()
        except BrokenPipeError:
            return 0
        seq += 1
        if mode == "crash" and seq >= 2:
            sys.stderr.write("fake: boom rtsp://user:secret@cam/x\n")
            return 1
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if "-i" in argv and argv[argv.index("-i") + 1] == "pipe:0":
        return transcoder(argv)
    url = argv[argv.index("-i") + 1] if "-i" in argv else ""
    parts = urlsplit(url)
    mode = parts.path.strip("/") or "ok"
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    rate = float(q.get("rate", 20))
    gop = int(q.get("gop", 5))
    size = int(q.get("size", 1000))
    frags = int(q.get("frags", 3))
    deadline = time.time() + float(q.get("life", 120))

    log = os.environ.get("FAKE_FFMPEG_LOG")
    if log:
        with open(log, "a") as fh:
            fh.write(f"{os.getpid()} {time.time():.3f}\n")

    out = sys.stdout.buffer

    def emit(data: bytes) -> None:
        try:
            out.write(data)
            out.flush()
        except BrokenPipeError:
            sys.exit(0)

    def sleep_forever() -> None:
        while time.time() < deadline:
            time.sleep(0.1)

    if mode == "flaky":
        counter = os.environ["FAKE_FFMPEG_COUNTER"]
        n = int(open(counter).read()) if os.path.exists(counter) else 0
        open(counter, "w").write(str(n + 1))
        if n < int(q.get("fail", 2)):
            sys.stderr.write("fake: connection refused\n")
            return 1
        mode = "ok"
    if mode == "silent":
        sleep_forever()
        return 0

    if mode == "hang":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        child = subprocess.Popen(
            [sys.executable, "-c",
             "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(120)"]
        )
        pidfile = os.environ.get("FAKE_FFMPEG_CHILD_PIDFILE")
        if pidfile:
            open(pidfile, "w").write(str(child.pid))
        mode = "ok"

    emit(fb.init_segment())
    seq = 0
    if mode in ("crash", "stall"):
        for _ in range(frags):
            emit(fb.fragment(seq, key=True, size=size))
            seq += 1
        if mode == "crash":
            sys.stderr.write("fake: connection reset rtsp://user:secret@cam/x\n")
            return 1
        sleep_forever()
        return 0

    interval = 1.0 / rate
    while time.time() < deadline:
        emit(fb.fragment(seq, key=(seq % gop == 0), size=size))
        seq += 1
        time.sleep(interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())

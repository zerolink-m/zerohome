"""Runs the REAL ffmpeg with the ingest flags, loopback only.

ffmpeg reports unknown options ("Option rw_timeout not found") only AFTER the input
opened, so a refused connection is not enough: a tiny RTSP server on 127.0.0.1 answers
OPTIONS/DESCRIBE/SETUP/PLAY, which gets ffmpeg past opening. No cameras, no network
beyond loopback."""
import shutil
import socket
import subprocess
import threading

import pytest

from app.core.config import settings
from app.services.ingest.hub import IngestHub
from app.services.ingest.supervisor import IngestSupervisor

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")

_SDP = (
    "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=x\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
    "m=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"
    "a=fmtp:96 packetization-mode=1;profile-level-id=42e01f;"
    "sprop-parameter-sets=Z0LgH9kAoD2gQAAAAwBAAAAMo8WLZYA=,aMuMsg==\r\na=control:trackID=0\r\n"
)


def _handle(conn: socket.socket) -> None:
    conn.settimeout(3)
    try:
        while True:
            data = conn.recv(4096).decode(errors="replace")
            if not data:
                return
            lines = data.split("\r\n")
            cseq = next(ln for ln in lines if ln.lower().startswith("cseq"))
            method = lines[0].split()[0]
            body, headers = "", ""
            if method == "OPTIONS":
                headers = "Public: OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN\r\n"
            elif method == "DESCRIBE":
                body = _SDP
                headers = f"Content-Type: application/sdp\r\nContent-Length: {len(body)}\r\n"
            elif method == "SETUP":
                headers = "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\nSession: 1\r\n"
            elif method == "PLAY":
                headers = "Session: 1\r\n"
            conn.sendall(f"RTSP/1.0 200 OK\r\n{cseq}\r\n{headers}\r\n{body}".encode())
    except (OSError, StopIteration):
        pass
    finally:
        conn.close()


@pytest.fixture
def rtsp_server():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=_handle, args=(conn,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    yield srv.getsockname()[1]
    srv.close()


def _run(url, cfg):
    sup = IngestSupervisor(hub=IngestHub(), config=cfg)
    args = sup._args(url)
    proc = subprocess.Popen(
        args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace",
    )
    try:
        _, err = proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, err = proc.communicate()
    return proc.returncode, err


def _assert_options_known(err):
    assert "Unrecognized option" not in err, err
    assert not ("Option" in err and "not found" in err), err


@pytest.fixture
def real_cfg(monkeypatch, cfg):
    monkeypatch.setattr(settings, "FFMPEG_BINARY", shutil.which("ffmpeg"))
    cfg.rtsp_timeout_seconds = 2.0
    return cfg


def test_flags_accepted_after_input_opens(real_cfg, rtsp_server):
    rc, err = _run(f"rtsp://127.0.0.1:{rtsp_server}/x", real_cfg)
    _assert_options_known(err)


def test_connection_refused_on_closed_loopback_port(real_cfg):
    rc, err = _run("rtsp://127.0.0.1:1/x", real_cfg)
    _assert_options_known(err)
    assert rc != 0 and "refused" in err.lower(), err

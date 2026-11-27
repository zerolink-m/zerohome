from __future__ import annotations

import asyncio
import contextlib
import hmac
import hashlib
import base64
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer, RTCIceCandidate, VideoStreamTrack
from aiortc.contrib.media import MediaBlackhole
from aiortc.sdp import candidate_from_sdp
from aiortc.mediastreams import MediaStreamError, VIDEO_TIME_BASE, VIDEO_CLOCK_RATE
from av import VideoFrame
import av

from app.core.config import settings
from app.models.cams import Cam
from app.services.rtsp_url import build_rtsp_url as _build_rtsp_url

from app.services.webrtc_manager import process as procs
from app.services.webrtc_manager.ice import WebRTCStreamError, generate_turn_credentials, get_ice_servers
from app.services.webrtc_manager.portrange import configure_port_range
from app.services.webrtc_manager.sdp import sanitize_local_sdp
from app.services.webrtc_manager.audio import RTSPAudioStreamTrack
from app.services.webrtc_manager.track import RTSPVideoStreamTrack, hub_has_audio


logger = logging.getLogger(__name__)

_CLOSE_TIMEOUT_SECONDS = 6.0  # one session: decoder stop + peer connection close
_STOP_ALL_TIMEOUT_SECONDS = 8.0  # app shutdown: all sessions together


@dataclass
class WebRTCSession:
    """WebRTC session for one peer connection"""
    cam_id: int
    peer_connection: RTCPeerConnection
    video_track: RTSPVideoStreamTrack
    created_at: float
    audio_track: RTSPAudioStreamTrack | None = None
    pending_ice: list[RTCIceCandidate | None] = field(default_factory=list)


_sessions: dict[str, WebRTCSession] = {}  # session_id -> WebRTCSession
_guard: asyncio.Lock = asyncio.Lock()



async def create_session(
    cam: Cam,
    session_id: str,
    on_end: Callable[[RTSPVideoStreamTrack], None] | None = None,
) -> RTCPeerConnection:
    """Create new WebRTC peer connection for a camera.

    `on_end(track)` fires when the video stream ends by itself (ingest restart, decoder
    death/stall) so the caller can tell the client to reconnect."""
    ice_servers = get_ice_servers()
    configure_port_range()

    pc = RTCPeerConnection(
        configuration=RTCConfiguration(iceServers=ice_servers)
    )

    # Create and add video track immediately
    rtsp_url = _build_rtsp_url(cam)
    # Audio only when the camera's stream has it: decided here, before the offer is made
    try:
        with_audio = await hub_has_audio(cam.id)
    except Exception:
        logger.exception("webrtc: audio check failed for cam %s, no audio", cam.id)
        with_audio = False
    video_track = RTSPVideoStreamTrack(rtsp_url, cam_id=cam.id, on_end=on_end, audio=with_audio)

    # Add track with sendonly transceiver
    pc.addTransceiver(video_track, direction="sendonly")
    audio_track = None
    if with_audio:
        audio_track = RTSPAudioStreamTrack(video_track)
        pc.addTransceiver(audio_track, direction="sendonly")

    # Store session
    session = WebRTCSession(
        cam_id=cam.id,
        peer_connection=pc,
        video_track=video_track,
        created_at=time.time(),
        audio_track=audio_track,
    )

    async with _guard:
        _sessions[session_id] = session

    # Setup connection state handler
    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        logger.debug(f"webrtc: Connection state changed: {pc.connectionState}")
        if pc.connectionState in ("failed", "closed"):
            await close_session(session_id)

    # Log ICE connection state changes
    @pc.on("iceconnectionstatechange")
    async def on_iceconnectionstatechange():
        logger.debug(f"webrtc: ICE connection state: {pc.iceConnectionState}")

    # Log ICE gathering state
    @pc.on("icegatheringstatechange")
    async def on_icegatheringstatechange():
        logger.debug(f"webrtc: ICE gathering state: {pc.iceGatheringState}")

    return pc


async def create_offer(session_id: str) -> str:
    """Create SDP offer from server side"""
    async with _guard:
        session = _sessions.get(session_id)

    if session is None:
        raise WebRTCStreamError("Session not found")

    pc = session.peer_connection

    logger.debug("webrtc: Creating offer...")
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    logger.debug("webrtc: Offer created and set as local description")

    # Log ICE candidates in the offer
    if pc.localDescription and pc.localDescription.sdp:
        sdp_lines = pc.localDescription.sdp.split('\n')
        candidates = [line for line in sdp_lines if line.startswith('a=candidate:')]
        logger.debug(f"webrtc: Generated {len(candidates)} ICE candidates")
        for i, candidate in enumerate(candidates[:5]):  # Log first 5
            # Extract IP from candidate
            parts = candidate.split()
            if len(parts) > 4:
                ip = parts[4]
                typ = parts[7] if len(parts) > 7 else 'unknown'
                logger.debug(f"webrtc:   Candidate {i+1}: {ip} (type: {typ})")
        if len(candidates) > 5:
            logger.debug(f"webrtc:   ... and {len(candidates) - 5} more candidates")

    if pc.localDescription is None:
        raise WebRTCStreamError("Failed to create offer")

    return sanitize_local_sdp(pc.localDescription.sdp)


async def handle_answer(session_id: str, sdp: str) -> None:
    """Handle SDP answer from client"""
    async with _guard:
        session = _sessions.get(session_id)

    if session is None:
        raise WebRTCStreamError("Session not found")

    pc = session.peer_connection

    logger.debug("webrtc: Setting remote description (answer)...")
    answer = RTCSessionDescription(sdp=sdp, type="answer")
    await pc.setRemoteDescription(answer)
    await _flush_pending_ice(session)
    logger.debug("webrtc: Remote description set")


async def handle_offer(session_id: str, sdp: str) -> str:
    """Handle SDP offer and return SDP answer - LEGACY, prefer server-side offer"""
    async with _guard:
        session = _sessions.get(session_id)

    if session is None:
        raise WebRTCStreamError("Session not found")

    pc = session.peer_connection

    logger.debug("webrtc: Setting remote description...")
    offer = RTCSessionDescription(sdp=sdp, type="offer")
    await pc.setRemoteDescription(offer)
    await _flush_pending_ice(session)
    logger.debug("webrtc: Remote description set")

    logger.debug("webrtc: Creating answer...")
    answer = await pc.createAnswer()
    logger.debug("webrtc: Answer created")

    logger.debug("webrtc: Setting local description...")
    await pc.setLocalDescription(answer)
    logger.debug("webrtc: Local description set")

    if pc.localDescription is None:
        raise WebRTCStreamError("Failed to create answer")

    return sanitize_local_sdp(pc.localDescription.sdp)


async def _flush_pending_ice(session: WebRTCSession) -> None:
    for candidate in session.pending_ice:
        await session.peer_connection.addIceCandidate(candidate)
    session.pending_ice.clear()


def _parse_ice_candidate(candidate: dict | None) -> RTCIceCandidate | None:
    if candidate is None or (isinstance(candidate, dict) and candidate.get("candidate") == ""):
        return None
    if not isinstance(candidate, dict):
        raise WebRTCStreamError("Некорректный ICE-кандидат")

    raw = candidate.get("candidate")
    mid = candidate.get("sdpMid")
    index = candidate.get("sdpMLineIndex")
    if (
        not isinstance(raw, str) or len(raw) > 2048
        or (mid is not None and not isinstance(mid, str))
        or (index is not None and (type(index) is not int or index < 0))
        or (mid is None and index is None)
    ):
        raise WebRTCStreamError("Некорректный ICE-кандидат")
    try:
        parsed = candidate_from_sdp(raw.removeprefix("candidate:"))
    except (AssertionError, IndexError, ValueError) as exc:
        raise WebRTCStreamError("Некорректный ICE-кандидат") from exc
    parsed.sdpMid = mid
    parsed.sdpMLineIndex = index
    return parsed


async def add_ice_candidate(session_id: str, candidate: dict | None) -> None:
    """Accept the candidate fields produced by RTCIceCandidate.toJSON()."""
    async with _guard:
        session = _sessions.get(session_id)
    if session is None:
        return

    parsed = _parse_ice_candidate(candidate)
    if session.peer_connection.remoteDescription is None:
        if len(session.pending_ice) >= 64:
            raise WebRTCStreamError("Слишком много ICE-кандидатов до получения SDP")
        session.pending_ice.append(parsed)
        return
    await session.peer_connection.addIceCandidate(parsed)


async def close_session(session_id: str) -> None:
    """Close WebRTC session and cleanup resources"""
    async with _guard:
        session = _sessions.pop(session_id, None)

    if session is None:
        return

    # Stop the track (decoder + hub subscription) and close the peer connection; bounded
    # so a stuck process or connection cannot hang a handler or the shutdown.
    try:
        await asyncio.wait_for(_shutdown(session), timeout=_CLOSE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.warning("webrtc: closing session of cam %s timed out, force-killing", session.cam_id)
        session.video_track.stop()
        if session.video_track.owned is not None:
            session.video_track.owned.kill_now()
        audio = session.video_track.audio
        if audio is not None and audio.owned is not None:
            audio.owned.kill_now()


async def _shutdown(session: WebRTCSession) -> None:
    if session.audio_track is not None:
        session.audio_track.stop()
    await session.video_track.aclose()
    await session.peer_connection.close()


async def stop_all_sessions(timeout: float = _STOP_ALL_TIMEOUT_SECONDS) -> None:
    """Stop all WebRTC sessions (called on shutdown): concurrently, within `timeout`
    overall, then whatever is left is SIGKILLed (ownership-checked)."""
    async with _guard:
        session_ids = list(_sessions.keys())

    if session_ids:
        tasks = [asyncio.create_task(close_session(sid)) for sid in session_ids]
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            logger.warning("webrtc: shutdown budget exceeded, force-killing the rest")
            for task in pending:
                task.cancel()
            await asyncio.wait(pending, timeout=1.0)
    procs.force_kill_all()


async def cleanup_old_sessions(max_age_seconds: int = 3600) -> None:
    """Remove sessions older than max_age_seconds"""
    now = time.time()

    async with _guard:
        old_sessions = [
            sid for sid, session in _sessions.items()
            if now - session.created_at > max_age_seconds
        ]

    for session_id in old_sessions:
        await close_session(session_id)
